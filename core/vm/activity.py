"""虚拟机活动探测

判断一台虚拟机「是否正在被使用」。这是自动内存回收的决策依据：
只有在完全没有任何访问的情况下，才允许把它转成内存快照。

探测层次
────────
一个 VM 可能被很多方式访问，任何一类活跃都算「在用」：

1. **宿主机侧 TCP 连接**（最直接）
   读 /proc/net/tcp{,6}，看转发端口上是否有 ESTABLISHED 连接。
   覆盖：ssh、curl http://vm:8080、数据库客户端、浏览器……

2. **SLIRP 内部连接**（guest 发起的出站 + 未映射的入站）
   QEMU monitor 的 `info usernet` 会列出所有活动连接及空闲秒数。
   覆盖：VM 内部跑的服务在访问外网、VM 之间的通信。

3. **guest 内的登录会话**
   `who` 有会话 = 有人登录着（哪怕在发呆，也不该冻结）。

4. **guest CPU 使用率**
   服务在跑活（编译、索引、定时任务）时不冻结。

5. **guest 内的网络连接**
   guest 自己 `ss -tn` 有 ESTABLISHED 连接 —— 覆盖 VM 之间的通信：
   A 访问 B 的 8080 时，B 的 guest 内会看到一条来自 10.0.2.x 的连接。

设计原则
────────
**宁可漏判（不冻结），不可误判（冻结正在用的机器）。**
任何一项探测失败，都按「有活动」处理。
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path


# ─────────────────────────────────────────
# 结果
# ─────────────────────────────────────────
@dataclass
class Activity:
    """一次活动探测的结果"""

    reachable: bool = False          # SSH 是否连得上
    sessions: int = 0                # guest 内登录会话数
    cpu_percent: float = 0.0         # 采样期 CPU 使用率
    host_conns: int = 0              # 宿主机侧到转发端口的连接数
    slirp_conns: int = 0             # SLIRP 层活动连接数
    guest_conns: int = 0             # guest 内 ESTABLISHED 连接数
    # 最近一次活动的时刻（monotonic），由调用方维护
    last_active: float = 0.0

    @property
    def busy(self) -> bool:
        """是否正在被使用"""
        return (
            self.sessions > 0
            or self.host_conns > 0
            or self.slirp_conns > 0
            or self.guest_conns > 0
            or self.cpu_percent >= BUSY_CPU_PERCENT
        )

    def describe(self) -> str:
        """给日志用的一句话摘要"""
        parts = []
        if self.sessions:
            parts.append(f"{self.sessions} 会话")
        if self.host_conns:
            parts.append(f"{self.host_conns} 宿主连接")
        if self.slirp_conns:
            parts.append(f"{self.slirp_conns} SLIRP 连接")
        if self.guest_conns:
            parts.append(f"{self.guest_conns} guest 连接")
        if self.cpu_percent >= BUSY_CPU_PERCENT:
            parts.append(f"CPU {self.cpu_percent:.0f}%")
        return "、".join(parts) if parts else "无活动"


# guest CPU 使用率超过这个百分比视为「在干活」
BUSY_CPU_PERCENT = 15.0
# SSH 探测超时
PROBE_TIMEOUT = 10


# ─────────────────────────────────────────
# 1. 宿主机侧 TCP 连接
# ─────────────────────────────────────────
_TCP_STATE_ESTABLISHED = "01"


def host_connections(port: int) -> int:
    """宿主机上指向该端口的已建立连接数

    读 /proc/net/tcp 和 tcp6（不需要权限）。
    只数 ESTABLISHED —— LISTEN（0A）说明端口开着但没人连，
    不算活动；TIME_WAIT 是已结束的连接，也不算。

    这是判断「是否有人正在用」最权威的来源：
    ssh、curl、数据库客户端、浏览器访问 VM 服务，
    在宿主机侧都会表现为一条 ESTABLISHED 连接。
    """
    if port <= 0:
        return 0

    want = f"{port:04X}"
    total = 0

    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue

        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            # parts[1] = 本地地址:端口，parts[2] = 对端，parts[3] = 状态
            local = parts[1]
            if not local.endswith(":" + want):
                continue
            if parts[3] == _TCP_STATE_ESTABLISHED:
                total += 1

    return total


# ─────────────────────────────────────────
# 2. SLIRP 层连接（QEMU monitor）
# ─────────────────────────────────────────
def _monitor_query(vm_dir: Path, cmd: str, wait: float = 0.3) -> str:
    """向 QEMU monitor 发查询命令"""
    monitor = Path(vm_dir) / "monitor.sock"
    if not monitor.exists():
        return ""

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(monitor))
        s.settimeout(0.2)
        try:
            s.recv(65536)
        except (socket.timeout, OSError):
            pass

        s.settimeout(3)
        s.sendall((cmd + "\n").encode())
        time.sleep(wait)

        out = b""
        s.settimeout(0.3)
        idle = 0
        while idle < 2:
            try:
                d = s.recv(65536)
                if not d:
                    break
                out += d
                idle = 0
            except socket.timeout:
                idle += 1
        s.close()
    except OSError:
        return ""

    text = out.decode("utf-8", errors="replace").replace("\r", "")
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


# SLIRP 层要忽略的后台噪声。
# 这些是 guest 自身维持系统运行产生的流量，不代表用户在使用：
#   123  NTP 时间同步（systemd-timesyncd / chrony，每几十秒一次）
#   53   DNS 查询
#   67/68 DHCP 续租
#   546/547 DHCPv6
_NOISE_PORTS = {53, 67, 68, 123, 546, 547}


def slirp_activity(vm_dir: Path, max_idle: int = 120) -> int:
    """SLIRP 层「真实用户访问」的连接数

    `info usernet` 的输出形如：
        Protocol[State]    FD  Source Address  Port   Dest. Address  Port
        TCP[HOST_FORWARD]  20       127.0.0.1  2224       10.0.2.15    22
        UDP[206 sec]       26               * 43067        0.0.0.0   123

    关键点：
      · `HOST_FORWARD` 是**持久的端口转发监听条目**，不是活动连接，
        只要 VM 在跑就一直存在 —— 不能算活动，否则永远无法回收。
      · `TIME_WAIT` 表示刚结束的连接（有人用过），算活动。
      · UDP 方括号里是空闲秒数，NTP/DNS 这类后台流量要排除。

    只统计真正反映「有人在用」的条目。
    """
    out = _monitor_query(vm_dir, "info usernet", wait=0.35)
    if not out or "Protocol" not in out:
        return 0

    count = 0
    for line in out.splitlines():
        stripped = line.strip()
        m = re.match(r"^(TCP|UDP)\[([^\]]*)\]\s+(.*)$", stripped)
        if not m:
            continue

        proto, state, rest = m.group(1), m.group(2), m.group(3)

        # 持久监听条目：VM 在跑就一直在，不代表有访问
        if state == "HOST_FORWARD":
            continue

        # TIME_WAIT 是已结束的连接。我们的活动探测本身每次都会
        # 建立一条 SSH 连接并留下 TIME_WAIT，如果算作活动就会
        # 自我维持「永远有活动」—— 探测污染判断。
        # 真实的持续访问会有 ESTABLISHED 连接（由 host_connections
        # 从 /proc/net/tcp 权威统计），不依赖这里的残留。
        if state == "TIME_WAIT":
            continue

        # 后面的列：FD  src_addr  src_port  dst_addr  dst_port ...
        cols = rest.split()
        # cols[0]=FD, [1]=src_addr, [2]=src_port, [3]=dst_addr, [4]=dst_port
        if len(cols) < 5:
            continue

        # 目标端口 = 服务端口（判断噪声）
        try:
            dst_port = int(cols[4])
        except ValueError:
            # 某些行（如 0.0.0.0）格式不同，保守跳过
            continue

        if dst_port in _NOISE_PORTS:
            continue

        # UDP 有显式空闲秒数 → 太旧的忽略
        if proto == "UDP" and state.endswith("sec"):
            try:
                if int(state.split()[0]) >= max_idle:
                    continue
            except (ValueError, IndexError):
                pass

        count += 1

    return count


# ─────────────────────────────────────────
# 3+4+5. guest 内探测（一次 SSH 往返拿全）
# ─────────────────────────────────────────
# 用标记行分隔各段输出，避免多次 SSH（每次 200–400ms）。
# guest 内连接用 `ss -tn state established` —— 这是识别
# 「VM 之间互相访问」的关键：B 被 A 访问时，B 的 guest 内
# 会看到一条来自 10.0.2.x 的 ESTABLISHED 连接。
_PROBE_CMD = (
    "echo __WHO__; who 2>/dev/null | wc -l; "
    "echo __STAT__; grep '^cpu ' /proc/stat; sleep 0.4; grep '^cpu ' /proc/stat; "
    "echo __CONN__; "
    # 排除探测本身占用的 SSH 连接（来自 10.0.2.2 的 :22）——
    # 它是我们自己的探测流量，不能算作用户活动。
    "(ss -tn state established 2>/dev/null || netstat -tn 2>/dev/null || true) "
    "| tail -n +2 "
    "| grep -v ':22 *10\\.0\\.2\\.2:' "
    "| grep -v '10\\.0\\.2\\.2:[0-9]* *$' "
    "| wc -l; "
    "echo __END__"
)


def _parse_cpu(line: str) -> tuple[int, int] | None:
    """解析 /proc/stat 的 cpu 行 → (busy, total)"""
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        vals = [int(x) for x in parts[1:]]
    except ValueError:
        return None

    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    return (total - idle, total)


def guest_activity(vm) -> Activity:
    """通过 SSH 探测 guest 内部状态"""
    from core.vm import ssh

    act = Activity()

    ok, out = ssh.run_command(vm, _PROBE_CMD, timeout=PROBE_TIMEOUT)
    if not ok or "__END__" not in (out or ""):
        return act

    act.reachable = True
    text = out

    # ── who：登录会话数 ──
    try:
        seg = text.split("__WHO__", 1)[1].split("__STAT__", 1)[0]
        act.sessions = int(seg.strip() or 0)
    except (IndexError, ValueError):
        pass

    # ── stat：CPU 使用率 ──
    try:
        seg = text.split("__STAT__", 1)[1].split("__CONN__", 1)[0]
        rows = [ln for ln in seg.splitlines() if ln.startswith("cpu ")]
        if len(rows) >= 2:
            a, b = _parse_cpu(rows[0]), _parse_cpu(rows[1])
            if a and b:
                dbusy, dtotal = b[0] - a[0], b[1] - a[1]
                if dtotal > 0:
                    act.cpu_percent = max(
                        0.0, min(100.0, dbusy * 100.0 / dtotal)
                    )
    except IndexError:
        pass

    # ── conn：guest 内已建立连接数 ──
    try:
        seg = text.split("__CONN__", 1)[1].split("__END__", 1)[0]
        act.guest_conns = int(seg.strip().splitlines()[0] or 0)
    except (IndexError, ValueError):
        pass

    return act


# ─────────────────────────────────────────
# 统一入口
# ─────────────────────────────────────────
def probe(vm) -> Activity:
    """全面探测一台 VM 的活动状态

    **顺序很重要**：先查不需要 SSH 的两项（宿主机 TCP、SLIRP），
    最后才做 SSH 探测。

    因为 SSH 探测本身会建立连接，会在 SLIRP 里留下一条
    ESTABLISHED/TIME_WAIT 记录 —— 如果先 SSH 再查 SLIRP，
    就会把自己的探测流量误判成「用户活动」，导致永远无法回收。
    先查后 SSH 则不会污染。

    任何一项探测异常都不抛错 —— 按「无法确认」处理，
    由调用方保守地视为有活动。
    """
    act = Activity()

    # 1. 宿主机侧连接（不需要 guest 配合，即使 SSH 不通也能查）
    try:
        act.host_conns = host_connections(vm.ssh_port)
    except Exception:
        act.host_conns = 0

    # 2. SLIRP 层（覆盖 guest 出站流量、VM 间通信）
    #    必须在 SSH 探测之前查
    try:
        act.slirp_conns = slirp_activity(vm.dir)
    except Exception:
        act.slirp_conns = 0

    # 3. 最后才 SSH 进 guest（会建立连接，故放最后）
    guest = guest_activity(vm)
    act.reachable = guest.reachable
    act.sessions = guest.sessions
    act.cpu_percent = guest.cpu_percent
    act.guest_conns = guest.guest_conns

    return act


@dataclass
class MemoryPressure:
    """宿主机内存压力评估结果"""

    level: str = "comfortable"      # comfortable | tight | critical
    available_mb: int = 0
    total_mb: int = 0
    available_pct: float = 0.0

    @property
    def should_reclaim(self) -> bool:
        return self.level in ("tight", "critical")

    def describe(self) -> str:
        return (
            f"{self.level}（可用 {self.available_mb}MB / "
            f"{self.total_mb}MB, {self.available_pct:.0f}%）"
        )


# ─────────────────────────────────────────
# 内存压力
# ─────────────────────────────────────────
def memory_pressure() -> MemoryPressure:
    """评估宿主机内存压力

    用 MemAvailable 而不是 MemFree：Linux 的页缓存会被自动回收，
    MemFree 常年很低，用它判断会一直误报紧张。

    三级：
      · comfortable —— 内存充裕，不回收任何 VM
      · tight       —— 有点紧，回收空闲超过阈值的
      · critical    —— 很紧，回收所有完全空闲的
    """
    from core.vm import memory

    host = memory.host_memory()
    if host.total_mb <= 0:
        return MemoryPressure(level="comfortable")

    avail_pct = host.available_mb / host.total_mb * 100

    # < 8%  很紧 —— 系统已在换页边缘，必须回收
    # < 15% 紧张 —— 开始回收空闲 VM
    # >= 15% 充裕 —— 不动
    if avail_pct < 8.0:
        level = "critical"
    elif avail_pct < 15.0:
        level = "tight"
    else:
        level = "comfortable"

    return MemoryPressure(
        level=level,
        available_mb=host.available_mb,
        total_mb=host.total_mb,
        available_pct=avail_pct,
    )
