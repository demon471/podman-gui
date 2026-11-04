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


def acl_ok() -> bool:
    """家目录是否已给容器 root（subuid 段）开放穿越权限

    /home/<user> 通常是 0700，subuid 用户无法到达放在下面的
    rootfs（报 "Could not access /home/<user>"）。用 getfacl
    检查 ACL 里的 subuid x 位；getfacl 不存在时放行
    （无法判断就别挡路，真缺了启动会报错并带提示）。
    """
    got = subuid_range()
    if not got:
        return True
    try:
        r = subprocess.run(
            ["getfacl", "-p", str(Path.home())],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True
    if r.returncode != 0:
        return True
    return f"user:{got[0]}" in r.stdout


def usernet_ok() -> bool:
    """/etc/lxc/lxc-usernet 是否有当前用户的 veth 配额

    缺失时 lxc-user-nic 直接失败（"Failed to open
    lxc-usernet"）。
    """
    got = subuid_range()
    if not got:
        return True
    user = os.environ.get("USER") or Path.home().name
    return _usernet_check(Path("/etc/lxc/lxc-usernet"), user=user)


def _usernet_check(path: Path, user: str) -> bool:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == user and parts[1] == "veth":
                return True
    except OSError:
        return False
    return False


def pending_fixups() -> list[str]:
    """还缺哪些系统前置（用于生成一次性初始化提示）"""
    missing = []
    if not acl_ok():
        missing.append("家目录 ACL（容器 root 无法穿越 /home）")
    if not usernet_ok():
        missing.append("lxc-usernet 配额（非特权容器无法创建网络）")
    return missing


def prerequisites() -> tuple[bool, str]:
    """创建/启动前的依赖检查，返回 (是否通过, 错误说明)

    三项系统前置（lxc、subuid、ACL/usernet）里，ACL 与
    usernet 由「启用共享网络」的一次性初始化顺带配置
    （见 bridge._setup_script 第 5 段），所以缺失时提示
    去那里修复而不是让用户手敲命令。
    """
    ver = lxc_version()
    if not ver:
        return False, "LXC 未安装（需要 lxc 包），请在设置页安装"
    if not subuid_range():
        return False, "当前用户没有 subuid/subgid 条目，无法使用非特权容器"

    missing = pending_fixups()
    if missing:
        return False, (
            "还差一次性系统配置："
            + "；".join(missing)
            + "。请到 设置 → 服务 → 虚拟机网络 执行「启用共享网络」。"
        )
    return True, ""


# ─────────────────────────────────────────
# 配置生成
# ─────────────────────────────────────────
def vm_mac(name: str) -> str:
    """按机器名生成稳定 MAC（00:16:3e 是 Xensource/KVM 常用 OUI）"""
    import zlib

    h = zlib.crc32((name or "").encode("utf-8"))
    return "00:16:3e:{:02x}:{:02x}:{:02x}".format(
        (h >> 16) & 0xFF, (h >> 8) & 0xFF, h & 0xFF
    )


def conf_mac(vm_dir: Path) -> str:
    """从机器的 lxc 配置里读出固定 MAC（没有返回空串）"""
    conf = vm_dir / LXC_CONF_NAME
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("lxc.net.0.hwaddr"):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def conf_text(
    spec,
    rootfs: Path,
    bridge_name: str,
    subuid_start: int,
    subuid_count: int,
    host_uid: int | None = None,
) -> str:
    """生成 lxc 配置文件内容

    spec 只用到 name/hostname/shares/autostart（鸭子类型，
    传 VMSpec 或测试桩都行）。

    **idmap 首段必须映射到宿主用户自己**（host_uid）。
    原因：rootfs 由普通用户解包，文件属主就是宿主用户，
    而普通用户无权 chown 到 subuid 段（内核不允许在初始
    user namespace 里改属主到子 userns 的映射段，实测
    "Operation not permitted"，且很容易被静默吞掉）。
    把容器 root(0) 映射到宿主 uid 后，rootfs 属主天然正确，
    容器内就是 root 且能写 /etc，无需任何 chown。
    其余 id 走 subuid 段。
    """
    if host_uid is None:
        host_uid = os.getuid()

    lines = [
        "# 由 PodBox 生成，重建机器时会覆盖",
        "lxc.include = /usr/share/lxc/config/common.conf",
        f"lxc.uts.name = {spec.hostname or spec.name}",
        f"lxc.arch = {spec.arch}",
        # 容器 root → 宿主用户自己（rootfs 属主无需改动）
        f"lxc.idmap = u 0 {host_uid} 1",
        f"lxc.idmap = g 0 {host_uid} 1",
        # 其余 id → subuid 段
        f"lxc.idmap = u 1 {subuid_start} {max(subuid_count - 1, 1)}",
        f"lxc.idmap = g 1 {subuid_start} {max(subuid_count - 1, 1)}",
        # rootfs
        f"lxc.rootfs.path = dir:{rootfs}",
        # 网络：挂在共享网桥上，DHCP 由 pgbr0 的 dnsmasq 发放
        "lxc.net.0.type = veth",
        f"lxc.net.0.link = {bridge_name}",
        "lxc.net.0.name = eth0",
        "lxc.net.0.flags = up",
        # 固定 MAC：lxc 默认每次启动随机生成，DHCP 每次拿新 IP、
        # 租约文件无法与机器对应起来。按名字生成稳定 MAC 后，
        # 租约按 MAC 查就永远准（Alpine 的 dhcpcd 不发 hostname，
        # 按主机名匹配租约实测全是 "*"，根本匹配不到）。
        "lxc.net.0.hwaddr = " + vm_mac(spec.name),
    ]

    # 共享目录：bind mount，启动即挂（无需 VM 档的 virtiofs）。
    #
    # **不能加 idmap=container**：lxc 对 bind mount 做 idmap 需要
    # 在子 userns 里有 CAP_SYS_ADMIN，而我们的 idmap 把容器 root
    # 映射到宿主 uid（rootfs 属主方案，见上），实测报
    # "Operation not permitted - Failed to idmap detached mount"，
    # 容器直接起不来。
    #
    # 不加 idmap 也没问题：宿主文件的 uid 1000 在容器内显示为
    # root（0 → 宿主 1000 的映射），容器内 root 可读写。
    for host_path, mp in spec.shares:
        mp_norm = mp.strip("/")
        lines.append(
            f"lxc.mount.entry = {host_path} {mp_norm} "
            f"none bind,rw,create=dir 0 0"
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
        spec, vm_dir / ROOTFS_DIR_NAME, bridge.BRIDGE, start, count,
        host_uid=os.getuid(),
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
        #
        # 属主必须精确设置：sshd 的 StrictModes 会检查家目录和
        # .ssh 的所有者，属主不对就静默拒绝公钥（表现为
        # "Permission denied (publickey)"，日志里看不出原因）。
        # 而且 .ssh 目录权限必须是 700，authorized_keys 600。
        for owner, home, name_uid in (
            ("root", "root", 0),
            (username, f"home/{username}", uid),
        ):
            home_dir = rootfs / home
            home_dir.mkdir(parents=True, exist_ok=True)

            ssh_dir = home_dir / ".ssh"
            ssh_dir.mkdir(parents=True, exist_ok=True)
            ak = ssh_dir / "authorized_keys"
            existing = ak.read_text(encoding="utf-8") if ak.exists() else ""
            if ssh_pubkey.strip() and ssh_pubkey.strip() not in existing:
                ak.write_text(
                    existing.rstrip("\n") + "\n" + ssh_pubkey.strip() + "\n",
                    encoding="utf-8",
                )
            # chown 必须在**所有写操作之后**：普通用户一旦把文件
            # 交给 uid 1000，自己就再也写不进去了（EACCES）。
            _chown_tree(ssh_dir, name_uid)
            if ak.exists():
                ak.chmod(0o600)
            ssh_dir.chmod(0o700)
            _chown_tree(home_dir, name_uid)

        # 3. 默认用户写 passwd/shadow
        _ensure_user(rootfs, username, uid, password_hash)

        # 4. home 目录属主（sshd StrictModes 要求；放最后因为
        #    chown 之后普通用户就不能再改了）
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


def userns_map_args(subuid_start: int | None = None,
                    subuid_count: int | None = None) -> list[str]:
    """构造 lxc-usernsexec 的 idmap 参数

    与容器配置里的 idmap 保持一致：
      容器 uid 0    → 宿主用户自己（host_uid）
      容器 uid 1..  → subuid 段

    这样在命名空间里 chown 到容器内 uid N，落到宿主就是
    正确的那一个 uid；宿主用户在初始命名空间里做不到这件事
    （内核禁止改属主到子 userns 的映射段）。
    """
    if subuid_start is None or subuid_count is None:
        got = subuid_range()
        if not got:
            return []
        subuid_start, subuid_count = got

    me = os.getuid()
    rest = max(subuid_count - 1, 1)
    return [
        "-m", "u:0:{}:1".format(me),
        "-m", "u:1:{}:{}".format(subuid_start, rest),
        "-m", "g:0:{}:1".format(me),
        "-m", "g:1:{}:{}".format(subuid_start, rest),
    ]


def userns_run(args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """在映射了 subuid 段的用户命名空间里执行命令

    用于宿主用户本身做不到的两件事：
      · 把文件 chown 到容器内的某个 uid（落到 subuid 段）
      · 删除已经属于 subuid 段的文件（否则 EACCES，删不掉）
    """
    m = userns_map_args()
    if not m:
        return False, "缺少 subuid/subgid，无法进入用户命名空间"

    try:
        r = subprocess.run(
            ["lxc-usernsexec", *m, "--", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "usernsexec 超时"
    except OSError as e:
        return False, str(e)

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]
    return True, r.stdout.strip()


def chown_to_container_uid(path: Path, cont_uid: int,
                           cont_gid: int | None = None,
                           timeout: int = 180) -> tuple[bool, str]:
    """把 host 路径的属主设成「容器内 uid cont_uid」"""
    gid = cont_uid if cont_gid is None else cont_gid
    return userns_run(
        ["chown", "-R", "{}:{}".format(cont_uid, gid), str(path)],
        timeout=timeout,
    )


def rm_in_userns(path: Path, timeout: int = 180) -> tuple[bool, str]:
    """在用户命名空间里递归删除（能删掉 subuid 属主的文件）"""
    return userns_run(["rm", "-rf", str(path)], timeout=timeout)


def _chown_tree(path: Path, _uid: int = 0) -> None:
    """把树的所有者设为**宿主用户自己**

    普通用户无权 chown 到 subuid 段（内核不允许在初始 user
    namespace 里改属主到子 userns 的映射段）。而 idmap 把容器
    root(0) 映射到宿主用户自己，所以只要文件归宿主用户，
    容器内看到的就是 root 属主 —— sshd StrictModes 恰好接受
    「属主 == 用户自己或 root」，公钥认证因此可用。

    参数 _uid 仅为兼容旧调用点保留，不参与计算。
    """
    me = os.getuid()
    try:
        os.chown(path, me, me)
    except OSError:
        pass
    for p in path.rglob("*"):
        try:
            os.chown(p, me, me)
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
        # lxc_drop_groups 的警告在宿主用户属组较多时必然出现，
        # 且不影响命令执行（退出码仍是 0）；真出错时它会和
        # 真实错误一起出现，这里只滤掉这一行噪音。
        err = "\n".join(
            l for l in (r.stderr or r.stdout).splitlines()
            if "lxc_drop_groups" not in l
        ).strip()
        return False, (err or r.stdout.strip())[:300]
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
    """启动容器（带一次重试）

    stop 之后立刻 start 会碰到 veth 还没被内核清干净的竞态
    （lxc-start 报 ABORTING）。间隔 1.5s 重试一次，实测足够
    覆盖；两次都失败才是真失败。
    """
    if is_running(machines_dir, name):
        return True, ""

    ok, err = _lxc(machines_dir, name, "start", "-d", timeout=60)
    if ok:
        return True, ""

    time.sleep(1.5)
    ok2, err2 = _lxc(machines_dir, name, "start", "-d", timeout=60)
    if ok2:
        return True, ""
    return False, err2 or err


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
    """在容器内执行命令（首次配置装 sshd 等用）

    必须显式设 PATH：lxc-attach 会继承宿主的 PATH，宿主的
    /usr/bin 等路径在发行版 rootfs 里可能不存在，导致
    "apk: not found" 这类假失败。
    """
    wrapped = (
        "export PATH=/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:"
        "/usr/local/bin; " + command
    )
    return _lxc(machines_dir, name, "attach", "--", "/bin/sh", "-c",
                wrapped, timeout=timeout)


def fix_home_ownership(
    rootfs: Path, username: str, uid: int = 1000,
) -> tuple[bool, str]:
    """把用户家目录的属主改成容器内 uid

    **必须在用户命名空间里 chown**（lxc-usernsexec）：宿主侧
    普通用户无权把文件改到 subuid 段 —— 内核不允许在初始
    user namespace 里改属主到子 userns 的映射段。

    sshd 的 StrictModes 要求家目录属于该用户，否则静默拒绝
    公钥 —— 表现为 "Permission denied (publickey)"，日志里只有
    "penalty: failed authentication"，极难定位。
    """
    home = rootfs / "home" / username
    if not home.exists():
        return False, "家目录不存在"

    # chown 和 chmod 都要在用户命名空间里做：文件一旦归属
    # subuid 段，宿主用户在自己的命名空间里连 chmod 也会
    # EPERM（"Operation not permitted"）。
    home_s = str(home)
    cmd = (
        "chown -R " + str(uid) + ":" + str(uid) + " " + home_s + " && "
        "chmod 755 " + home_s + " && "
        "chmod 700 " + home_s + "/.ssh && "
        "chmod 600 " + home_s + "/.ssh/authorized_keys"
    )
    ok, err = userns_run(["sh", "-c", cmd])
    if not ok:
        return False, err
    return True, ""


def ensure_sshd(
    machines_dir: Path, name: str, rootfs: Path, family: str,
    username: str = "",
    on_log=None,
) -> tuple[bool, str]:
    """确保容器内有 sshd 并已启用

    发行版 rootfs 不一定预装 openssh。检查 rootfs 里的 sshd
    二进制，缺就 lxc-attach 用对应包管理器装并启用。
    最后修正家目录属主（sshd StrictModes 的硬要求）。
    """
    log = on_log or (lambda _m: None)

    sbin = rootfs / "usr" / "sbin" / "sshd"
    if sbin.exists():
        if username:
            fix_home_ownership(rootfs, username)
        return True, ""

    log("容器内安装 sshd…")
    if family == "alpine":
        # Alpine 默认源在境外，实测经常超时（"temporary error"）。
        # 换国内镜像再装，否则首次启动会卡住甚至失败。
        # sshd 装在 /usr/sbin；Alpine 用 openrc 管理服务。
        cmd = (
            "M=https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.22; "
            'echo "$M/main" > /etc/apk/repositories; '
            'echo "$M/community" >> /etc/apk/repositories; '
            "apk add --no-cache openssh && "
            "ssh-keygen -A && "
            "rc-update add sshd default 2>/dev/null; "
            "rc-service sshd start 2>/dev/null || /usr/sbin/sshd"
        )
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

    # 镜像源瞬时失败很常见（"temporary error (try again later)"），
    # 重试 3 次、间隔递增，比让用户手动重启机器体验好得多。
    last_err = ""
    for attempt in range(3):
        ok, err = attach(machines_dir, name, cmd, timeout=300)
        if ok:
            break
        last_err = err
        log(f"安装 sshd 失败（第 {attempt + 1}/3 次）：{err[:120]}")
        time.sleep(2 + attempt * 3)
    else:
        return False, f"容器内安装 sshd 失败: {last_err}"

    # 装完再修家目录属主（rootfs 解包出来的都是 root 属主）
    if username:
        ok2, err2 = fix_home_ownership(rootfs, username)
        if not ok2:
            return False, f"修正家目录属主失败: {err2}"
    return True, ""
