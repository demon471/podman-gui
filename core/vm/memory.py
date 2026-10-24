"""VM 内存监控与优化

职责：
  · 读取 VM 进程的真实内存占用（RSS / VSZ / Swap）
  · 通过 QEMU monitor 控制内存气球（动态回收）
  · KSM（内核同页合并）状态检测与启用
  · 内存压力评估与建议

实测数据（Alpine 512MB）：
  guest 配置 512MB → 宿主机 RSS 298MB（58%）
  优化后（气球 + 禁显卡）→ 约 140MB
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


# QEMU 进程的固定内存开销（MB）：QEMU 基线约 30MB + OVMF 固件约 50MB。
# 计算 guest 真实占用、内存压力、占用效率时需剔除，否则小内存 VM
# 会显示超过 100% 的读数，造成误判。
QEMU_OVERHEAD_MB = 90


# ─────────────────────────────────────────
# 进程内存
# ─────────────────────────────────────────
@dataclass
class MemUsage:
    """VM 进程内存占用"""

    rss_mb: float = 0.0        # 实际物理内存
    vsz_mb: float = 0.0        # 虚拟地址空间
    swap_mb: float = 0.0       # 被换出
    guest_mb: int = 0          # guest 配置内存
    peak_mb: float = 0.0       # 峰值

    @property
    def efficiency(self) -> float:
        """(RSS - QEMU 固定开销) / guest 配置（越低越好）

        QEMU 进程 RSS 含约 90MB 固定开销（QEMU 基线 + UEFI 固件），
        这部分不受 balloon 影响。不剔除的话，小内存 VM 会算出 115%
        这类超过 100% 的读数，用户无法判断是否真的紧张。
        """
        if self.guest_mb <= 0:
            return 0.0
        usable = max(0.0, self.rss_mb - QEMU_OVERHEAD_MB)
        return usable / self.guest_mb * 100

    def display(self) -> str:
        return (
            f"{self.rss_mb:.0f} MB / {self.guest_mb} MB "
            f"({self.efficiency:.0f}%)"
        )


def read_process_mem(pid: int, guest_mb: int = 0) -> MemUsage:
    """从 /proc/PID/status 读取内存"""
    usage = MemUsage(guest_mb=guest_mb)

    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return usage

    for line in text.splitlines():
        if line.startswith("VmRSS:"):
            usage.rss_mb = int(line.split()[1]) / 1024
        elif line.startswith("VmSize:"):
            usage.vsz_mb = int(line.split()[1]) / 1024
        elif line.startswith("VmSwap:"):
            usage.swap_mb = int(line.split()[1]) / 1024
        elif line.startswith("VmPeak:"):
            usage.peak_mb = int(line.split()[1]) / 1024

    return usage


def vm_mem_usage(vm) -> MemUsage:
    """获取某个 VM 的内存占用"""
    from core.vm import qemu

    pid = qemu.read_pid(vm.dir)
    if pid is None:
        return MemUsage(guest_mb=vm.spec.memory_mb)
    return read_process_mem(pid, vm.spec.memory_mb)


def total_vm_memory() -> tuple[float, int]:
    """所有 VM 的总内存占用 (MB, 数量)"""
    from core.vm import manager

    total = 0.0
    count = 0
    for vm in manager.load_all():
        if not vm.running:
            continue
        usage = vm_mem_usage(vm)
        total += usage.rss_mb
        count += 1
    return total, count


# ─────────────────────────────────────────
# 宿主机内存
# ─────────────────────────────────────────
@dataclass
class HostMemory:
    """宿主机内存状况"""

    total_mb: int = 0
    available_mb: int = 0
    used_mb: int = 0
    swap_total_mb: int = 0
    swap_used_mb: int = 0

    @property
    def used_percent(self) -> float:
        if self.total_mb <= 0:
            return 0.0
        return self.used_mb / self.total_mb * 100

    @property
    def can_allocate(self) -> int:
        """还能分配给 VM 的内存（MB，保留 1GB 余量）"""
        return max(0, self.available_mb - 1024)


def host_memory() -> HostMemory:
    """读取宿主机内存"""
    mem = HostMemory()
    try:
        text = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return mem

    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        key = parts[0].rstrip(":")
        try:
            val = int(parts[1]) / 1024       # KB → MB
        except ValueError:
            continue

        if key == "MemTotal":
            mem.total_mb = int(val)
        elif key == "MemAvailable":
            mem.available_mb = int(val)
        elif key == "SwapTotal":
            mem.swap_total_mb = int(val)
        elif key == "SwapFree":
            mem.swap_used_mb = int(mem.swap_total_mb - val)

    mem.used_mb = mem.total_mb - mem.available_mb
    return mem


def check_memory_before_start(memory_mb: int) -> tuple[bool, str]:
    """启动 VM 前的内存检查

    返回 (是否允许, 提示信息)
    """
    host = host_memory()

    if host.available_mb >= memory_mb + 512:
        return True, ""

    # 不足
    if host.swap_total_mb > 0:
        return True, (
            f"内存偏紧：可用 {host.available_mb} MB，"
            f"申请 {memory_mb} MB。将使用 swap（可能变慢）"
        )

    return False, (
        f"内存不足：可用 {host.available_mb} MB，"
        f"申请 {memory_mb} MB。\n"
        f"请关闭其他程序或降低 VM 内存配置。"
    )


# ─────────────────────────────────────────
# 内存气球（动态回收）
# ─────────────────────────────────────────
def balloon_set(vm, target_mb: int) -> tuple[bool, str]:
    """通过 QEMU monitor 设置气球目标

    target_mb 是希望 guest 保留的内存量。
    例如 guest 配置 1024MB，设为 512 表示让 guest 归还 512MB。
    """
    monitor = vm.dir / "monitor.sock"
    if not monitor.exists():
        return False, "monitor socket 不存在（VM 可能未运行）"

    target_mb = max(64, min(target_mb, vm.spec.memory_mb))

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(monitor))
        # 读取欢迎信息
        s.recv(1024)
        # 发送 balloon 命令
        s.sendall(f"balloon {target_mb}\n".encode())
        import time
        time.sleep(0.2)
        resp = s.recv(1024).decode("utf-8", errors="ignore")
        s.close()
    except (OSError, socket.timeout) as e:
        return False, str(e)

    if "error" in resp.lower():
        return False, resp.strip()

    return True, f"气球目标设为 {target_mb} MB"


def balloon_reclaim(vm, keep_mb: int = 256) -> tuple[bool, str]:
    """回收空闲内存（把 guest 压到 keep_mb）"""
    return balloon_set(vm, keep_mb)


def balloon_restore(vm) -> tuple[bool, str]:
    """恢复到配置的完整内存"""
    return balloon_set(vm, vm.spec.memory_mb)


# ─────────────────────────────────────────
# KSM（内核同页合并）
# ─────────────────────────────────────────
KSM_RUN = Path("/sys/kernel/mm/ksm/run")
KSM_PAGES_SHARED = Path("/sys/kernel/mm/ksm/pages_shared")
KSM_PAGES_SHARING = Path("/sys/kernel/mm/ksm/pages_sharing")
KSM_SLEEP = Path("/sys/kernel/mm/ksm/sleep_millisecs")


@dataclass
class KSMStatus:
    """KSM 状态"""

    enabled: bool = False
    pages_shared: int = 0
    pages_sharing: int = 0
    saved_mb: float = 0.0
    sleep_ms: int = 0

    def display(self) -> str:
        if not self.enabled:
            return "未启用"
        if self.pages_sharing == 0:
            return "已启用（暂无合并）"
        return f"已启用，已合并节省 {self.saved_mb:.0f} MB"


def ksm_status() -> KSMStatus:
    """读取 KSM 状态"""
    st = KSMStatus()

    try:
        st.enabled = KSM_RUN.read_text().strip() == "1"
    except OSError:
        return st

    try:
        st.pages_shared = int(KSM_PAGES_SHARED.read_text().strip())
        st.pages_sharing = int(KSM_PAGES_SHARING.read_text().strip())
        # pages_sharing * 4KB = 节省的字节数
        st.saved_mb = st.pages_sharing * 4096 / 1024 / 1024
    except (OSError, ValueError):
        pass

    try:
        st.sleep_ms = int(KSM_SLEEP.read_text().strip())
    except (OSError, ValueError):
        pass

    return st


def ksm_available() -> bool:
    """KSM 是否可用"""
    return KSM_RUN.exists()


def enable_ksm() -> tuple[bool, str]:
    """启用 KSM（需要 root）"""
    if not ksm_available():
        return False, "内核未启用 KSM 支持"

    if ksm_status().enabled:
        return True, "KSM 已启用"

    cmd = (
        "echo 1 > /sys/kernel/mm/ksm/run && "
        "echo 1000 > /sys/kernel/mm/ksm/sleep_millisecs"
    )

    # 尝试直接写（root）
    try:
        if os.access(KSM_RUN, os.W_OK):
            KSM_RUN.write_text("1")
            if KSM_SLEEP.exists():
                KSM_SLEEP.write_text("1000")
            return True, "KSM 已启用"
    except OSError:
        pass

    # 用 sudo -n
    try:
        r = subprocess.run(
            ["sudo", "-n", "sh", "-c", cmd],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return True, "KSM 已启用"
        return False, (r.stderr or "").strip()[:200]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def disable_ksm() -> tuple[bool, str]:
    """禁用 KSM"""
    try:
        if os.access(KSM_RUN, os.W_OK):
            KSM_RUN.write_text("0")
            return True, "KSM 已禁用"
    except OSError:
        pass

    try:
        r = subprocess.run(
            ["sudo", "-n", "sh", "-c", "echo 0 > /sys/kernel/mm/ksm/run"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return True, "KSM 已禁用"
        return False, (r.stderr or "").strip()[:200]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def ksm_service_installed() -> bool:
    """是否已安装 KSM 持久化服务"""
    return Path("/etc/systemd/system/ksm.service").exists() or \
           Path("/usr/lib/systemd/system/ksm.service").exists()


def install_ksm_service() -> tuple[bool, str]:
    """安装 KSM 开机自启服务（需要 sudo）"""
    unit = """[Unit]
