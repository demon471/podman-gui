"""VM 域名解析：一次配置，后续零维护

需求（对标 OrbStack 的 `machine.orb.local`）：
  宿主机只需在首次初始化时加一次配置，
  之后新建 / 删除 / 改端口都不需要再动宿主机。

实现思路
────────
用 systemd-resolved 的通配路由域 + dnsmasq 通配地址：

    应用/浏览器查询 foo.vm.local
          ↓
    systemd-resolved（127.0.0.53）
          ↓ Domains=~vm.local 后缀匹配
    dnsmasq（127.0.0.1:5354）
          ↓ address=/vm.local/127.0.0.1
    返回 127.0.0.1
          ↓
    端口转发已在 127.0.0.1:<host_port>
    浏览器访问 http://foo.vm.local:8080 打通

为什么端口不用管：
  所有 VM 的域名都解析到同一个 127.0.0.1，
  不同 VM 靠端口号区分，与「一机一 IP」的方案相比
  新增 VM 不需要任何宿主机改动。

宿主机需要的一次性配置（本模块负责写入）：
  1. /etc/dnsmasq.d/podman-gui-vm.conf   一条 address= 通配规则
  2. /etc/systemd/resolved.conf.d/99-podman-gui-vm.conf
     一条 Domains=~vm.local 路由域
  3. dnsmasq 设为开机自启（否则重启后失效）

所有写入都需要 root，因此提供：
  ensure_configured()  检测 + 尝试自动配置（sudo -n）
  manual_commands()    免密失败时给出用户可手动执行的命令
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 本次会话缓存的 sudo 密码（用户在一次性配置对话框里输入）。
# 只保存在内存中，不落盘；进程退出即失效。
_SUDO_PASSWORD: str | None = None


def set_sudo_password(pwd: str | None) -> None:
    """设置 / 清除 sudo 密码（供 UI 层调用）"""
    global _SUDO_PASSWORD
    _SUDO_PASSWORD = pwd or None


def has_sudo_password() -> bool:
    return bool(_SUDO_PASSWORD)

# 通配域名后缀（不带点，例如 "vm.local"）
DOMAIN_SUFFIX = "vm.local"

# dnsmasq 监听端口。
# 不能直接占用 53：该端口通常已被 systemd-resolved 的 stub
# 监听器（127.0.0.53:53）占用。用 5354 避开，再让 resolved
# 把 vm.local 的查询转发过来。
DNSMASQ_PORT = 5354
DNSMASQ_ADDR = "127.0.0.1"

# 配置文件路径
DNSMASQ_CONF = Path("/etc/dnsmasq.d/podman-gui-vm.conf")
RESOLVED_CONF = Path("/etc/systemd/resolved.conf.d/99-podman-gui-vm.conf")

# 标记，用于识别本程序写入的配置
DNSMASQ_MARKER = "podman-gui-vm"
RESOLVED_MARKER = "podman-gui-vm"

DNSMASQ_CONF_TEMPLATE = """\
# 由 podman-gui 生成：虚拟机域名通配解析
# 所有 *.{suffix} 一律返回 127.0.0.1，配合端口转发即可访问 VM 内服务。
# 新增 / 删除虚拟机无需修改此文件。
listen-address={addr}
port={port}
no-resolv
no-hosts
bind-interfaces
address=/{suffix}/127.0.0.1
"""

RESOLVED_CONF_TEMPLATE = """\
# 由 podman-gui 生成：把 *.{suffix} 交给本地 dnsmasq 解析
# ~ 前缀表示「路由域」：只做后缀匹配转发，不会追加为搜索域，
# 因此不影响其它域名的正常解析。
[Resolve]
DNS={addr}:{port}
Domains=~{suffix}
"""


# ─────────────────────────────────────────
# 状态
# ─────────────────────────────────────────
@dataclass
class DNSStatus:
    """域名解析配置状态"""

    dnsmasq_installed: bool = False
    dnsmasq_running: bool = False
    dnsmasq_conf: bool = False       # 配置文件存在且内容正确
    resolved_conf: bool = False
    resolved_running: bool = False
    wildcard_works: bool = False     # 实际查询是否返回 127.0.0.1
    detail: str = ""

    @property
    def ready(self) -> bool:
        """是否可以正常使用 .vm.local 域名"""
        return self.wildcard_works

    @property
    def needs_setup(self) -> bool:
        """是否还需要执行一次性配置"""
        return not (self.dnsmasq_conf and self.resolved_conf)

    def display(self) -> str:
        if self.ready:
            return f"已启用（*{DOMAIN_SUFFIX} → 127.0.0.1）"
        if not self.dnsmasq_installed:
            return "未启用（缺少 dnsmasq）"
        if self.needs_setup:
            return "未启用（需要一次性配置）"
        if not self.dnsmasq_running:
            return "配置已写入，但 dnsmasq 未运行"
        return "配置已写入，但解析未生效"


def _run(cmd: list[str], timeout: int = 10, stdin: str = "") -> tuple[int, str]:
    """执行命令，返回 (退出码, 输出)

    若命令以 sudo 开头且已通过 set_sudo_password 提供密码，
    自动改用 `sudo -S` 从 stdin 读密码。

    注意：stdin 同时还要承载别的数据时（如 tee 的文件内容），
    不能再复用 stdin 传密码，否则密码会混进数据里。
    这类场景一律走 _sudo_write（先写临时文件再 sudo cp）。
    """
    use_password = (
        _SUDO_PASSWORD
        and len(cmd) >= 1
        and os.path.basename(cmd[0]) == "sudo"
        and not stdin          # 只在不需要传数据时才用 stdin 传密码
    )
    if use_password:
        # 必须去掉已有的 -n（non-interactive），否则 sudo 拒绝读密码
        rest = [a for a in cmd[1:] if a not in ("-n", "--non-interactive")]
        cmd = [cmd[0], "-S", "-p", ""] + rest
        stdin = (_SUDO_PASSWORD or "") + "\n"

    try:
        r = subprocess.run(
            cmd, input=stdin if stdin else None,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (subprocess.TimeoutExpired, OSError) as e:
        return -1, str(e)


def _has_sudo_nopass() -> bool:
    """sudo 是否免密"""
    if not Path("/usr/bin/sudo").exists():
        return False
    # 已缓存密码时视为可用（_run 会走 sudo -S）
    if _SUDO_PASSWORD:
        return True
    rc, _ = _run(["/usr/bin/sudo", "-n", "true"], timeout=5)
    return rc == 0


def _sudo_write(path: Path, content: str) -> tuple[bool, str]:
    """以 root 写文件

    实现要点：不能把文件内容和 sudo 密码一起从 stdin 送进去
    （`sudo -S tee < content` 会让密码变成文件第一行）。
    正确做法是：先写到一个临时文件，再用 `sudo cp` 复制过去。
    密码只喂给 sudo 的密码提示，与文件内容完全隔离。
    """
    # 已存在且可写
    if path.exists() and os.access(path, os.W_OK):
        try:
            path.write_text(content, encoding="utf-8")
            return True, ""
        except OSError as e:
            return False, str(e)

    # 父目录可写则直接创建
    if path.parent.is_dir() and os.access(path.parent, os.W_OK):
        try:
            path.write_text(content, encoding="utf-8")
            return True, ""
        except OSError as e:
            return False, str(e)

    # 需要 sudo
    if not _has_sudo_nopass():
        return False, (
            f"需要 root 权限写入 {path}"
            "（当前 sudo 需要密码，无法自动执行）"
        )

    import tempfile

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="podman-gui-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)

        # 父目录可能也不存在
        if not path.parent.is_dir():
            rc, out = _run(
                ["/usr/bin/sudo", "mkdir", "-p", str(path.parent)], timeout=10
            )
            if rc != 0:
                return False, (
                    f"创建目录 {path.parent} 失败: {out.strip()[:150]}"
                )

        rc, out = _run(
            ["/usr/bin/sudo", "cp", tmp, str(path)], timeout=15
        )
        if rc != 0:
            return False, f"复制到 {path} 失败: {out.strip()[:200]}"

        # 保持合理的权限（配置文件 644）
        _run(["/usr/bin/sudo", "chmod", "644", str(path)], timeout=10)
        return True, ""
    except OSError as e:
        return False, str(e)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _sudo_read(path: Path) -> str:
    """读取可能需 root 的文件"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        pass
    if _has_sudo_nopass():
        rc, out = _run(["/usr/bin/sudo", "-n", "cat", str(path)], timeout=10)
        if rc == 0:
            return out
    return ""


