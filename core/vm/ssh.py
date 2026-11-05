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


def _ssh_target(vm) -> str:
    """SSH 连到哪台机器

    共享网桥模式下 guest 有自己的 IP（10.99.x.x），宿主机能直接
    访问；这种模式下必须连那个 IP —— 网桥没有 hostfwd，
    连 127.0.0.1 会打到宿主机自己的 22 端口上
    （Connection refused），SSH、文件页、共享挂载全会失效。
    SLIRP 模式下才走 127.0.0.1 的转发端口。
    """
    host = getattr(vm, "ssh_host", "")
    return host or "127.0.0.1"


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
        f"{vm.spec.username}@{_ssh_target(vm)}",
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
        f"{vm.spec.username}@{_ssh_target(vm)}:{remote_path}",
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
        f"{vm.spec.username}@{_ssh_target(vm)}:{remote_path}",
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
# 共享目录（容器 bind mount）
# ─────────────────────────────────────────
def share_mount_point(tag: str) -> str:
    """共享目录在机器内的挂载点 = /<tag>

    lxc.mount.entry 的目标写的是相对 rootfs 的 <tag>，
    机器内看到的就是 /<tag>。旧 VM 档（9p/virtiofs）用
    /mnt/host/<tag>，已随 QEMU 删除 —— 界面曾因此显示错路径。
    """
    return "/" + tag.strip("/")


def mount_share(vm, tag: str, mount_point: str | None = None) -> tuple[bool, str]:
    """确认共享目录已挂载

    容器档的共享目录是 lxc bind mount：lxc-start 时自动挂载，
    **机器内不需要（也不能）手动 mount**。这里只做验证 ——
    挂载点出现在 /proc/mounts 即已挂载；没有说明配置是在
    启动后才加的，需要重启机器生效。
    """
    mp = mount_point or share_mount_point(tag)
    ok, out = run_command(
        vm,
        f"grep -qs ' {mp} ' /proc/mounts && echo MOUNTED || echo ABSENT",
        timeout=15,
    )
    if ok and "MOUNTED" in out:
        return True, ""
    return False, "未挂载（新加的共享目录需重启机器生效）"


def unmount_share(vm, tag: str,
                  mount_point: str | None = None) -> tuple[bool, str]:
    """卸载机器内的共享目录

    容器 bind mount 的卸载只影响当前这次运行；配置（lxc.mount.entry）
    已在 remove_share 里删掉，下次启动自然不再挂。这里 umount
    只是让「删除后立刻不可见」，失败也不阻塞。
    """
    mp = mount_point or share_mount_point(tag)
    run_command(
        vm, f"sudo umount {shlex.quote(mp)} 2>/dev/null; true", timeout=20
    )
    return True, ""


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
    """查询已挂载的共享目录 tag 集合

    容器档是 bind mount，文件系统类型是 btrfs/ext4 等宿主
    文件系统 —— 按类型（9p/virtiofs）查永远查不到，界面上
    就是「明明挂了却显示未挂载」。改为拉全部挂载点，
    在本地按 /<tag> 反查 tag。
    """
    ok, out = run_command(
        vm,
        "awk '{print $2}' /proc/mounts 2>/dev/null",
        timeout=15,
    )
    if not ok:
        return set()

    points = {line.strip() for line in (out or "").splitlines() if line.strip()}
    return {
        tag.strip("/")
        for tag in (t for _h, t in getattr(vm.spec, "shares", []) or [])
        if share_mount_point(tag) in points
    }


def share_states(vm) -> dict[str, bool]:
    """每个已配置共享目录的挂载状态：{tag: 是否已挂载}"""
    live = mounted_tags(vm)
    return {tag: (tag in live) for _host, tag in vm.spec.shares}


def mount_all_shares(vm) -> list[tuple[str, bool, str]]:
    """验证所有共享目录的挂载状态

    容器档的 bind mount 由 lxc-start 自动完成，这里逐个确认
    （新加的配置在重启前会返回未挂载，界面据此提示）。
    """
    results = []
    for _host_path, tag in vm.spec.shares:
        ok, msg = mount_share(vm, tag, share_mount_point(tag))
        results.append((tag, ok, msg))
    return results
