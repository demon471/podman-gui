"""轻机器：LXC 系统容器后端

产品只有这一种机器形态（docs/light-machines.md v2）：
  · 启动 1-2s、内存开销 ~几MB（共享宿主内核）
  · 跑完整 init + sshd，有自己的 rootfs，发行版随便换
  · 网络走既有共享网桥 pgbr0（veth + DHCP），每台唯一 IP

LXC 只做运行时（lxc-start/stop/info/attach）；创建、配置、
网络沿用本应用既有体系。lxc 工具用 -P 指定实例目录，
要求实例目录布局为 <lxcpath>/<name>/config —— 我们的机器
目录正好是 machines/<name>/，所以 lxc 配置文件固定叫 config。

踩过的坑（勿再犯）：
  · lxc 默认 lxcpath 是 /var/lib/lxc，必须显式 -P，
    否则永远找不到机器
  · 非特权容器必须有 idmap（subuid/subgid），宿主用户没有
    subuid 条目时启动会失败
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

log_notes: list[str] = []

# 机器目录内的文件名（lxc 约定：<lxcpath>/<name>/config）
LXC_CONF_NAME = "config"
ROOTFS_DIR_NAME = "rootfs"


# ─────────────────────────────────────────
# 依赖
# ─────────────────────────────────────────
def lxc_version() -> str | None:
    """lxc 是否可用，返回版本号；不可用返回 None"""
    try:
        r = subprocess.run(
            ["lxc-start", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip().splitlines()[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def subuid_range() -> tuple[int, int] | None:
    """当前用户的 subuid 起始与数量

    非特权容器必需：容器内 root(0) 映射到宿主的 subuid 段。
    """
    user = os.environ.get("USER") or Path.home().name
    for fname in ("/etc/subuid", "/etc/subgid"):
        try:
            for line in Path(fname).read_text(encoding="utf-8").splitlines():
                parts = line.split(":")
                if len(parts) >= 3 and parts[0] == user:
                    return int(parts[1]), int(parts[2])
        except OSError:
            continue
    return None


def prerequisites() -> tuple[bool, str]:
    """创建/启动前的依赖检查，返回 (是否通过, 错误说明)"""
    ver = lxc_version()
    if not ver:
        return False, "LXC 未安装（需要 lxc 包），请在设置页安装"
    if not subuid_range():
        return False, "当前用户没有 subuid/subgid 条目，无法使用非特权容器"
    return True, ""


# ─────────────────────────────────────────
# 配置生成
# ─────────────────────────────────────────
def conf_text(
    spec,
    rootfs: Path,
    bridge_name: str,
    subuid_start: int,
    subuid_count: int,
) -> str:
    """生成 lxc 配置文件内容

    spec 只用到 name/hostname/shares/autostart（鸭子类型，
    传 VMSpec 或测试桩都行）。
    """
    lines = [
        "# 由 PodBox 生成，重建机器时会覆盖",
        "lxc.include = /usr/share/lxc/config/common.conf",
        f"lxc.uts.name = {spec.hostname or spec.name}",
        f"lxc.arch = {spec.arch}",
        # 非特权容器：容器内 id 0..65535 → 宿主 subuid 段
        f"lxc.idmap = u 0 {subuid_start} {subuid_count}",
        f"lxc.idmap = g 0 {subuid_start} {subuid_count}",
        # rootfs
        f"lxc.rootfs.path = dir:{rootfs}",
        # 网络：挂在共享网桥上，DHCP 由 pgbr0 的 dnsmasq 发放
        "lxc.net.0.type = veth",
        f"lxc.net.0.link = {bridge_name}",
        "lxc.net.0.name = eth0",
        "lxc.net.0.flags = up",
        "lxc.net.0.hwaddr = 00:16:3e:xx:xx:xx",  # 占位，start 时由 lxc 生成
    ]

    # 共享目录：bind mount，启动即挂（无需 VM 档的 virtiofs）
    # idmap=container 把宿主文件 id 映射进容器用户命名空间
    for host_path, mp in spec.shares:
        mp_norm = mp.strip("/")
        lines.append(
            f"lxc.mount.entry = {host_path} {mp_norm} "
            f"none bind,rw,create=dir,idmap=container 0 0"
        )

    if spec.autostart:
        lines.append("lxc.start.autostart = 1")

    return "\n".join(lines) + "\n"


def write_conf(vm_dir: Path, spec) -> None:
    """写出 lxc 配置（machines/<name>/config）"""
    from core.vm import bridge

    ok = subuid_range()
    if not ok:
        raise RuntimeError("缺少 subuid/subgid，无法生成容器配置")

    start, count = ok
    text = conf_text(
        spec, vm_dir / ROOTFS_DIR_NAME, bridge.BRIDGE, start, count
    )
    (vm_dir / LXC_CONF_NAME).write_text(text, encoding="utf-8")


# ─────────────────────────────────────────
# rootfs 播种（替代 cloud-init）
# ─────────────────────────────────────────
def seed_rootfs(
    rootfs: Path,
    *,
    hostname: str,
    username: str,
    ssh_pubkey: str,
    password_hash: str,
    uid: int = 1000,
) -> tuple[bool, str]:
    """解包后的 rootfs 做一次性配置（纯文件操作，无需 chroot）

    1. hostname / hosts
    2. sshd 公钥注入 root 与默认用户
    3. 默认用户（名字 = 宿主用户名）写 passwd/shadow
    4. 密码哈希（改密码功能直接改 shadow 行）
    """
    try:
        # 1. hostname / hosts
        (rootfs / "etc" / "hostname").write_text(hostname + "\n",
                                                 encoding="utf-8")
        hosts = rootfs / "etc" / "hosts"
        base = hosts.read_text(encoding="utf-8") if hosts.exists() else ""
        if f"\t{hostname}" not in base:
            hosts.write_text(
                base.rstrip("\n")
                + f"\n127.0.1.1\t{hostname}\n",
                encoding="utf-8",
            )

        # 2. ssh 公钥（root + 用户）
        for owner, home, name_uid in (
            ("root", "root", 0),
            (username, f"home/{username}", uid),
        ):
            ssh_dir = rootfs / home / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            ak = ssh_dir / "authorized_keys"
            existing = ak.read_text(encoding="utf-8") if ak.exists() else ""
            if ssh_pubkey.strip() and ssh_pubkey.strip() not in existing:
                ak.write_text(
                    existing.rstrip("\n") + "\n" + ssh_pubkey.strip() + "\n",
                    encoding="utf-8",
                )
            if ak.exists():
                ak.chmod(0o600)
            _chown_tree(ssh_dir, name_uid)

        # 3. 默认用户写 passwd/shadow
        _ensure_user(rootfs, username, uid, password_hash)

        # 4. home 目录存在（骨架由发行版 rootfs 提供，没有就建）
        home = rootfs / "home" / username
        home.mkdir(parents=True, exist_ok=True)
        _chown_tree(home, uid)

        return True, ""
    except OSError as e:
        return False, str(e)


def _ensure_user(rootfs: Path, username: str, uid: int,
                 password_hash: str) -> None:
    passwd = rootfs / "etc" / "passwd"
    shadow = rootfs / "etc" / "shadow"

    # rootfs 意外缺失 passwd/shadow 时补最小骨架，
    # 否则直接 FileNotFoundError —— 创建机器整个失败
    if not passwd.exists():
        passwd.write_text("root:x:0:0:root:/root:/bin/sh\n",
                          encoding="utf-8")
    if not shadow.exists():
        shadow.write_text("root:!:19000:0:99999:7:::\n", encoding="utf-8")

    pw_lines = passwd.read_text(encoding="utf-8").splitlines()
    sh_lines = shadow.read_text(encoding="utf-8").splitlines()

    user_re = username + ":"
    pw_entry = f"{username}:x:{uid}:{uid}::/home/{username}:/bin/sh"
    sh_entry = f"{username}:{password_hash}:19000:0:99999:7:::"

    pw_lines = [l for l in pw_lines if not l.startswith(user_re)]
    pw_lines.append(pw_entry)
    sh_lines = [l for l in sh_lines if not l.startswith(user_re)]
    sh_lines.append(sh_entry)

    passwd.write_text("\n".join(pw_lines) + "\n", encoding="utf-8")
    shadow.write_text("\n".join(sh_lines) + "\n", encoding="utf-8")
    shadow.chmod(0o640)


def set_root_password_hash(rootfs: Path, username: str,
                           password_hash: str) -> tuple[bool, str]:
    """改密码（详情页账户页调用）：只改 shadow 里对应用户那一行"""
    try:
        shadow = rootfs / "etc" / "shadow"
        lines = shadow.read_text(encoding="utf-8").splitlines()
        prefix = username + ":"
        out = []
        hit = False
        for l in lines:
            if l.startswith(prefix):
                fields = l.split(":")
                fields[1] = password_hash
                l = ":".join(fields)
                hit = True
            out.append(l)
        if not hit:
            return False, f"rootfs 里没有用户 {username}"
        shadow.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True, ""
    except OSError as e:
        return False, str(e)


def _chown_tree(path: Path, uid: int) -> None:
    """把树的所有者改成容器内 uid（宿主侧 chmod/chown 尽力而为）"""
    try:
        os.chown(path, uid, uid)
    except OSError:
        pass
    for p in path.rglob("*"):
        try:
            os.chown(p, uid, uid)
        except OSError:
            pass


# ─────────────────────────────────────────
# 生命周期
# ─────────────────────────────────────────
def _lxc(machines_dir: Path, name: str, *args: str,
         timeout: int = 30) -> tuple[bool, str]:
    cmd = ["lxc-" + args[0], "-P", str(machines_dir), "-n", name, *args[1:]]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "lxc 命令超时"
    except OSError as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]
    return True, r.stdout.strip()


def is_running(machines_dir: Path, name: str) -> bool:
    ok, out = _lxc(machines_dir, name, "info", timeout=10)
    return ok and "RUNNING" in out


def uptime_seconds(machines_dir: Path, name: str) -> int:
    """容器主进程的运行秒数（拿不到返回 0）"""
    ok, out = _lxc(machines_dir, name, "info", timeout=10)
    if not ok:
        return 0
    for line in out.splitlines():
        if line.lower().startswith("pid:"):
            try:
                pid = int(line.split(":")[1].strip())
                with open(f"/proc/{pid}/stat", "rb") as f:
                    starttime = float(f.read().split()[21])
                return int(time.monotonic()
                           - starttime / os.sysconf("SC_CLK_TCK"))
            except (OSError, ValueError, IndexError):
                return 0
    return 0


def start(machines_dir: Path, name: str) -> tuple[bool, str]:
    if is_running(machines_dir, name):
        return True, ""
    return _lxc(machines_dir, name, "start", "-d", timeout=60)


def stop(machines_dir: Path, name: str, timeout: int = 30) -> tuple[bool, str]:
    """停止容器。

    lxc-stop 默认先发 SIGPWR 等待优雅退出，超时或加 -k 直接杀。
    对用户来说容器 stop 即「真休眠」：状态保留在 rootfs，
    再次 start 毫秒级恢复。
    """
    if not is_running(machines_dir, name):
        return True, ""
    ok, err = _lxc(machines_dir, name, "stop", timeout=timeout)
    if ok:
        return True, ""
    # 优雅停止失败，强制杀（等价拔电源）
    ok2, err2 = _lxc(machines_dir, name, "stop", "-k", timeout=15)
    if ok2:
        return True, ""
    return False, err or err2


def kill(machines_dir: Path, name: str) -> tuple[bool, str]:
    """强制停止（删除机器前用）"""
    if not is_running(machines_dir, name):
        return True, ""
    return _lxc(machines_dir, name, "stop", "-k", timeout=15)


def attach(machines_dir: Path, name: str, command: str,
           timeout: int = 120) -> tuple[bool, str]:
    """在容器内执行命令（首次配置装 sshd 等用）"""
    return _lxc(machines_dir, name, "attach", "--", "/bin/sh", "-c",
                command, timeout=timeout)


def ensure_sshd(
    machines_dir: Path, name: str, rootfs: Path, family: str,
    on_log=None,
) -> tuple[bool, str]:
    """确保容器内有 sshd 并已启用

    发行版 rootfs 不一定预装 openssh。检查 rootfs 里的 sshd
    二进制，缺就 lxc-attach 用对应包管理器装并启用。
    """
    log = on_log or (lambda _m: None)

    sbin = rootfs / "usr" / "sbin" / "sshd"
    if sbin.exists():
        return True, ""

    log("容器内安装 sshd…")
    if family == "alpine":
        cmd = "apk add openssh && rc-update add sshd default"
    elif family in ("debian",):
        cmd = ("DEBIAN_FRONTEND=noninteractive apt-get update "
               "&& apt-get install -y openssh-server "
               "&& systemctl enable ssh")
    elif family in ("fedora", "rocky", "rhel", "suse"):
        cmd = "dnf install -y openssh-server && systemctl enable sshd"
    elif family == "arch":
        cmd = "pacman -Sy --noconfirm openssh && systemctl enable sshd"
    else:
        cmd = "dnf install -y openssh-server && systemctl enable sshd"

    ok, err = attach(machines_dir, name, cmd, timeout=300)
    if not ok:
        return False, f"容器内安装 sshd 失败: {err}"
    return True, ""