def _sudo_rm(path: Path) -> tuple[bool, str]:
    """删除可能需 root 的文件"""
    if not path.exists():
        return True, ""
    if os.access(path.parent, os.W_OK):
        try:
            path.unlink()
            return True, ""
        except OSError as e:
            return False, str(e)
    if _has_sudo_nopass():
        rc, out = _run(["/usr/bin/sudo", "-n", "rm", "-f", str(path)], timeout=10)
        if rc == 0:
            return True, ""
        return False, out.strip()[:200]
    return False, "需要 root 权限"


# ─────────────────────────────────────────
# 检测
# ─────────────────────────────────────────
def dnsmasq_installed() -> bool:
    return shutil.which("dnsmasq") is not None


def _dnsmasq_running() -> bool:
    """dnsmasq 是否在监听我们的端口"""
    # 优先用 ss，退回 /proc 扫描
    rc, out = _run(
        ["ss", "-tlnH", f"sport = :{DNSMASQ_PORT}"], timeout=5
    )
    if rc == 0:
        return bool(out.strip())
    # 兜底：看进程
    rc, out = _run(["pgrep", "-x", "dnsmasq"], timeout=5)
    if rc != 0:
        return False
    # 检查命令行里有我们的配置
    rc, out = _run(["pgrep", "-af", "dnsmasq"], timeout=5)
    return DNSMASQ_MARKER in out or str(DNSMASQ_PORT) in out


