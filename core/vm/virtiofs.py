"""virtiofs 共享目录后端

为什么用 virtiofs 而不是 9p
──────────────────────────
9p 是上一代方案，正在被淘汰：

  · Rocky / RHEL 10 的内核**完全移除了 9p 模块**
    （`modprobe 9p` 报 "Module 9p not found"，
     `dnf` 仓库里也没有任何包提供）
  · Alpine 3.22 还支持，但 9p 本身有已知的权限与性能问题

virtiofs 是它的官方继任者，实测支持情况：

    Alpine 3.22   → virtiofs ✓   9p ✓
    Rocky 10      → virtiofs ✓   9p ✗

所以统一走 virtiofs，不再依赖 9p。

工作方式
────────
每个共享目录配一个 virtiofsd 守护进程（QEMU 自带 `/usr/lib/virtiofsd`），
通过 unix socket 与 QEMU 的 vhost-user-fs 设备通信：

    [virtiofsd] --shared-dir /home/john/下载 --socket-path <vm_dir>/vfs-download.sock
          ↕ unix socket
    [QEMU] -chardev socket,id=vfs0,path=... -device vhost-user-fs-pci,chardev=vfs0,tag=download
          ↕ virtio
    [guest] mount -t virtiofs download /mnt/host/download

生命周期
────────
· VM 启动前：为每个共享目录拉起 virtiofsd
· VM 停止后：杀掉这些进程（避免残留）
· 进程崩溃：下次启动时自动重建
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

# virtiofsd 的常见安装位置（不同发行版/包管理器不一致）
_DAEMON_CANDIDATES = (
    "/usr/lib/virtiofsd",
    "/usr/libexec/virtiofsd",
    "/usr/bin/virtiofsd",
    "/usr/local/bin/virtiofsd",
)


def virtiofsd_path() -> str | None:
    """找到 virtiofsd 可执行文件"""
    for p in _DAEMON_CANDIDATES:
        if os.access(p, os.X_OK):
            return p
    return shutil.which("virtiofsd")


def available() -> bool:
    """宿主机是否具备 virtiofs 能力"""
    return virtiofsd_path() is not None


def socket_path(vm_dir: Path, tag: str) -> Path:
    """每个共享目录一个独立的 socket 文件"""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
    return Path(vm_dir) / f"vfs-{safe}.sock"


def pid_path(vm_dir: Path, tag: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tag)
    return Path(vm_dir) / f"vfs-{safe}.pid"


# ─────────────────────────────────────────
# 守护进程管理
# ─────────────────────────────────────────
def read_pid(vm_dir: Path, tag: str) -> int | None:
    p = pid_path(vm_dir, tag)
    if not p.exists():
        return None
    try:
        pid = int(p.read_text().strip())
    except (ValueError, OSError):
        return None
    if pid <= 0 or not Path(f"/proc/{pid}").exists():
        return None
    return pid


def is_running(vm_dir: Path, tag: str) -> bool:
    """该共享目录的 virtiofsd 是否在跑"""
    pid = read_pid(vm_dir, tag)
    if pid is None:
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"virtiofsd" in cmdline


def start_daemon(vm_dir: Path, host_path: str, tag: str) -> tuple[bool, str]:
    """为单个共享目录启动 virtiofsd

    返回 (成功, 错误信息)。已运行则直接返回成功（幂等）。
    """
    exe = virtiofsd_path()
    if exe is None:
        return False, "宿主机未安装 virtiofsd"

    host = Path(host_path).expanduser()
    if not host.is_dir():
        return False, f"目录不存在: {host_path}"

    if is_running(vm_dir, tag):
        return True, ""

    sock = socket_path(vm_dir, tag)
    try:
        sock.unlink(missing_ok=True)
    except OSError:
        pass

    # 参数说明：
    #   --sandbox none
    #       不需要 user namespace 权限，否则普通用户启动会失败。
    #   --inode-file-handles=never
    #       virtiofs 的 file handle 优化需要 CAP_DAC_READ_SEARCH
    #       （一般要 root）。普通用户跑时 virtiofsd 会先警告
    #       "Failed to open file handle ... Operation not permitted"，
    #       然后自己降级；但 QEMU 仍会按协议请求 file handle，
    #       导致 "HandleRequest(InvalidParam)" 并断开连接 ——
    #       表现为 VM 内 mount 一直卡住。
    #       显式禁用后双方协商一致，功能正常（只是少了这点优化）。
    cmd = [
        exe,
        "--shared-dir", str(host),
        "--socket-path", str(sock),
        "--sandbox", "none",
        "--inode-file-handles=never",
    ]

    log_path = Path(vm_dir) / f"vfs-{tag}.log"
    try:
        logf = open(log_path, "w")
    except OSError:
        logf = subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            cmd, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as e:
        try:
            if logf is not subprocess.DEVNULL:
                logf.close()
        except Exception:
            pass
        return False, str(e)
    finally:
        try:
            if logf is not subprocess.DEVNULL:
                logf.close()
        except Exception:
            pass

    # 等 socket 出现（QEMU 需要它）
    deadline = time.time() + 8
    while time.time() < deadline:
        if proc.poll() is not None:
            err = ""
            try:
                err = log_path.read_text(errors="replace")[-300:]
            except OSError:
                pass
            return False, f"virtiofsd 启动失败: {err.strip()[:200]}"
        if sock.exists():
            try:
                pid_path(vm_dir, tag).write_text(str(proc.pid))
            except OSError:
                pass
            return True, ""
        time.sleep(0.1)

    try:
        proc.kill()
    except OSError:
        pass
    return False, "virtiofsd 未在 8 秒内就绪"


def stop_daemon(vm_dir: Path, tag: str) -> None:
    """停止单个共享目录的 virtiofsd"""
    pid = read_pid(vm_dir, tag)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        # 给它 2 秒优雅退出，否则强杀
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

    for p in (pid_path(vm_dir, tag), socket_path(vm_dir, tag)):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def stop_all(vm_dir: Path) -> int:
    """停止该 VM 的所有 virtiofsd（VM 停止时调用）

    返回清理的进程数。
    """
    vm_dir = Path(vm_dir)
    count = 0
    for pid_file in vm_dir.glob("vfs-*.pid"):
        tag = pid_file.stem[4:]        # 去掉 "vfs-" 前缀
        if is_running(vm_dir, tag):
            count += 1
        stop_daemon(vm_dir, tag)
    # 兜底：清掉没有 pid 文件的残留 socket
    for sock in vm_dir.glob("vfs-*.sock"):
        try:
            sock.unlink(missing_ok=True)
        except OSError:
            pass
    return count


def ensure_all(vm_dir: Path, shares: list[tuple[str, str]]) -> list[str]:
    """为所有共享目录确保守护进程在跑

    返回启动失败的描述列表（空表示全部就绪）。
    """
    errors: list[str] = []
    for host_path, tag in shares:
        ok, err = start_daemon(vm_dir, host_path, tag)
        if not ok:
            errors.append(f"{tag}: {err}")
    return errors


# ─────────────────────────────────────────
# QEMU 参数
# ─────────────────────────────────────────
def qemu_args(vm_dir: Path, shares: list[tuple[str, str]]) -> list[str]:
    """生成 vhost-user-fs 相关的 QEMU 参数

    调用前必须先用 ensure_all() 把守护进程拉起来，
    否则 QEMU 连不上 socket 会启动失败。
    """
    args: list[str] = []
    for i, (_host_path, tag) in enumerate(shares):
        sock = socket_path(vm_dir, tag)
        args += [
            "-chardev", f"socket,id=vfschar{i},path={sock}",
            "-device",
            f"vhost-user-fs-pci,chardev=vfschar{i},tag={tag}",
        ]
    return args