Description=Kernel Samepage Merging (KSM)
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'echo 1 > /sys/kernel/mm/ksm/run'
ExecStart=/bin/sh -c 'echo 1000 > /sys/kernel/mm/ksm/sleep_millisecs'
ExecStop=/bin/sh -c 'echo 0 > /sys/kernel/mm/ksm/run'

[Install]
WantedBy=multi-user.target
"""

    try:
        # 写临时文件，再 sudo 安装
        tmp = Path("/tmp/ksm.service")
        tmp.write_text(unit, encoding="utf-8")

        cmd = (
            f"cp {tmp} /etc/systemd/system/ksm.service && "
            f"systemctl daemon-reload && "
            f"systemctl enable --now ksm.service"
        )
        r = subprocess.run(
            ["sudo", "-n", "sh", "-c", cmd],
            capture_output=True, text=True, timeout=30,
        )
        tmp.unlink(missing_ok=True)

        if r.returncode == 0:
            return True, "KSM 服务已安装并启用"
        return False, (r.stderr or "").strip()[:300]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


# ─────────────────────────────────────────
# 综合建议
# ─────────────────────────────────────────
def optimization_hints() -> list[str]:
    """生成内存优化建议"""
    hints = []
    host = host_memory()
    ksm = ksm_status()

    if host.used_percent > 75:
        hints.append(
            f"宿主机内存已用 {host.used_percent:.0f}%，"
            f"建议关闭一些程序或减少 VM 数量"
        )

    if not ksm.enabled and ksm_available():
        hints.append(
            "KSM 未启用。同时运行多个 VM 时启用可节省 30-40% 内存"
        )

    if ksm.enabled and ksm.saved_mb > 0:
        hints.append(f"KSM 已节省 {ksm.saved_mb:.0f} MB 内存")

    total_mb, count = total_vm_memory()
    if count >= 2 and total_mb > host.total_mb * 0.5:
        hints.append(
            f"{count} 个 VM 占用 {total_mb:.0f} MB，"
            f"超过宿主机内存一半"
        )

    return hints
