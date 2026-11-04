"""虚拟机共享网桥（让 VM 之间互通、宿主机可用内部 IP 访问）

为什么要这个
────────────
QEMU 默认的用户网络（SLIRP）有两个硬限制：
  · **VM 之间互相看不见** —— 每台 VM 一个独立 NAT，ping 不通
  · **宿主机无法用内部 IP 访问** —— 只能靠 hostfwd 端口转发，
    所以外部 SSH 工具只能填 127.0.0.1:222x，填内部 IP 必然失败

改成共享网桥后：
  · 所有 VM 挂在同一个二层网络上 → 互相能 ping
  · 宿主机在网桥上有地址（10.99.0.1）→ 直接 ssh 10.99.x.y 就行
  · dnsmasq 给 guest 发 DHCP，还能反解名字

权限
────
建网桥需要 root，但**只在初始化时一次**：
之后的 QEMU 通过 setuid 的 qemu-bridge-helper 挂网桥（普通用户即可），
需要把它加进 /etc/qemu/bridge.conf 的 allow 列表。

初始化走 pkexec 弹系统授权框，不需要用户开终端。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

BRIDGE = "pgbr0"
SUBNET = "10.99.0.0/16"
HOST_IP = "10.99.0.1"
PREFIX = 16
DHCP_START = "10.99.10.1"
DHCP_END = "10.99.250.254"

BRIDGE_CONF = Path("/etc/qemu/bridge.conf")
LEASES = Path("/var/lib/misc/dnsmasq-pgbr0.leases")
UNIT_NAME = "podman-gui-net.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME


# ─────────────────────────────────────────
# 状态查询（不需要 root）
# ─────────────────────────────────────────
def bridge_exists() -> bool:
    return Path(f"/sys/class/net/{BRIDGE}").exists()


def helper_available() -> bool:
    """qemu-bridge-helper 是否可用（setuid 才能免 root 挂网桥）"""
    for p in (
        "/usr/lib/qemu/qemu-bridge-helper",
        "/usr/libexec/qemu-bridge-helper",
    ):
        if Path(p).exists():
            return True
    return False


def bridge_allowed() -> bool:
    """网桥是否已加入 /etc/qemu/bridge.conf 白名单"""
    try:
        text = BRIDGE_CONF.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("allow"):
            name = s.split(None, 1)[1].strip() if len(s.split()) > 1 else ""
            if name in (BRIDGE, "all"):
                return True
    return False


def ready() -> bool:
    """是否已经可以直接给 QEMU 用（无需再提权）

    白名单文件可能因权限读不到，所以只在「能读到」时才要求它；
    读不到就只看网桥和 helper —— 真配错了 QEMU 启动会报错，
    那时再提示用户重新初始化。
    """
    if not (bridge_exists() and helper_available()):
        return False
    try:
        readable = os.access(BRIDGE_CONF, os.R_OK)
    except OSError:
        readable = False
    if readable:
        return bridge_allowed()
    return True


def has_pkexec() -> bool:
    return shutil.which("pkexec") is not None


# ─────────────────────────────────────────
# 初始化（一次性，需要 root）
# ─────────────────────────────────────────
def _setup_script() -> str:
    """建网桥 + 白名单 + NAT + DHCP 的完整脚本

    幂等：每步都先判断再动，重复执行不会报错。
    """
    return f"""
set -e

# 1. 网桥（已存在就跳过）
if [ ! -e /sys/class/net/{BRIDGE} ]; then
    ip link add name {BRIDGE} type bridge
fi
ip addr replace {HOST_IP}/{PREFIX} dev {BRIDGE}
ip link set {BRIDGE} up

# 2. 允许普通用户通过 qemu-bridge-helper 挂这个网桥
#    必须是 644：QEMU 自己会解析这个文件，640 root:kvm 时
#    普通用户读不到，启动直接报 "failed to parse default acl file"
mkdir -p /etc/qemu
touch {BRIDGE_CONF}
if ! grep -qE '^[[:space:]]*allow[[:space:]]+({BRIDGE}|all)[[:space:]]*$' {BRIDGE_CONF}; then
    echo "allow {BRIDGE}" >> {BRIDGE_CONF}
fi
chmod 644 {BRIDGE_CONF}

# 3. 让 guest 能访问外网（源地址伪装）
sysctl -qw net.ipv4.ip_forward=1
if ! iptables -t nat -C POSTROUTING -s {SUBNET} -j MASQUERADE 2>/dev/null; then
    iptables -t nat -A POSTROUTING -s {SUBNET} -j MASQUERADE
fi
if ! iptables -C FORWARD -i {BRIDGE} -j ACCEPT 2>/dev/null; then
    iptables -A FORWARD -i {BRIDGE} -j ACCEPT
fi
if ! iptables -C FORWARD -o {BRIDGE} -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; then
    iptables -A FORWARD -o {BRIDGE} -m state --state RELATED,ESTABLISHED -j ACCEPT
fi

