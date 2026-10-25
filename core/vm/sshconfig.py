"""SSH 客户端配置管理

目标：外部 SSH 客户端用域名直接连虚拟机，不用记端口。

    ssh test.vm.local
    ssh w.vm.local

实现
────
自动维护 ~/.ssh/config，为每台 VM 写入一段配置：

    # === podman-gui managed: begin ===
    Host test.vm.local
        HostName 127.0.0.1
        Port 2222
        User john
        IdentityFile ~/.local/share/podman-gui/vm/machines/test/ssh_key
        StrictHostKeyChecking no
        UserKnownHostsFile /dev/null
    # === podman-gui managed: end ===

用标记注释包裹，同步时只替换这一段，不动用户自己的配置。

为什么端口不统一
────────────────
QEMU 的 hostfwd 在宿主机侧必须用不同端口（同一个 127.0.0.1:2222
只能被一个 QEMU 实例监听）。但用户不需要关心这个——
~/.ssh/config 里已按域名映射到对应端口，外部统一用域名访问。

域名解析由 core.vm.dns 负责（*.vm.local → 127.0.0.1），
两者配合实现「ssh <名字>.vm.local 就能连」。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SSH_DIR = Path.home() / ".ssh"
SSH_CONFIG = SSH_DIR / "config"

MARKER = "podman-gui managed"
BEGIN = f"# === {MARKER}: begin ==="
END = f"# === {MARKER}: end ==="


def _managed_block(vms: list) -> str:
    """生成本程序管理的配置段"""
    lines = [BEGIN]

    for vm in vms:
        # 找 SSH 转发（guest_port == 22）
        ssh_fwd = next(
            (f for f in vm.spec.forwards if f.guest_port == 22), None
        )
        if ssh_fwd is None:
            continue

        from core.vm.dns import vm_domain

        domain = vm_domain(vm.spec.name)
        key = vm.ssh_key

        lines += [
            f"Host {domain}",
            f"    HostName {ssh_fwd.host_addr or '127.0.0.1'}",
            f"    Port {ssh_fwd.host_port}",
            f"    User {vm.spec.username}",
        ]
        if key.exists():
            lines.append(f"    IdentityFile {key}")
        # 虚拟机重建后 host key 会变，避免频繁提示
        lines += [
            "    StrictHostKeyChecking no",
            "    UserKnownHostsFile /dev/null",
            "    LogLevel ERROR",
            "",
        ]

    lines.append(END)
    return "\n".join(lines) + "\n"


def _strip_managed(text: str) -> str:
    """移除本程序管理的配置段"""
    pattern = re.compile(
        re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?",
        re.S,
    )
    return pattern.sub("", text)


def sync_config(vms: list) -> tuple[bool, str]:
    """同步 VM 到 ~/.ssh/config

    只替换标记段之间的内容，用户自己的配置原样保留。
    """
    try:
        SSH_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(SSH_DIR, 0o700)

        current = ""
        if SSH_CONFIG.exists():
            current = SSH_CONFIG.read_text(encoding="utf-8")

        base = _strip_managed(current).rstrip()
        block = _managed_block(vms)

        if base:
            content = base + "\n\n" + block
        else:
            content = block

        SSH_CONFIG.write_text(content, encoding="utf-8")
        os.chmod(SSH_CONFIG, 0o600)
        return True, ""

    except OSError as e:
        return False, str(e)


def read_managed_hosts() -> list[str]:
    """读取已配置的域名（供 UI 显示）"""
    if not SSH_CONFIG.exists():
        return []
    try:
        text = SSH_CONFIG.read_text(encoding="utf-8")
    except OSError:
        return []

    out = []
    in_block = False
    for line in text.splitlines():
        if BEGIN in line:
            in_block = True
            continue
        if END in line:
            break
        if in_block:
            m = re.match(r"\s*Host\s+(\S+)", line)
            if m:
                out.append(m.group(1))
    return out


def ssh_command(vm) -> str:
    """给用户的连接命令（用域名，不用端口）"""
    from core.vm.dns import vm_domain

    return f"ssh {vm_domain(vm.spec.name)}"
