"""宿主机配置探测

VM 的默认配置应该继承宿主机，而不是写死：
  · 用户名      → getpass.getuser()
  · 时区        → timedatectl / /etc/localtime
  · Locale      → $LANG
  · 键盘布局    → localectl
  · DNS 服务器  → /etc/resolv.conf
  · NTP         → timedatectl

所有函数都有回退值，读取失败不影响 VM 创建。
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ─────────────────────────────────────────
# 基础探测工具
# ─────────────────────────────────────────
def _run(cmd: list[str], timeout: int = 3) -> str:
    """执行命令并返回 stdout（失败返回空串）"""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ""


# ─────────────────────────────────────────
# 用户名
# ─────────────────────────────────────────
def username() -> str:
    """当前用户名

    优先级：getpass → $USER → $LOGNAME → "user"
    cloud-init 要求：小写字母开头，只含字母数字连字符。
    """
    name = ""

    try:
        import getpass

        name = getpass.getuser()
    except Exception:
        pass

    if not name:
        name = os.environ.get("USER") or os.environ.get("LOGNAME") or ""

    # root 用 user（cloud image 禁用 root 登录）
    if not name or name == "root":
        name = "user"

    # 规范化
    name = re.sub(r"[^a-zA-Z0-9-]", "", name).lower()
    if not name or not name[0].isalpha():
        name = "user"

    return name[:32]


def user_full_name() -> str:
    """用户全名（GECOS 字段）"""
    try:
        import pwd

        entry = pwd.getpwuid(os.getuid())
        # GECOS 格式: Full Name,Room,Work Phone,...
        gecos = entry.pw_gecos.split(",")[0].strip()
        return gecos
    except (KeyError, ImportError, OSError):
        return ""


# ─────────────────────────────────────────
# 时区
# ─────────────────────────────────────────
def timezone() -> str:
    """系统时区

    优先级：
      1. timedatectl（systemd 系统，最准确）
      2. /etc/localtime 符号链接
      3. /etc/timezone 文件（Debian 系）
      4. TZ 环境变量
      5. 回退 UTC
    """
    # 1. timedatectl
    tz = _run(["timedatectl", "show", "-p", "Timezone", "--value"])
    if tz and "/" in tz:
        return tz

    # 2. /etc/localtime 符号链接
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass

    # 3. /etc/timezone
    try:
        tz = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if tz and "/" in tz:
            return tz
    except OSError:
        pass

    # 4. 环境变量
    tz = os.environ.get("TZ", "")
    if tz and "/" in tz:
        return tz

    return "UTC"


# ─────────────────────────────────────────
# Locale
# ─────────────────────────────────────────
def locale() -> str:
    """系统 locale（如 zh_CN.UTF-8）"""
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var, "")
        if val and val not in ("C", "POSIX"):
            return val

    # 从 localectl 读
    out = _run(["localectl", "status"])
    m = re.search(r"System Locale:\s*LANG=(\S+)", out)
    if m:
        return m.group(1).strip()

    return "en_US.UTF-8"


def locale_short() -> str:
    """短 locale（如 zh_CN）"""
    loc = locale()
    # zh_CN.UTF-8 → zh_CN
    return loc.split(".")[0] if "." in loc else loc


# ─────────────────────────────────────────
# 键盘布局
# ─────────────────────────────────────────
def keyboard_layout() -> str:
    """X11 键盘布局（如 us, cn）"""
    out = _run(["localectl", "status"])
    m = re.search(r"X11 Layout:\s*(\S+)", out)
    if m and m.group(1) != "(unset)":
        return m.group(1)

    # 从 vconsole 读
    m = re.search(r"VC Keymap:\s*(\S+)", out)
    if m and m.group(1) != "(unset)":
        return m.group(1)

    return "us"


# ─────────────────────────────────────────
# DNS
# ─────────────────────────────────────────
def dns_servers() -> list[str]:
    """系统 DNS 服务器

    注意：如果只有 127.0.0.53（systemd-resolved），
    VM 内无法访问，需要回退到上游 DNS 或公共 DNS。
    """
    servers = []
    try:
        text = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    except OSError:
        return ["1.1.1.1", "8.8.8.8"]

    for line in text.splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) >= 2:
                servers.append(parts[1])

    # 过滤本地回环（VM 内不可用）+ 清理 # 后缀 + 过滤 IPv6
    usable = []
    for s in servers:
        addr = s.split("#", 1)[0].strip()
        if addr.startswith("127.") or ":" in addr:
            continue
        usable.append(addr)

    if usable:
        return usable

    # 全是 127.x → 尝试读 systemd-resolved 的上游
    upstream = _resolved_upstream()
    if upstream:
        return upstream

    # 回退公共 DNS
    return ["1.1.1.1", "8.8.8.8"]


def _resolved_upstream() -> list[str]:
    """从 systemd-resolved 读上游 DNS

    resolvectl 输出格式可能是：
      9.9.9.9#dns.quad9.net          ← 带 TLS 标记
      2620:fe::9#dns.quad9.net       ← IPv6
      192.168.31.1                   ← 普通 IPv4

    需要清理 # 后缀，并优先选 IPv4（VM 内 IPv6 可能不通）。
    """
    out = _run(["resolvectl", "status"], timeout=5)
    if not out:
        return []

    raw = []
    for line in out.splitlines():
        m = re.search(r"(?:Current DNS Server|DNS Servers):\s*(.+)", line)
        if m:
            for token in m.group(1).split():
                raw.append(token)

    # 清理：去掉 # 后缀
    cleaned = []
    for token in raw:
        addr = token.split("#", 1)[0].strip()
        if not addr or addr.startswith("127."):
            continue
        # 过滤 IPv6（含冒号），VM 内 NAT 不支持 IPv6 DNS
        if ":" in addr:
            continue
        cleaned.append(addr)

    # 去重保序
    seen = set()
    result = []
    for addr in cleaned:
        if addr not in seen:
            seen.add(addr)
            result.append(addr)

    return result[:3]


def search_domain() -> str:
    """DNS 搜索域"""
    try:
        text = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    except OSError:
        return ""

    for line in text.splitlines():
        if line.strip().startswith("search"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
    return ""


# ─────────────────────────────────────────
# 主机名
# ─────────────────────────────────────────
def hostname() -> str:
    """宿主机名（用于生成 VM 默认名称参考）"""
    try:
        return socket.gethostname().split(".")[0]
    except OSError:
        return "host"


# ─────────────────────────────────────────
# NTP
# ─────────────────────────────────────────
def ntp_enabled() -> bool:
    """是否启用 NTP 时间同步"""
    out = _run(["timedatectl", "show", "-p", "NTP", "--value"])
    return out.lower() in ("yes", "true", "1")


# ─────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────
@dataclass
class HostProfile:
    """宿主机配置快照（用于 VM 默认值）"""

    username: str = ""
    user_full_name: str = ""
    timezone: str = ""
    locale: str = ""
    keyboard: str = ""
    dns: list[str] = field(default_factory=list)
    search_domain: str = ""
    hostname: str = ""
    ntp: bool = False

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "user_full_name": self.user_full_name,
            "timezone": self.timezone,
            "locale": self.locale,
            "keyboard": self.keyboard,
            "dns": self.dns,
            "search_domain": self.search_domain,
            "hostname": self.hostname,
            "ntp": self.ntp,
        }


def host_profile() -> HostProfile:
    """采集宿主机配置"""
    return HostProfile(
        username=username(),
        user_full_name=user_full_name(),
        timezone=timezone(),
        locale=locale(),
        keyboard=keyboard_layout(),
        dns=dns_servers(),
        search_domain=search_domain(),
        hostname=hostname(),
        ntp=ntp_enabled(),
    )


def summary() -> str:
    """人类可读的摘要"""
    p = host_profile()
    lines = [
        f"用户名:   {p.username}" + (f" ({p.user_full_name})" if p.user_full_name else ""),
        f"时区:     {p.timezone}",
        f"Locale:   {p.locale}",
        f"键盘:     {p.keyboard}",
        f"DNS:      {', '.join(p.dns)}",
        f"搜索域:   {p.search_domain or '(无)'}",
        f"主机名:   {p.hostname}",
        f"NTP:      {'启用' if p.ntp else '禁用'}",
    ]
    return "\n".join(lines)