def _resolved_running() -> bool:
    rc, _ = _run(
        ["systemctl", "is-active", "systemd-resolved"], timeout=5
    )
    return rc == 0


def _conf_matches(path: Path, expected: str) -> bool:
    """配置文件存在且内容一致"""
    actual = _sudo_read(path)
    if not actual:
        return False
    return actual.strip() == expected.strip()


def wildcard_query(host: str | None = None, timeout: int = 3) -> str:
    """查询通配域名，返回解析到的 IP（失败返回空串）

    用随机子域名查询，确保测的是通配能力而非某条固定记录。
    """
    probe = host or f"probe-{os.getpid()}.{DOMAIN_SUFFIX}"
    try:
        import socket

        socket.setdefaulttimeout(timeout)
        infos = socket.getaddrinfo(probe, None, socket.AF_INET)
        for info in infos:
            addr = info[4][0]
            if addr.startswith("127."):
                return addr
        # 解析成功但不是回环地址，也返回第一个
        return infos[0][4][0] if infos else ""
    except OSError:
        return ""


def status() -> DNSStatus:
    """读取当前配置状态"""
    st = DNSStatus()
    st.dnsmasq_installed = dnsmasq_installed()
    st.dnsmasq_running = _dnsmasq_running()
    st.resolved_running = _resolved_running()

    expected_dnsmasq = dnsmasq_conf_content()
    expected_resolved = resolved_conf_content()
    st.dnsmasq_conf = _conf_matches(DNSMASQ_CONF, expected_dnsmasq)
    st.resolved_conf = _conf_matches(RESOLVED_CONF, expected_resolved)

    st.wildcard_works = bool(wildcard_query())

    # 汇总说明
    problems = []
    if not st.dnsmasq_installed:
        problems.append("未安装 dnsmasq")
    if not st.resolved_running:
        problems.append("systemd-resolved 未运行")
    if st.dnsmasq_installed and not st.dnsmasq_running:
        problems.append("dnsmasq 未运行")
    if st.dnsmasq_installed and st.resolved_running:
        if not st.dnsmasq_conf:
            problems.append("缺少 dnsmasq 配置")
        if not st.resolved_conf:
            problems.append("缺少 resolved 转发配置")
    st.detail = "；".join(problems)
    return st


