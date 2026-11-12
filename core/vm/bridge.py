"""轻机器共享网桥（机器互通、宿主机直连内部 IP）

为什么需要
──────────
轻机器挂在这个网桥上：
  · 所有机器在同一个二层网络 → 机器之间互通
  · 宿主机在网桥上有地址（10.99.0.1）→ 直接 ssh 机器 IP
  · dnsmasq 发 DHCP，每台固定 MAC → 租约稳定可查
  · NAT 出外网（装包、下载）

权限
────
建网桥需要 root，只在初始化时一次（pkexec 弹系统授权框）。
容器侧由 lxc-user-nic 接管（/etc/lxc/lxc-usernet 配额 +
家目录 ACL），同一份脚本一并配置 —— 见 _setup_script。
初始化幂等：重复执行不出错。
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

LEASES = Path("/var/lib/misc/dnsmasq-pgbr0.leases")
UNIT_NAME = "podbox-net.service"
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
# 初始化时给发起用户分配的 veth 配额（/etc/lxc/lxc-usernet）
USERNET_QUOTA = 10


# ─────────────────────────────────────────
# 状态查询（不需要 root）
# ─────────────────────────────────────────
def bridge_exists() -> bool:
    return Path(f"/sys/class/net/{BRIDGE}").exists()


def ready() -> bool:
    """网桥是否就绪（机器能直接用，无需再提权）"""
    return bridge_exists()

def lease_ip(mac_or_name: str) -> str:
    """从 dnsmasq 租约里查某台机器的 IP

    dnsmasq 租约格式：<到期时间> <MAC> <IP> <主机名> <客户端ID>

    **MAC 优先**：Alpine 的 dhcpcd 不发 hostname，租约里主机名
    一栏全是 "*"，按名字匹配经常落空（机器重启换 IP 后拿到旧
    地址，探测 "No route to host"）。容器现在有固定 MAC
    （container.vm_mac），按 MAC 查永远准；主机名匹配保留作
    兜底（兼容手写配置的机器）。
    """
    try:
        text = LEASES.read_text(encoding="utf-8")
    except OSError:
        return ""
    is_mac = mac_or_name.count(":") == 5
    fallback = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if is_mac and parts[1].lower() == mac_or_name.lower():
            return parts[2]
        if not is_mac and parts[3] == mac_or_name:
            fallback = parts[2]
    return fallback


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
Description=PodBox VM bridge DHCP
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
    --interface={BRIDGE} --except-interface=lo --bind-dynamic \
    --dhcp-range={DHCP_START},{DHCP_END},255.255.0.0,12h \
    --dhcp-leasefile={LEASES} \
    --dhcp-option=option:dns-server,{HOST_IP} \
    --dhcp-option=option:router,{HOST_IP} \
    --listen-address={HOST_IP} --port=53 \
    --resolv-file=/run/systemd/resolve/resolv.conf
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT

# 5. 非特权容器前置（lxc-user-nic 的配额与 ACL）
#
#    a) /etc/lxc/lxc-usernet：给发起用户分配 veth 配额，
#       不存在时 lxc-user-nic 直接失败（"Failed to open
#       lxc-usernet"，实测容器起不来）
#    b) 家目录 ACL：/home/<user> 通常是 0700，容器 root
#       （subuid 段）无法穿越到放在下面的 rootfs，
#       报 "Could not access /home/<user>"。给 subuid 段加 x。
#
#    pkexec 环境里用 PKEXEC_UID 拿发起用户（root 的 whoami 不对）。
HOME_USER=$(getent passwd "${{PASSWORD_xzrychpv}}" | cut -d: -f1)
[ -n "$HOME_USER" ] || HOME_USER=$(getent passwd 1000 | cut -d: -f1)
# 注意：**不能用 getent subuid** —— 部分发行版的 glibc 没有
# subuid 数据库（实测 CachyOS 报「未知的数据库：subuid」并返回
# 空），导致下面的 setfacl 被静默跳过，用户表现为「已启用共享
# 网络，却一直提示还缺家目录 ACL」。直接读文件。
SUBUID=$(grep "^$HOME_USER:" /etc/subuid 2>/dev/null | head -1 | cut -d: -f2)

mkdir -p /etc/lxc
if ! grep -qE "^$HOME_USER[[:space:]]" /etc/lxc/lxc-usernet 2>/dev/null; then
    echo "$HOME_USER veth {BRIDGE} 10" >> /etc/lxc/lxc-usernet
fi

if [ -n "$SUBUID" ] && [ -d "/home/$HOME_USER" ]; then
    if ! setfacl -m "u:$SUBUID:x" "/home/$HOME_USER" 2>/dev/null; then
        echo "WARN: 家目录 ACL 设置失败（是否缺少 acl 包？）" >&2
    fi
else
    echo "WARN: 取不到 subuid 段（/etc/subuid 里没有 $HOME_USER）" >&2
fi

systemctl daemon-reload
systemctl enable --now {UNIT_NAME} || true
systemctl restart {UNIT_NAME} || true
"""


def setup(timeout: int = 180) -> tuple[bool, str]:
    """初始化共享网桥（会弹系统授权框）"""
    if not has_pkexec():
        return False, "系统缺少 pkexec（polkit），无法提权创建网桥"

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
        # 脚本退出码 0 不代表前置全配好（例如 setfacl 那步可能因
        # 缺 acl 包失败，只往 stderr 打 WARN）。这里复核一遍，
        # 否则用户会看到「已启用」却一直提示还缺 ACL。
        try:
            from core.vm import container as _c

            missing = _c.pending_fixups()
        except Exception:
            missing = []
        if missing:
            return False, "网桥已创建，但系统前置仍缺：" + "；".join(missing)
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
