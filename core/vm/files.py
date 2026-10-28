"""虚拟机文件浏览（SFTP 协议）

用 paramiko 走 SFTP 浏览 VM 内文件系统。
如果没装 paramiko，退回用 ssh 命令 + ls 解析（能力受限但能用）。

为什么用 SFTP 而不是 9p
──────────────────────
9p 只能看到显式共享出去的目录，且需要 VM 内手动 mount。
SFTP 走已有的 SSH 通道，能看到整个文件系统，零配置。
"""

from __future__ import annotations

import shlex
import stat as stat_mod
from dataclasses import dataclass, field
from pathlib import Path

# paramiko 可选：装了走 SFTP，没装退回命令行
try:
    import paramiko

    HAS_PARAMIKO = True
except ImportError:
    HAS_PARAMIKO = False


@dataclass
class RemoteEntry:
    """远程文件/目录项"""

    name: str
    path: str
    is_dir: bool
    size: int = 0
    mode: int = 0
    mtime: float = 0.0
    is_link: bool = False

    @property
    def size_text(self) -> str:
        if self.is_dir:
            return "—"
        return _fmt_size(self.size)

    @property
    def perms_text(self) -> str:
        if not self.mode:
            return "—"
        return stat_mod.filemode(self.mode)


def _shq(s: str) -> str:
    """shell 单引号转义"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


# ─────────────────────────────────────────
# SFTP 后端
# ─────────────────────────────────────────
class SftpSession:
    """一个 SFTP 连接（上下文管理器）

    用同一连接做多次操作，避免每次列目录都重连
    （SSH 握手约 300ms，浏览时体感明显）。
    """

    def __init__(self, vm):
        self.vm = vm
        self._client: "paramiko.SSHClient | None" = None
        self._sftp = None

    def __enter__(self) -> "SftpSession":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        """建立 SFTP 连接（幂等）"""
        if self._sftp is not None:
            return

        if not HAS_PARAMIKO:
            raise RuntimeError("未安装 paramiko，无法使用 SFTP")

        port = self.vm.ssh_port
        if not port:
            raise RuntimeError("该虚拟机未配置 SSH 端口")

        key = self.vm.ssh_key
        if not key.exists():
            raise RuntimeError("SSH 密钥不存在")

        client = paramiko.SSHClient()
        # VM 是新建的临时机器，主机密钥每次都变，不校验
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # 重试几次：VM 刚从内存快照恢复时 sshd 可能还没完全就绪，
        # 或宿主机端口代理正在交接，此时会读到 "Error reading SSH
        # protocol banner" 或直接连接被拒。
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                client.connect(
                    hostname="127.0.0.1",
                    port=int(port),
                    username=self.vm.spec.username,
                    key_filename=str(key),
                    timeout=10,
                    banner_timeout=20,
                    auth_timeout=20,
                    allow_agent=False,
                    look_for_keys=False,
                )
                self._client = client
                self._sftp = client.open_sftp()
                return
            except (paramiko.SSHException, OSError) as e:
                last_err = e
                try:
                    client.close()
                except Exception:
                    pass
                if attempt < 3:
                    import time as _t

                    _t.sleep(1.5 * (attempt + 1))
                    client = paramiko.SSHClient()
                    client.set_missing_host_key_policy(
                        paramiko.AutoAddPolicy()
                    )

        raise RuntimeError(f"SFTP 连接失败: {last_err}")

    def _sudo_run(self, command: str) -> None:
        """用 sudo 执行一条命令（SFTP 权限不足时的兜底）

        SFTP 协议本身没有提权概念 —— 它就以登录用户身份操作。
        遇到系统目录时只能走 SSH + sudo。
        """
        from core.vm import sudo as vm_sudo

        ok, out = vm_sudo.run(self.vm, command, timeout=60)
        if not ok:
            # 把 sudo 的失败原因抛出去，让 UI 能显示
            raise PermissionError(out or "操作失败（需要 root 权限）")

    def close(self) -> None:
        for obj in (self._sftp, self._client):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._sftp = None
        self._client = None

    # ── 操作 ──
    def ensure_home_writable(self) -> tuple[bool, str]:
        """确保 home 目录属于当前用户且可写

        云镜像（cloudimg）常见问题：cloud-init 创建用户时没来得及
        修正 /home/<user> 的属主，导致它归 root 所有，普通用户
        无法在里面创建文件 —— 表现为「上传/新建目录」全部失败，
        但浏览还能用（读权限是有的）。

        这里在连接后自动检查一次，发现不对就用 sudo 修掉，
        用户不需要理解发生了什么。
        """
        try:
            home = self.home()
        except Exception:
            return False, ""

        # 用 stat 看属主
        try:
            st = self._sftp.stat(home)
        except Exception:
            return False, ""

        uid = getattr(st, "st_uid", None)
        if uid is None:
            return False, ""

        # 与登录用户比对（SFTP 侧拿到的是数字 uid）
        # 简单可靠的做法：直接尝试在 home 下建临时目录
        probe = home.rstrip("/") + "/.podman-gui-write-probe"
        try:
            self._sftp.mkdir(probe)
            try:
                self._sftp.rmdir(probe)
            except Exception:
                pass
            return True, "可写"
        except IOError:
            pass
        except Exception:
            pass

        # 不可写 → 用统一 sudo 层修复（会自动处理免密/密码/一次性提升）
        from core.vm import sudo as vm_sudo

        user = self.vm.spec.username
        ok1, _ = vm_sudo.chown(self.vm, home, f"{user}:{user}")
        ok2, _ = vm_sudo.chmod(self.vm, home, "u+rwx")

        # 修复后重新验证
        try:
            self._sftp.mkdir(probe)
            self._sftp.rmdir(probe)
            return True, "已修复 home 目录权限"
        except Exception:
            pass

        if ok1 and ok2:
            return True, "已修复 home 目录权限"
        return False, "home 目录不可写，且无法自动修复（需要 VM 内 root 权限）"

    def listdir(self, path: str) -> list[RemoteEntry]:
        """列目录（目录在前，各自按名称排序）"""
        self.connect()

        entries: list[RemoteEntry] = []
        for attr in self._sftp.listdir_attr(path):
            full = f"{path.rstrip('/')}/{attr.filename}"
            is_link = stat_mod.S_ISLNK(attr.st_mode or 0)
            entries.append(RemoteEntry(
                name=attr.filename,
                path=full,
                is_dir=stat_mod.S_ISDIR(attr.st_mode or 0),
                size=attr.st_size or 0,
                mode=attr.st_mode or 0,
                mtime=attr.st_mtime or 0.0,
                is_link=is_link,
            ))

        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def home(self) -> str:
        """取登录用户的家目录"""
        self.connect()
        try:
            return self._sftp.normalize(".")
        except Exception:
            return f"/home/{self.vm.spec.username}"

    def mkdir(self, path: str) -> None:
        self.connect()
        try:
            self._sftp.mkdir(path)
        except PermissionError:
            # SFTP 以普通用户身份运行，写系统目录必然被拒。
            # 有 sudo 权限时用命令行兜底 —— 否则用户会看到
            # 「授权成功了但还是建不了」。
            self._sudo_run(f"mkdir -p {_shq(path)}")

    def remove(self, path: str) -> None:
        """删除文件"""
        self.connect()
        try:
            self._sftp.remove(path)
        except PermissionError:
            self._sudo_run(f"rm -f {_shq(path)}")

    def rmdir(self, path: str) -> None:
        self.connect()
        try:
            self._sftp.rmdir(path)
        except PermissionError:
            self._sudo_run(f"rmdir {_shq(path)}")

    def rename(self, old: str, new: str) -> None:
        self.connect()
        try:
            self._sftp.rename(old, new)
        except PermissionError:
            self._sudo_run(f"mv {_shq(old)} {_shq(new)}")

    def get(self, remote: str, local: str) -> None:
        """下载"""
        self.connect()
        self._sftp.get(remote, local)

    def put(self, local: str, remote: str) -> None:
        """上传（目标不可写时用 sudo 兜底）"""
        self.connect()
        try:
            self._sftp.put(local, remote)
        except (PermissionError, IOError) as e:
            if "denied" not in str(e).lower():
                raise
            # 先传到用户可写的中转位置，再 sudo mv 过去
            import tempfile

            tmp = f"/tmp/.podman-gui-up-{Path(remote).name}"
            self._sftp.put(local, tmp)
            self._sudo_run(f"mv -f {_shq(tmp)} {_shq(remote)}")

    def read_text(self, path: str, max_bytes: int = 512 * 1024) -> str:
        """读文本文件（超长截断）"""
        self.connect()
        with self._sftp.open(path, "r") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")

    def write_text(self, path: str, content: str) -> None:
        self.connect()
        with self._sftp.open(path, "w") as f:
            f.write(content.encode("utf-8"))


# ─────────────────────────────────────────
# 命令行后端（paramiko 不可用时的兜底）
# ─────────────────────────────────────────
def listdir_cli(vm, path: str) -> list[RemoteEntry]:
    """用 ssh + ls 列目录（兜底方案）

    解析 `ls -la --time-style=+%s` 的输出。
    只支持基本列目录，不支持上传下载。
    """
    from core.vm import ssh as vm_ssh

    cmd = f"ls -la --time-style=+%s {shlex.quote(path)} 2>/dev/null"
    ok, out = vm_ssh.run_command(vm, cmd, timeout=15)
    if not ok:
        return []

    entries: list[RemoteEntry] = []
    for line in out.splitlines():
        if line.startswith("total") or not line.strip():
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue

        perms, _links, _owner, _group, size, mtime, name = parts
        if name in (".", ".."):
            continue

        is_link = perms.startswith("l")
        real_name = name.split(" -> ")[0] if is_link else name

        try:
            mode = _perms_to_mode(perms)
        except Exception:
            mode = 0

        entries.append(RemoteEntry(
            name=real_name,
            path=f"{path.rstrip('/')}/{real_name}",
            is_dir=perms.startswith("d"),
            size=int(size) if size.isdigit() else 0,
            mode=mode,
            mtime=float(mtime) if mtime.replace(".", "").isdigit() else 0.0,
            is_link=is_link,
        ))

    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def _perms_to_mode(perms: str) -> int:
    """`drwxr-xr-x` → 整数 mode"""
    if len(perms) < 10:
        return 0
    mode = 0
    # 文件类型
    type_map = {"d": stat_mod.S_IFDIR, "l": stat_mod.S_IFLNK,
                "-": stat_mod.S_IFREG}
    mode |= type_map.get(perms[0], stat_mod.S_IFREG)
    # 权限位
    for i, ch in enumerate(perms[1:10]):
        if ch in "rwxst":
            mode |= 1 << (8 - i)
    return mode
