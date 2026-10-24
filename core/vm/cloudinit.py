"""cloud-init 配置生成

生成 seed ISO（含 user-data + meta-data），让 VM 首次启动时：
  · 创建用户、注入 SSH 公钥
  · 配置 sudo 免密
  · 启动 sshd
  · 设置主机名、时区

ISO 生成优先用 xorriso，回退 genisoimage / mkisofs。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

# 默认时区
DEFAULT_TZ = "Asia/Shanghai"

# 各发行版默认登录 shell（必须真实存在，否则 sshd 认为用户无效）
# 各发行版保证可用的基础包
# Alpine 默认不带 sudo，cloud-init 的 sudo 规则不会生效，
# 因此显式安装；bash 也一并提供（默认 shell 仍是 ash）。
FAMILY_PACKAGES: dict[str, list[str]] = {
    "alpine": ["sudo", "bash", "openssh"],
    "debian": ["sudo"],
    "ubuntu": ["sudo"],
    "fedora": ["sudo"],
    "rocky": ["sudo"],
    "alma": ["sudo"],
    "centos": ["sudo"],
    "arch": ["sudo"],
    "suse": ["sudo"],
    "opensuse": ["sudo"],
}

FAMILY_SHELL: dict[str, str] = {
    "debian": "/bin/bash",
    "ubuntu": "/bin/bash",
    "fedora": "/bin/bash",
    "rocky": "/bin/bash",
    "alma": "/bin/bash",
    "centos": "/bin/bash",
    "arch": "/bin/bash",
    "suse": "/bin/bash",
    "opensuse": "/bin/bash",
    "alpine": "/bin/ash",   # Alpine 只有 busybox ash，没有 bash
}

# 默认密码（用户首次登录后可自行修改）
DEFAULT_PASSWORD = "12345"


def default_username() -> str:
    """默认用户名 = 当前系统用户名（委托给 hostinfo）"""
    from core.vm.hostinfo import username

    return username()


def default_timezone() -> str:
    """默认时区 = 系统时区（委托给 hostinfo）"""
    from core.vm.hostinfo import timezone

    return timezone()


# ─────────────────────────────────────────
# user-data 生成
# ─────────────────────────────────────────
def build_user_data(
    *,
    username: str = "",
    ssh_pubkey: str,
    password: str = DEFAULT_PASSWORD,
    hostname: str = "vm",
    timezone: str = "",
    locale: str = "",
    dns: list[str] | None = None,
    search_domain: str = "",
    # 注意：uid/gid 默认 -1（不指定）。
    # 部分 cloud image 已占用 UID 1000，强行指定宿主机 UID 会让
    # cloud-init 报 "Failed to create user" 并中断 users_groups 模块，
    # 导致 SSH 认证全部失败。共享目录权限改由宿主机侧 chmod 处理。
    uid: int = -1,
    gid: int = -1,
    extra_packages: list[str] | None = None,
    runcmd: list[str] | None = None,
    family: str = "debian",
    allow_password_login: bool = True,
) -> str:
    """生成 cloud-init user-data（YAML）

    所有默认值从宿主机继承（core.vm.hostinfo）：
      username  → 当前系统用户
      timezone  → 系统时区
      locale    → 系统 LANG
      dns       → /etc/resolv.conf（过滤回环和 IPv6）

    family 影响包管理命令和 sshd 服务名：
      debian → apt / ssh
      fedora → dnf / sshd
      alpine → apk / sshd（OpenRC）
      arch   → pacman / sshd
      suse   → zypper / sshd

    allow_password_login=True 时启用密码登录（默认密码 12345），
    方便外部 SSH 客户端（PuTTY、手机 SSH）直接连接。
    """
    # ── 默认值从宿主机继承 ──
    from core.vm import hostinfo

    if not username:
        username = hostinfo.username()
    if not timezone:
        timezone = hostinfo.timezone()
    if not locale:
        locale = hostinfo.locale()
    if dns is None:
        dns = hostinfo.dns_servers()
    if not search_domain:
        search_domain = hostinfo.search_domain()

    lines: list[str] = ["#cloud-config"]

    # ── 基础 ──
    lines.append(f"hostname: {hostname}")
    lines.append(f"timezone: {timezone}")
    lines.append("manage_etc_hosts: true")
    if locale:
        lines.append(f"locale: {locale}")
    lines.append("")

    # ── 用户 ──
    # shell 必须按发行版选择。Alpine 等精简发行版没有 /bin/bash，
    # 若用户 shell 指向不存在的路径，OpenSSH 的 allowed_user() 会
    # 把该用户判定为 invalid user，从而拒绝所有认证方式
    # （公钥、密码全部失败），服务端日志表现为：
    #   userauth_pubkey: invalid user <name> querying public key
    #   userauth_pubkey: disabled because of invalid user
    # 这一坑与密钥内容、文件权限、StrictModes 均无关，极难排查。
    shell = FAMILY_SHELL.get(family, "/bin/bash")
    lines += [
        "users:",
        f"  - name: {username}",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        f"    shell: {shell}",
        "    lock_passwd: false",
        f"    plain_text_passwd: '{password}'",
        "    ssh_authorized_keys:",
        f"      - {ssh_pubkey.strip()}",
    ]
    # 显式指定 UID/GID 与宿主机一致。
    # 9p 共享用 passthrough 安全模型时，VM 内 UID 必须与宿主机相同，
    # 否则 VM 内用户写共享目录会 Permission denied（连 root 也不行）。
    # 只在宿主机能取到有效 UID 时才写，避免与镜像已有 UID 冲突。
    if uid >= 0 and gid >= 0:
        lines += [
            f"    uid: {uid}",
            f"    gid: {gid}",
        ]
    lines.append("")
    

    # ── SSH 认证 ──
    lines.append(
        f"# 密码登录：{'启用（默认密码 ' + password + '）' if allow_password_login else '禁用'}"
    )
    lines.append(f"ssh_pwauth: {'true' if allow_password_login else 'false'}")
    lines.append("disable_root: true")
    lines.append("")

    # ── 软件包（必须在 runcmd 前）──
    # 合并发行版必需包 + 用户额外包（去重、保序）
    pkgs: list[str] = []
    for pkg in FAMILY_PACKAGES.get(family, []):
        if pkg not in pkgs:
            pkgs.append(pkg)
    for pkg in (extra_packages or []):
        if pkg not in pkgs:
            pkgs.append(pkg)

    if pkgs:
        lines.append("packages:")
        for pkg in pkgs:
            lines.append(f"  - {pkg}")
        lines.append("")

    # ── write_files（统一在一处，避免重复顶层键）──
    lines.append("write_files:")
    wf_count = 0

    # 1. locale
    if locale:
        lines += [
            "  - path: /etc/default/locale",
            "    permissions: '0644'",
            "    owner: root:root",
            "    content: |",
            f"      LANG={locale}",
        ]
        wf_count += 1

    # 2. DNS（如果和默认不同）
    if dns:
        resolv = ["# 由 podman-gui 生成"]
        if search_domain:
            resolv.append(f"search {search_domain}")
        for server in dns[:3]:
            resolv.append(f"nameserver {server}")
        lines += [
            "  - path: /etc/resolv.conf",
            "    permissions: '0644'",
            "    owner: root:root",
            "    content: |",
        ]
        for row in resolv:
            lines.append(f"      {row}")
        wf_count += 1

    # 3. authorized_keys（保证末尾换行）
    lines += [
        f"  - path: /home/{username}/.ssh/authorized_keys",
        "    permissions: '0600'",
        f"    owner: {username}:{username}",
        "    content: |",
        f"      {ssh_pubkey.strip()}",
    ]
    wf_count += 1

    # 4. 权限修复脚本
    # 关键：cloud image 里 /home/<user> 可能已存在且属于 root，
    # cloud-init 创建用户时不会改已有目录的 owner，
    # 导致 sshd 的 StrictModes 检查失败（拒绝公钥认证）。
    lines += [
        f"  - path: /home/{username}/.ssh/fix-perms.sh",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        "      #!/bin/sh",
        "      set -e",
        f"      # 1. 修正 home 目录 owner（StrictModes 会检查整条路径）",
        f"      chown {username}:{username} /home/{username} 2>/dev/null || true",
        f"      chmod 755 /home/{username} 2>/dev/null || true",
        f"      # 2. 修正 .ssh 目录",
        f"      mkdir -p /home/{username}/.ssh",
        f"      chown -R {username}:{username} /home/{username}/.ssh",
        f"      chmod 700 /home/{username}/.ssh",
        f"      chmod 600 /home/{username}/.ssh/authorized_keys",
        f"      # 3. 确保 authorized_keys 末尾有换行",
        f"      [ -f /home/{username}/.ssh/authorized_keys ] && \\",
        f"        [ -n \"$(tail -c1 /home/{username}/.ssh/authorized_keys)\" ] && \\",
        f"        echo >> /home/{username}/.ssh/authorized_keys || true",
    ]
    wf_count += 1

    lines.append("")

    # ── runcmd ──
    lines.append("runcmd:")

    # SSH 服务
    if family == "alpine":
        lines.append("  - rc-update add sshd default || true")
        lines.append("  - service sshd start || true")
    else:
        lines.append(
            "  - systemctl enable sshd 2>/dev/null || "
            "systemctl enable ssh 2>/dev/null || true"
        )
        lines.append(
            "  - systemctl start sshd 2>/dev/null || "
            "systemctl start ssh 2>/dev/null || true"
        )

    # 权限修复
    lines.append(f"  - /home/{username}/.ssh/fix-perms.sh || true")

    # 密码设置（兜底）
    if allow_password_login and password:
        lines.append(
            f"  - echo '{username}:{password}' | chpasswd 2>/dev/null || "
            f"echo '{username}:{password}' | busybox chpasswd 2>/dev/null || true"
        )
        # 确保 sshd 允许密码
        lines.append(
            "  - sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' "
            "/etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null || true"
        )

    # 重启 sshd 应用配置
    lines.append(
        "  - rc-service sshd restart 2>/dev/null || "
        "systemctl restart sshd 2>/dev/null || true"
    )

    # 自定义命令
    for cmd in (runcmd or []):
        lines.append(f"  - {cmd}")

    lines.append("")
    return "\n".join(lines)


def build_meta_data(
    *,
    instance_id: str,
    hostname: str = "vm",
) -> str:
    """生成 cloud-init meta-data"""
    return "\n".join([
        f"instance-id: {instance_id}",
        f"local-hostname: {hostname}",
        "",
    ])


# ─────────────────────────────────────────
# seed ISO 生成
# ─────────────────────────────────────────
def _find_iso_tool() -> tuple[str, list[str]] | None:
    """找到可用的 ISO 生成工具

    返回 (工具名, 基础参数)
    """
    if shutil.which("xorriso"):
        return "xorriso", []
    if shutil.which("genisoimage"):
        return "genisoimage", []
    if shutil.which("mkisofs"):
        return "mkisofs", []
    return None


def iso_tool_available() -> bool:
    return _find_iso_tool() is not None


def build_seed_iso(
    out_path: Path,
    *,
    username: str = "dev",
    ssh_pubkey: str,
    hostname: str = "vm",
    instance_id: str = "iid-1",
    timezone: str = DEFAULT_TZ,
    locale: str = "",
    dns: list[str] | None = None,
    uid: int = -1,
    gid: int = -1,
    extra_packages: list[str] | None = None,
    runcmd: list[str] | None = None,
    family: str = "debian",
) -> tuple[bool, str]:
    """生成 cloud-init seed ISO

    timezone / locale / dns 建议从宿主机继承（core.vm.hostinfo），
    这样 VM 与宿主机行为一致，不写死任何区域设置。

    返回 (成功, 错误信息)
    """
    tool = _find_iso_tool()
    if not tool:
        return False, (
            "未找到 ISO 生成工具。\n"
            "请安装 xorriso（Arch: libisoburn / Debian: xorriso / Fedora: xorriso）"
        )

    tool_name, _ = tool

    user_data = build_user_data(
        username=username,
        ssh_pubkey=ssh_pubkey,
        hostname=hostname,
        timezone=timezone,
        locale=locale,
        dns=dns,
        uid=uid,
        gid=gid,
        extra_packages=extra_packages,
        runcmd=runcmd,
        family=family,
    )
    meta_data = build_meta_data(instance_id=instance_id, hostname=hostname)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "user-data").write_text(user_data, encoding="utf-8")
        (tmp / "meta-data").write_text(meta_data, encoding="utf-8")

        out_path.parent.mkdir(parents=True, exist_ok=True)

        if tool_name == "xorriso":
            cmd = [
                "xorriso", "-as", "mkisofs",
                "-output", str(out_path),
                "-volid", "cidata",
                "-joliet", "-rock",
                str(tmp / "user-data"),
                str(tmp / "meta-data"),
            ]
        else:
            # genisoimage / mkisofs
            cmd = [
                tool_name,
                "-output", str(out_path),
                "-volid", "cidata",
                "-joliet", "-rock",
                str(tmp / "user-data"),
                str(tmp / "meta-data"),
            ]

        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[:400]

    if not out_path.exists() or out_path.stat().st_size == 0:
        return False, "ISO 生成失败（文件为空）"

    return True, ""


# ─────────────────────────────────────────
# SSH 密钥
# ─────────────────────────────────────────
def generate_ssh_key(key_path: Path, comment: str = "podman-gui-vm") -> tuple[bool, str]:
    """生成 Ed25519 密钥对（无密码）

    返回 (成功, 错误信息)
    """
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        return False, "未找到 ssh-keygen"

    if key_path.exists():
        return True, ""     # 已有，复用

    key_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        r = subprocess.run(
            [
                ssh_keygen, "-t", "ed25519",
                "-f", str(key_path),
                "-N", "",              # 无密码
                "-C", comment,
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]

    # 权限收紧
    key_path.chmod(0o600)
    pub = key_path.with_suffix(key_path.suffix + ".pub")
    if pub.exists():
        pub.chmod(0o644)

    return True, ""


def read_pubkey(key_path: Path) -> str:
    """读取公钥内容

    注意：cloud-init 写入 authorized_keys 时，如果公钥没有
    末尾换行，OpenSSH 可能无法正确解析（实测 Alpine 会拒绝）。
    这里统一去掉多余空白，写入时由 build_user_data 保证格式。
    """
    pub = key_path.with_suffix(key_path.suffix + ".pub")
    if pub.exists():
        # 只保留单行内容，去掉首尾空白
        content = pub.read_text(encoding="utf-8").strip()
        # 取第一行（公钥文件可能有多行注释）
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        return lines[0] if lines else ""
    return ""
