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
# ─────────────────────────────────────────
# 串口控制台修复脚本
#
# 部分 cloud image（如 Alpine 3.22.7+）的内核 cmdline 里有多个
# console=，Linux 取最后一个当 /dev/console。我们 -display none
# 无显卡，若最后一个是 tty0，OpenRC 的服务日志就全进黑洞，
# 「VM 起不来」时宿主机串口里只有 initramfs 的几行，无法排查。
#
# 这个脚本在首次启动时把 cmdline 收敛成单个 console=ttyS0。
# 以独立常量保存、投放时 base64 内嵌，免去 YAML/shell 多层转义。
# ─────────────────────────────────────────
_SERIAL_CONSOLE_PY = r'''#!/usr/bin/env python3
"""把内核与服务的日志收敛到串口（由 podman-gui 投放，幂等）

问题
────
Alpine 3.22.7+ cloud image 的内核 cmdline 里有三个 console=：
    console=ttyS0,115200n8 console=ttyAMA0,115200n8 console=tty0
Linux 取**最后一个** console= 作为 /dev/console。我们的 VM 跑
-display none（无显卡），tty0 是黑洞，于是 OpenRC 的服务日志和报错
全部丢失，宿主机串口只剩 initramfs 那几行 —— VM 起不来时毫无线索。

做法
────
把 cmdline 收敛成单个 console=ttyS0。直接补丁 /boot/grub/grub.cfg
（GRUB 实际读取的文件），并把 /etc/default/grub 也写成规范单行，
避免将来 update-grub 又生成回三 console 的形式。

为什么不改完 /etc/default/grub 就交给 grub-mkconfig
──────────────────────────────────────────────────
该镜像的 GRUB_CMDLINE_LINUX_DEFAULT 值跨多行，里面还有
    autodetect_serial=setconsole=[Aa]mazon,...^\\(7783-...\\|OracleCloud...
这类带反斜杠转义与正则元字符的片段。行内 sed 会留下未闭合的引号，
grub-mkconfig 直接报
    syntax error: unterminated quoted string
即使折叠成单行，那些元字符也会让 GRUB 解析器报 syntax error。
所以这里直接补丁 grub.cfg，并把含元字符的 token 整个剔除。
"""
import shutil
import subprocess
import sys
from pathlib import Path

FLAG = Path("/var/lib/podman-gui-serial-console.done")
GRUB_CFG = Path("/boot/grub/grub.cfg")
GRUB_DEFAULT = Path("/etc/default/grub")

WANT = "console=ttyS0,115200n8"
BAD_CHARS = set("$\\()[]|*^")


def clean(tokens):
    """保留有意义的 token；console= 只留一个；剔除元字符残留"""
    kept = []
    seen = False
    for t in tokens:
        if not t:
            continue
        if any(c in BAD_CHARS for c in t):
            continue
        if t.startswith("console="):
            if seen:
                continue
            seen = True
            kept.append(WANT)
            continue
        kept.append(t)
    if not seen:
        kept.append(WANT)
    return kept


def patch_grub_cfg():
    if not GRUB_CFG.exists():
        return False
    rows = GRUB_CFG.read_text().split("\n")
    changed = False
    for i, row in enumerate(rows):
        if not row.lstrip().startswith("linux") or "vmlinuz" not in row:
            continue
        parts = row.split()
        head, tokens = parts[0], parts[1:]
        new = clean(tokens)
        if new != tokens:
            rows[i] = head + "\t" + " ".join(new)
            changed = True
    if changed:
        GRUB_CFG.write_text("\n".join(rows))
    return changed


def patch_grub_default():
    if not GRUB_DEFAULT.exists():
        return False
    rows = GRUB_DEFAULT.read_text().split("\n")
    out, changed, i = [], False, 0
    while i < len(rows):
        if rows[i].startswith("GRUB_CMDLINE_LINUX_DEFAULT="):
            buf = rows[i]
            while buf.count('"') % 2 == 1 and i + 1 < len(rows):
                i += 1
                buf += rows[i]
            value = buf.split("=", 1)[1]
            line = 'GRUB_CMDLINE_LINUX_DEFAULT="%s"' % " ".join(
                clean(value.replace('"', "").split()))
            if line != rows[i]:
                changed = True
            out.append(line)
        else:
            out.append(rows[i])
        i += 1
    if changed:
        GRUB_DEFAULT.write_text("\n".join(out))
    return changed


def main():
    if FLAG.exists():
        return 0
    did = patch_grub_cfg()
    did = patch_grub_default() or did
    if did:
        if shutil.which("grub-mkconfig"):
            subprocess.run(["grub-mkconfig", "-o", str(GRUB_CFG)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            patch_grub_cfg()
        elif shutil.which("update-grub"):
            subprocess.run(["update-grub"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            patch_grub_cfg()
        FLAG.parent.mkdir(parents=True, exist_ok=True)
        FLAG.touch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _b64_lines(text: str) -> list[str]:
    """把文本 base64 编码并按 76 列折行（用于内嵌进 cloud-init）"""
    import base64

    raw = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return [raw[i:i + 76] for i in range(0, len(raw), 76)]


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
    # owner 必须是 root，不能写 {username}：
    # write_files 跑在 init-network 阶段，早于 users 模块创建用户；
    # 此时 cloud-init 的 ensure_dir 会 chown 该用户并抛
    #   OSError: Unknown user or group: getpwnam name not found
    # 这个异常会**中断整个 write_files 模块**，排在后面的
    # fix-perms.sh 与串口修复脚本会静默不落地。
    # 属主由 runcmd 里的 fix-perms.sh 在用户存在后修正。
        f"  - path: /home/{username}/.ssh/authorized_keys",
        "    permissions: '0600'",
        "    owner: root:root",
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

    # 5. 串口控制台修复脚本
    #
    # 不能放 bootcmd：bootcmd 属于 local 阶段，早于 write_files 所在的
    # config 阶段，脚本文件还不存在（会报 not found 并记一条 init error）。
    # 用 runcmd 才能拿到这个脚本。
    lines += [
        "  - path: /usr/local/sbin/podman-gui-serial-console",
        "    permissions: '0755'",
        "    owner: root:root",
        "    content: |",
        "      #!/bin/sh",
        "      # 自解压并执行内嵌的 python3 脚本",
        "      base64 -d > /usr/local/sbin/podman-gui-serial-console.py <<'__PGPY__'",
    ]
    for _line in _b64_lines(_SERIAL_CONSOLE_PY):
        lines.append("        " + _line)
    lines += [
        "      __PGPY__",
        "      chmod 755 /usr/local/sbin/podman-gui-serial-console.py",
        "      python3 /usr/local/sbin/podman-gui-serial-console.py || true",
    ]
    wf_count += 1

    lines.append("")

    # ── runcmd ──
    lines.append("runcmd:")
    # 串口控制台修复（幂等；首启改 cmdline，下次开机日志完整）
    lines.append("  - /usr/local/sbin/podman-gui-serial-console || true")

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