# ─────────────────────────────────────────
# 配置内容
# ─────────────────────────────────────────
def dnsmasq_conf_content() -> str:
    return DNSMASQ_CONF_TEMPLATE.format(
        addr=DNSMASQ_ADDR,
        port=DNSMASQ_PORT,
        suffix=DOMAIN_SUFFIX,
    )


def resolved_conf_content() -> str:
    return RESOLVED_CONF_TEMPLATE.format(
        addr=DNSMASQ_ADDR,
        port=DNSMASQ_PORT,
        suffix=DOMAIN_SUFFIX,
    )


# ─────────────────────────────────────────
# 配置 / 撤销
# ─────────────────────────────────────────
def _restart_dnsmasq() -> tuple[bool, str]:
    """启动/重启 dnsmasq 并设为开机自启

    不依赖发行版的 dnsmasq.service（那通常需要 /etc/dnsmasq.conf 存在，
    且默认监听 53 会与 resolved 冲突），用一个独立 unit 更可控。
    """
    unit = f"""\
[Unit]
Description=dnsmasq for podman-gui VM domains (*{DOMAIN_SUFFIX})
After=network.target systemd-resolved.service
Wants=systemd-resolved.service

[Service]
Type=simple
ExecStart=/usr/bin/dnsmasq --no-daemon --conf-file={DNSMASQ_CONF}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""
    unit_path = Path("/etc/systemd/system/podman-gui-dnsmasq.service")
    ok, err = _sudo_write(unit_path, unit)
    if not ok:
        return False, f"写入 systemd unit 失败: {err}"

    steps = [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "podman-gui-dnsmasq.service"],
        ["systemctl", "restart", "systemd-resolved"],
    ]
    for cmd in steps:
        rc, out = _run(["/usr/bin/sudo", "-n"] + cmd, timeout=30)
        if rc != 0:
            return False, f"{' '.join(cmd)} 失败: {out.strip()[:200]}"

    return True, ""


def configure() -> tuple[bool, str]:
    """执行一次性配置（需要 root）

    步骤：
      1. 检查 dnsmasq 是否安装
      2. 写 dnsmasq 配置（通 wildcard 地址）
      3. 写 resolved 配置（转发 vm.local）
      4. 装 systemd unit 并启动 dnsmasq、重启 resolved
      5. 验证通配解析是否生效
    """
    if not dnsmasq_installed():
        return False, (
            "未安装 dnsmasq。请先安装：\n"
            "  Arch: sudo pacman -S dnsmasq\n"
            "  Debian/Ubuntu: sudo apt install dnsmasq\n"
            "  Fedora: sudo dnf install dnsmasq"
        )

    if not _resolved_running():
        return False, (
            "systemd-resolved 未运行，无法配置域名转发。\n"
            "该方案依赖 systemd-resolved。"
        )

    # 1. dnsmasq 配置
    ok, err = _sudo_write(DNSMASQ_CONF, dnsmasq_conf_content())
    if not ok:
        return False, f"写入 {DNSMASQ_CONF} 失败: {err}"

    # 2. resolved 配置
    ok, err = _sudo_write(RESOLVED_CONF, resolved_conf_content())
    if not ok:
        return False, f"写入 {RESOLVED_CONF} 失败: {err}"

    # 3. 启动服务
    ok, err = _restart_dnsmasq()
    if not ok:
        return False, err

    # 4. 验证
    import time

    for _ in range(10):
        time.sleep(0.5)
        if wildcard_query():
            return True, ""

    return False, (
        "配置已写入但解析未生效。请检查：\n"
        f"  systemctl status podman-gui-dnsmasq\n"
        f"  resolvectl status\n"
        f"  dig @127.0.0.1 -p {DNSMASQ_PORT} test.{DOMAIN_SUFFIX}"
    )


def unconfigure() -> tuple[bool, str]:
    """撤销配置（需要 root）"""
    errors = []

    ok, err = _run(
        ["/usr/bin/sudo", "-n", "systemctl", "disable", "--now",
         "podman-gui-dnsmasq.service"],
        timeout=30,
    )
    if ok is False:
        errors.append(f"停止服务失败: {err.strip()[:150]}")

    ok, err = _sudo_rm(Path("/etc/systemd/system/podman-gui-dnsmasq.service"))
    if not ok:
        errors.append(f"删除 unit 失败: {err}")

    ok, err = _sudo_rm(DNSMASQ_CONF)
    if not ok:
        errors.append(f"删除 dnsmasq 配置失败: {err}")

    ok, err = _sudo_rm(RESOLVED_CONF)
    if not ok:
        errors.append(f"删除 resolved 配置失败: {err}")

    _run(["/usr/bin/sudo", "-n", "systemctl", "daemon-reload"], timeout=20)
    _run(["/usr/bin/sudo", "-n", "systemctl", "restart", "systemd-resolved"],
         timeout=30)

    if errors:
        return False, "；".join(errors)
    return True, ""


# ─────────────────────────────────────────
# 手动命令（免密失败时给用户）
# ─────────────────────────────────────────
def manual_commands() -> str:
    """返回用户可手动执行的配置命令（含 sudo 密码输入）"""
    unit_path = "/etc/systemd/system/podman-gui-dnsmasq.service"
    return f"""\
