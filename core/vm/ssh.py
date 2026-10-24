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
    """在 VM 内挂载 9p 共享目录

    msize 用 262144（256KB）：默认 8KB 会在大文件传输时频繁分片，
    提升后拷贝大文件快数倍。version=9p2000.L 是 Linux 客户端专用，
    支持 symlink / 硬链接 / 扩展属性。
    """
    cmd = (
        f"sudo mkdir -p {shlex.quote(mount_point)} && "
        f"sudo modprobe 9pnet_virtio 2>/dev/null; "
        f"sudo mount -t 9p "
        f"-o trans=virtio,version=9p2000.L,msize=262144 "
        f"{shlex.quote(tag)} {shlex.quote(mount_point)}"
    )
    return run_command(vm, cmd, timeout=30)


def mount_all_shares(vm) -> list[tuple[str, bool, str]]:
    """挂载所有共享目录到 /mnt/host/<tag>

    与详情对话框里展示的路径保持一致。
    """
    results = []
    for _host_path, tag in vm.spec.shares:
        ok, msg = mount_share(vm, tag, f"/mnt/host/{tag}")
        results.append((tag, ok, msg))
    return results
