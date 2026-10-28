"""SSH 连接与文件传输

用系统 ssh/scp 命令（不引入 paramiko 依赖），通过专属密钥免密登录。

用途：
  · 在 VM 内执行命令
  · 宿主 ⇄ VM 文件传输
  · 剪贴板互通（读 VM 文件到剪贴板，或反向写入）
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

# SSH 公共参数（禁用交互、忽略 host key）
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",           # 绝不提示输入密码
]

SCP_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
]


def ssh_available() -> bool:
    return bool(shutil.which("ssh"))


def scp_available() -> bool:
    return bool(shutil.which("scp"))


# ─────────────────────────────────────────
# 命令执行
# ─────────────────────────────────────────
def run_command(
    vm,
    command: str,
    *,
    timeout: int = 30,
    check: bool = False,
) -> tuple[bool, str]:
    """在 VM 内执行命令

    返回 (成功, 输出)
    """
    if not ssh_available():
        return False, "未找到 ssh 命令"

    port = vm.ssh_port
    if not port:
        return False, "该虚拟机未配置 SSH 端口"

    if not vm.ssh_key.exists():
        return False, "SSH 密钥不存在"

    cmd = [
        "ssh",
        "-i", str(vm.ssh_key),
        "-p", str(port),
        *SSH_OPTS,
        f"{vm.spec.username}@127.0.0.1",
        command,
    ]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"命令超时（{timeout}s）"
    except OSError as e:
        return False, str(e)

    output = (r.stdout or "") + (r.stderr or "")
    ok = r.returncode == 0

    if check and not ok:
        return False, output.strip() or f"退出码 {r.returncode}"

    return ok, output.strip()


def test_connection(vm, timeout: int = 10) -> tuple[bool, str]:
    """测试 SSH 是否可连接"""
    ok, out = run_command(vm, "echo ok", timeout=timeout)
    if ok and "ok" in out:
        return True, ""
    return False, out or "连接失败"


# ─────────────────────────────────────────
# 文件传输
# ─────────────────────────────────────────
def upload_file(
    vm, local_path: str | Path, remote_path: str,
    timeout: int = 120,
) -> tuple[bool, str]:
    """上传文件到 VM"""
    if not scp_available():
        return False, "未找到 scp 命令"

    local = Path(local_path)
    if not local.exists():
        return False, f"本地文件不存在: {local}"

    port = vm.ssh_port
    if not port:
        return False, "未配置 SSH 端口"

    cmd = [
        "scp",
        "-i", str(vm.ssh_key),
        "-P", str(port),
        *SCP_OPTS,
        str(local),
        f"{vm.spec.username}@127.0.0.1:{remote_path}",
    ]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "传输超时"
    except OSError as e:
        return False, str(e)

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]
    return True, ""


def download_file(
    vm, remote_path: str, local_path: str | Path,
    timeout: int = 120,
) -> tuple[bool, str]:
    """从 VM 下载文件"""
    if not scp_available():
        return False, "未找到 scp 命令"

    port = vm.ssh_port
    if not port:
        return False, "未配置 SSH 端口"

    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "scp",
        "-i", str(vm.ssh_key),
        "-P", str(port),
        *SCP_OPTS,
        f"{vm.spec.username}@127.0.0.1:{remote_path}",
        str(local),
    ]

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "传输超时"
    except OSError as e:
        return False, str(e)

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]
    return True, ""


# ─────────────────────────────────────────
# 剪贴板互通
# ─────────────────────────────────────────
def read_remote_file(vm, remote_path: str, max_bytes: int = 4 * 1024 * 1024) -> tuple[bool, str]:
    """读取 VM 内文件内容（用于复制到宿主机剪贴板）"""
    # 先检查大小，避免把超大文件读进内存
    ok, size_str = run_command(
        vm, f"stat -c %s {shlex.quote(remote_path)} 2>/dev/null || echo 0",
        timeout=15,
    )
    if ok:
        try:
            size = int(size_str.strip().splitlines()[-1])
            if size > max_bytes:
                return False, f"文件过大（{size / 1024**2:.1f} MB > {max_bytes / 1024**2:.0f} MB）"
        except (ValueError, IndexError):
            pass

    ok, content = run_command(
        vm, f"cat {shlex.quote(remote_path)}", timeout=30
    )
    if not ok:
        return False, content
    return True, content


def write_remote_file(vm, remote_path: str, content: str) -> tuple[bool, str]:
    """把内容写入 VM 内文件（用于从宿主机剪贴板粘贴）"""
    # 用 base64 避免引号和特殊字符问题
    import base64

    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = (
        f"mkdir -p $(dirname {shlex.quote(remote_path)}) && "
        f"echo {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)}"
    )
    ok, out = run_command(vm, cmd, timeout=60)
    return ok, out


# ─────────────────────────────────────────
# 系统信息
# ─────────────────────────────────────────
def get_system_info(vm) -> dict:
    """获取 VM 基本信息（发行版、内核、内存等）"""
    info = {}

    ok, out = run_command(
        vm,
        "cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|ID)=' || echo 'ID=unknown'",
        timeout=15,
    )
    if ok:
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip().strip('"')

    ok, out = run_command(vm, "uname -r", timeout=10)
    if ok:
        info["kernel"] = out.strip()

    ok, out = run_command(
        vm, "free -m | awk 'NR==2{print $2}'", timeout=10
    )
    if ok:
        info["mem_total_mb"] = out.strip()

    ok, out = run_command(vm, "nproc", timeout=10)
    if ok:
        info["cpus"] = out.strip()

    ok, out = run_command(
        vm, "df -h / | awk 'NR==2{print $2, $3, $4}'", timeout=10
    )
    if ok:
        info["disk"] = out.strip()

    return info


def get_ip(vm) -> str:
    """获取 VM 在 NAT 网络中的 IP"""
    ok, out = run_command(
        vm,
        "ip -4 addr show scope global | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}' | head -1",
        timeout=10,
    )
    return out.strip() if ok else ""


# ─────────────────────────────────────────
# 挂载共享目录（9p）
# ─────────────────────────────────────────
def mount_share(vm, tag: str, mount_point: str) -> tuple[bool, str]:
    """在 VM 内挂载共享目录

    优先 virtiofs（新方案），失败回退 9p（老内核）。

    为什么需要回退：
    Rocky / RHEL 10 内核没有 9p 模块，只能 virtiofs；
    而很老的发行版可能没有 virtiofs。两条都试，谁通用谁。

    msize 262144（256KB）：9p 默认 8KB 会让大文件传输频繁分片。
    """
    mp = shlex.quote(mount_point)

    # 已挂载就跳过（幂等 —— 每次启动都会调一遍）
    check = (
        f"sudo mkdir -p {mp}; "
        f"if grep -qs ' {mount_point} ' /proc/mounts; then "
        f"echo ALREADY_MOUNTED; exit 0; fi; "
    )

    # 1) virtiofs
    vfs_cmd = (
        check
        + "sudo modprobe virtiofs 2>/dev/null; "
        + f"sudo mount -t virtiofs {shlex.quote(tag)} {mp} 2>/dev/null "
        + "&& echo MOUNT_OK_VIRTIOFS && exit 0; "
    )

    # 2) 9p 回退（需要完整的模块链：文件系统 + 网络 + virtio 传输）
    p9_cmd = (
        "sudo modprobe 9p 2>/dev/null; "
        "sudo modprobe 9pnet 2>/dev/null; "
        "sudo modprobe 9pnet_virtio 2>/dev/null; "
        f"sudo mount -t 9p "
        f"-o trans=virtio,version=9p2000.L,msize=262144 "
        f"{shlex.quote(tag)} {mp} 2>&1 "
        "&& echo MOUNT_OK_9P && exit 0; "
        "echo MOUNT_FAILED"
    )

    ok, out = run_command(vm, vfs_cmd + p9_cmd, timeout=40)
    text = out or ""
    if "MOUNT_OK_VIRTIOFS" in text or "MOUNT_OK_9P" in text:
        return True, ""
    if "ALREADY_MOUNTED" in text:
        return True, ""

    # 给出能直接看懂的原因
    if "unknown filesystem type" in text:
        return False, (
            "该发行版内核既不支持 virtiofs 也不支持 9p。\n"
            "共享目录不可用，请改用「文件」页的上传/下载。"
        )
    return False, text.strip()[:300] or "挂载失败"


def unmount_share(vm, tag: str, mount_point: str | None = None) -> tuple[bool, str]:
    """卸载 VM 内的 9p 共享

    删除共享目录时调用，避免留下悬空的挂载点
    （后续访问会报 "Stale file handle"）。
    """
    mp = mount_point or f"/mnt/host/{tag}"
    cmd = (
        f"sudo umount {shlex.quote(mp)} 2>/dev/null; "
        f"sudo rmdir {shlex.quote(mp)} 2>/dev/null; "
        f"echo UM_OK"
    )
    ok, out = run_command(vm, cmd, timeout=20)
    return ok and "UM_OK" in (out or ""), out


def unmount_all_shares(vm) -> list[tuple[str, bool, str]]:
    """卸载 VM 内所有共享目录

    休眠前调用。必须真的卸干净（而不只是丢掉引用），
    否则 guest 的 dentry/inode 缓存会跨快照残留，
    恢复后读到错乱的文件。
    """
    results: list[tuple[str, bool, str]] = []
    for _host, tag in vm.spec.shares:
        try:
            ok, msg = unmount_share(vm, tag)
            results.append((tag, ok, msg or ""))
        except Exception as e:
            results.append((tag, False, str(e)))
    return results


def mounted_tags(vm) -> set[str]:
    """查询 VM 内已挂载的 9p tag 集合

    读 /proc/mounts 里 type=9p 的条目，第二列是挂载点，
    第一列是 tag。用于在界面上显示「已挂载 / 未挂载」。
    """
    # 必须同时匹配 virtiofs 和 9p：
    # 现在主用 virtiofs，只查 9p 会让状态一直显示「未挂载」，
    # 而实际已经挂上了 —— 用户看到的就是「挂载成功但显示未挂载」。
    ok, out = run_command(
        vm,
        "awk '$3==\"9p\" || $3==\"virtiofs\" {print $1\" \"$2}' "
        "/proc/mounts 2>/dev/null",
        timeout=15,
    )
    if not ok:
        return set()

    tags = set()
    for line in (out or "").splitlines():
        parts = line.split()
        if parts:
            tags.add(parts[0])
    return tags


def share_states(vm) -> dict[str, bool]:
    """每个已配置共享目录的挂载状态：{tag: 是否已挂载}"""
    live = mounted_tags(vm)
    return {tag: (tag in live) for _host, tag in vm.spec.shares}


def mount_all_shares(vm) -> list[tuple[str, bool, str]]:
    """挂载所有共享目录到 /mnt/host/<tag>

    **这是共享目录功能的关键一环**：QEMU 用 -virtfs 把宿主机目录
    暴露成 9p 设备，但 guest 内还需要主动 mount 才能访问。
    之前没有任何地方调用本函数，导致用户配好共享目录、重启 VM 后
    /mnt/host/ 下依然什么都没有。

    调用时机：VM 启动且 SSH 就绪之后。
    """
    results = []
    for _host_path, tag in vm.spec.shares:
        ok, msg = mount_share(vm, tag, f"/mnt/host/{tag}")
        results.append((tag, ok, msg))
    return results