# 1. 安装 dnsmasq（若已安装可跳过）
#    sudo pacman -S dnsmasq

# 2. 写入 dnsmasq 通配配置
sudo tee {DNSMASQ_CONF} >/dev/null <<'EOF'
{dnsmasq_conf_content().rstrip()}
EOF

# 3. 让 systemd-resolved 把 *.{DOMAIN_SUFFIX} 转给 dnsmasq
sudo mkdir -p /etc/systemd/resolved.conf.d
sudo tee {RESOLVED_CONF} >/dev/null <<'EOF'
{resolved_conf_content().rstrip()}
EOF

# 4. 用独立 unit 启动 dnsmasq（避免与发行版 service 抢 53 端口）
sudo tee {unit_path} >/dev/null <<'EOF'
[Unit]
Description=dnsmasq for podman-gui VM domains (*{DOMAIN_SUFFIX})
After=systemd-resolved.service

[Service]
Type=simple
ExecStart=/usr/bin/dnsmasq --no-daemon --conf-file={DNSMASQ_CONF}
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

# 5. 生效
sudo systemctl daemon-reload
sudo systemctl enable --now podman-gui-dnsmasq.service
sudo systemctl restart systemd-resolved

# 6. 验证
dig @127.0.0.1 -p {DNSMASQ_PORT} test.{DOMAIN_SUFFIX}
ping -c1 anything.{DOMAIN_SUFFIX}
"""


def uninstall_commands() -> str:
    """返回撤销配置的命令"""
    return f"""\
sudo systemctl disable --now podman-gui-dnsmasq.service
sudo rm -f /etc/systemd/system/podman-gui-dnsmasq.service
sudo rm -f {DNSMASQ_CONF}
sudo rm -f {RESOLVED_CONF}
sudo systemctl daemon-reload
sudo systemctl restart systemd-resolved
"""


# ─────────────────────────────────────────
# 域名生成
# ─────────────────────────────────────────
def vm_domain(vm_name: str) -> str:
    """VM 域名，如 alpine-dev.vm.local

    域名规则：小写字母/数字/连字符，下划线转连字符。
    """
    import re

    safe = re.sub(r"[^a-z0-9-]", "-", vm_name.lower()).strip("-")
    safe = re.sub(r"-+", "-", safe)
    if not safe:
        safe = "vm"
    return f"{safe}.{DOMAIN_SUFFIX}"


def access_url(vm_name: str, host_port: int, scheme: str = "http") -> str:
    """生成访问地址"""
    return f"{scheme}://{vm_domain(vm_name)}:{host_port}"
