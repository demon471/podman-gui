"""VM 网络：端口转发管理 + 访问地址生成

域名方案见 core/vm/dns.py：
  宿主机一次性配置 dnsmasq 通配规则后，所有 *.<vm-name>.vm.local
  都解析到 127.0.0.1，VM 增删不需要再改宿主机。

本模块负责：
  - 端口占用检测
  - 生成访问地址（localhost / 域名 两种形式）
  - 按场景推荐端口转发

旧的 /etc/hosts 方案已废弃，保留 sync_hosts / read_managed_hosts
仅为兼容历史数据，新代码请用 dns.vm_domain()。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path

HOSTS_FILE = Path("/etc/hosts")

# 域名后缀（参考 OrbStack 的 .orb.local）
DOMAIN_SUFFIX = ".local"

# 标记注释（用于识别本程序写入的条目）
MARKER = "# podman-gui-vm"


# ─────────────────────────────────────────
# 域名
# ─────────────────────────────────────────
def vm_domain(vm_name: str) -> str:
    """VM 域名，如 alpine-dev.vm.local

    实际规则由 core.vm.dns 统一定义（通配后缀 + 名称清洗）。
    """
    from core.vm.dns import vm_domain as _vm_domain

    return _vm_domain(vm_name)


def hosts_writable() -> bool:
    """是否能写 /etc/hosts（需要 root 或 sudo 免密）"""
    return os.access(HOSTS_FILE, os.W_OK)


def _has_sudo_nopass() -> bool:
    """检查 sudo 是否免密"""
    sudo = "/usr/bin/sudo"
    if not Path(sudo).exists():
        return False
    try:
        r = subprocess.run(
            [sudo, "-n", "true"], capture_output=True, timeout=5
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _write_hosts(content: str) -> tuple[bool, str]:
    """写入 /etc/hosts（直接写或通过 sudo tee）"""
    if hosts_writable():
        try:
            HOSTS_FILE.write_text(content, encoding="utf-8")
            return True, ""
        except OSError as e:
            return False, str(e)

    if _has_sudo_nopass():
        try:
            r = subprocess.run(
                ["sudo", "-n", "tee", str(HOSTS_FILE)],
                input=content, capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return True, ""
            return False, (r.stderr or "").strip()[:200]
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    return False, "需要 root 权限修改 /etc/hosts（当前无 sudo 免密）"


def _strip_managed(text: str) -> str:
    """移除本程序写入的行"""
    lines = []
    for line in text.splitlines():
        if MARKER in line:
            continue
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def sync_hosts(vms: list) -> tuple[bool, str]:
    """同步所有 VM 域名到 /etc/hosts

    先清除旧的本程序条目，再写入当前的。
    """
    try:
        current = HOSTS_FILE.read_text(encoding="utf-8")
    except OSError as e:
        return False, str(e)

    base = _strip_managed(current)

    entries = []
    for vm in vms:
        domain = vm_domain(vm.name)
        entries.append(f"127.0.0.1\t{domain}\t{MARKER}")

    if entries:
        content = base + "\n" + "\n".join(entries) + "\n"
    else:
        content = base

    return _write_hosts(content)


def read_managed_hosts() -> list[str]:
    """读取本程序写入的域名"""
    try:
        text = HOSTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if MARKER in line:
            parts = line.split()
            if len(parts) >= 2:
                out.append(parts[1])
    return out


def resolve(domain: str) -> str:
    """解析域名（验证 hosts 是否生效）"""
    try:
        return socket.gethostbyname(domain)
    except OSError:
        return ""


# ─────────────────────────────────────────
# 端口
# ─────────────────────────────────────────
def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已被监听"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def scan_ports(start: int = 8000, count: int = 20) -> list[int]:
    """扫描一段范围内的可用端口"""
    free = []
    for port in range(start, start + count):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                free.append(port)
        except OSError:
            continue
    return free


# ─────────────────────────────────────────
# 访问地址生成
# ─────────────────────────────────────────
def access_urls(vm) -> list[tuple[str, str, str]]:
    """生成 VM 的访问地址列表

    返回 [(显示名, 地址, 说明)]
    """
    out = []
    domain = vm_domain(vm.name)

    for fwd in vm.spec.forwards:
        # SSH 单独处理
        if fwd.guest_port == 22:
            out.append((
                "SSH",
                f"ssh -i {vm.ssh_key} -p {fwd.host_port} "
                f"{vm.spec.username}@127.0.0.1",
                "命令行连接",
            ))
            continue

        # Web 类端口：域名优先（更直观），localhost 兜底
        if fwd.guest_port in (80, 443, 3000, 8080, 8000, 5000, 9000):
            scheme = "https" if fwd.guest_port == 443 else "http"
            out.append((
                f"Web (VM:{fwd.guest_port})",
                f"{scheme}://{domain}:{fwd.host_port}",
                "域名访问",
            ))
            out.append((
                f"Web (VM:{fwd.guest_port})",
                f"{scheme}://127.0.0.1:{fwd.host_port}",
                "localhost 访问",
            ))
        else:
            out.append((
                f"TCP (VM:{fwd.guest_port})",
                f"127.0.0.1:{fwd.host_port}",
                f"{fwd.protocol} 转发",
            ))

    return out


def suggest_forwards(preset: str = "web") -> list:
    """按预设推荐端口转发

    返回 [(host_port, guest_port, 说明)]
    """
    from core.vm.qemu import PortForward

    presets = {
        "web": [(8080, 80, "HTTP"), (8443, 443, "HTTPS")],
        "node": [(3000, 3000, "Node.js 开发服务器")],
        "db": [(3306, 3306, "MySQL/MariaDB"), (5432, 5432, "PostgreSQL"),
               (6379, 6379, "Redis")],
        "dev": [(8080, 80, "HTTP"), (3000, 3000, "开发服务器"),
                (5173, 5173, "Vite"), (8000, 8000, "通用")],
    }
    return presets.get(preset, presets["web"])