# 4. dnsmasq 负责给 guest 发 DHCP（开机自启）
# 4. dnsmasq 负责给 guest 发 DHCP（开机自启）
#
#    **unit 必须自己建桥**：一次性脚本建的是内存态（ip link add），
#    重启就没了 —— 之前 unit 只跑 dnsmasq，重启后 "unknown interface
#    pgbr0" 无限崩溃循环（实测 1500+ 次）。桥 + 地址 + 转发 + NAT
#    全部放进 ExecStartPre（system 级 root 权限），`-` 前缀让每条
#    都幂等：已存在时报错被忽略，不存在时创建。
cat > {UNIT_PATH} <<'UNIT'
[Unit]
Description=Podman GUI VM bridge DHCP
After=network.target

[Service]
ExecStartPre=-/usr/bin/ip link add {BRIDGE} type bridge
ExecStartPre=/usr/bin/ip link set {BRIDGE} up
ExecStartPre=-/usr/bin/ip addr add {HOST_IP}/16 dev {BRIDGE}
ExecStartPre=/usr/bin/sysctl -w net.ipv4.ip_forward=1
ExecStartPre=-/usr/bin/iptables -t nat -C POSTROUTING -s {SUBNET} ! -o {BRIDGE} -j MASQUERADE
ExecStartPre=-/usr/bin/iptables -t nat -A POSTROUTING -s {SUBNET} ! -o {BRIDGE} -j MASQUERADE
ExecStartPre=-/usr/bin/iptables -C FORWARD -i {BRIDGE} -j ACCEPT
ExecStartPre=-/usr/bin/iptables -A FORWARD -i {BRIDGE} -j ACCEPT
ExecStart=/usr/bin/dnsmasq --keep-in-foreground --conf-file=/dev/null \
    --interface={BRIDGE} --bind-interfaces \
    --dhcp-range={DHCP_START},{DHCP_END},255.255.0.0,12h \
    --dhcp-leasefile={LEASES} \
    --port=0
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now {UNIT_NAME} || true
systemctl restart {UNIT_NAME} || true
"""


def setup(timeout: int = 180) -> tuple[bool, str]:
    """初始化共享网桥（会弹系统授权框）"""
    if not has_pkexec():
        return False, "系统缺少 pkexec（polkit），无法提权创建网桥"
    if not helper_available():
        return False, (
            "未找到 qemu-bridge-helper（通常由 qemu 包提供），"
            "无法免 root 挂网桥"
        )

    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", _setup_script()],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "初始化超时"
    except OSError as e:
        return False, str(e)

    if r.returncode == 0 and ready():
        return True, ""
    if r.returncode in (126, 127):
        return False, "已取消授权"

    detail = (r.stderr or r.stdout or "").strip().splitlines()
    return False, " / ".join(detail[-3:])[:300] or "初始化失败"


def teardown(timeout: int = 60) -> tuple[bool, str]:
    """移除网桥与相关配置（需要提权）"""
    script = f"""
systemctl disable --now {UNIT_NAME} 2>/dev/null || true
rm -f {UNIT_PATH}
systemctl daemon-reload
iptables -t nat -D POSTROUTING -s {SUBNET} -j MASQUERADE 2>/dev/null || true
iptables -D FORWARD -i {BRIDGE} -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -o {BRIDGE} -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true
ip link del {BRIDGE} 2>/dev/null || true
sed -i '/^allow {BRIDGE}$/d' {BRIDGE_CONF} 2>/dev/null || true
"""
    try:
        r = subprocess.run(
            ["pkexec", "sh", "-c", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)

    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or "").strip()[:200] or "移除失败"


# ─────────────────────────────────────────
# QEMU 侧
# ─────────────────────────────────────────
def qemu_args(mac: str = "") -> list[str]:
    """挂到共享网桥的 netdev 参数

    注意：网桥模式不需要 hostfwd —— 宿主机本来就能直接用
    guest 的内部 IP 访问。

    mac 必须每台 VM 不同：QEMU 默认给第一块网卡
    52:54:00:12:34:56，两台 VM 一样的话网桥分不清它们。
    """
    dev = "virtio-net-pci,netdev=net0"
    if mac:
        dev += f",mac={mac}"
    return ["-netdev", f"bridge,id=net0,br={BRIDGE}", "-device", dev]


def lease_ip(mac_or_name: str) -> str:
    """从 dnsmasq 租约里查某台 VM 的 IP

    dnsmasq 租约格式：<到期时间> <MAC> <IP> <主机名> <客户端ID>
    我们按主机名（= VM 名字）匹配，匹配不到返回空串。
    """
    try:
        text = LEASES.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3] == mac_or_name:
            return parts[2]
    return ""


def ping_ok(ip: str, timeout: int = 2) -> bool:
    """宿主机能否 ping 通某个 guest（用于判断网桥是否真的在用）"""
    if not ip:
        return False
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True, timeout=timeout + 3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def host_addr() -> str:
    return HOST_IP
