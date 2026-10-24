"""QEMU 进程管理

负责：
  · 构建 qemu-system-x86_64 命令行
  · 启动/停止/强制终止 VM
  · 读取运行状态（PID、运行时长）
  · 解析串口日志

网络方案（重要）
    本机只有无线网卡（wlan0），无法做网桥，因此使用 QEMU
    用户态网络（SLIRP）：
      · hostfwd 把宿主机端口转发到 VM
      · VM 内访问宿主机用 10.0.2.2
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# QEMU 二进制名（按架构）
QEMU_BINARIES = {
    "amd64": "qemu-system-x86_64",
    "arm64": "qemu-system-aarch64",
}

# 默认机器类型
MACHINE_TYPES = {
    "amd64": "q35",
    "arm64": "virt",
}

# SLIRP 网关（VM 内看到的宿主机地址）
HOST_GATEWAY = "10.0.2.2"

# UEFI 固件候选路径（Alpine/部分发行版的 cloud image 必须 UEFI 启动）
OVMF_CANDIDATES = [
    "/usr/share/edk2/x64/OVMF_CODE.4m.fd",
    "/usr/share/edk2/x64/OVMF_CODE.fd",
    "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd",
    "/usr/share/ovmf/x64/OVMF_CODE.fd",
    "/usr/share/OVMF/OVMF_CODE.fd",
]


def find_ovmf() -> str | None:
    """找到 OVMF UEFI 固件"""
    for p in OVMF_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def needs_uefi(spec: "VMSpec") -> bool:
    """判断该 VM 是否需要 UEFI 启动

    Alpine 的 nocloud cloud image 是 UEFI-only（BIOS 无法启动）。
    """
    from core.vm import images

    img = images.get_spec(spec.image_key)
    if img is None:
        return False
    # Alpine 的 cloud image 全部是 UEFI
    return img.family == "alpine"


@dataclass
class PortForward:
    """端口转发规则：宿主机端口 → VM 端口"""

    host_port: int
    guest_port: int
    protocol: str = "tcp"          # tcp | udp
    host_addr: str = "127.0.0.1"   # 绑定地址，0.0.0.0 表示局域网可访问

    def to_qemu(self) -> str:
        return (
            f"hostfwd={self.protocol}:{self.host_addr}:{self.host_port}"
            f"-:{self.guest_port}"
        )

    def display(self) -> str:
        return f"{self.host_addr}:{self.host_port} → VM:{self.guest_port}/{self.protocol}"


@dataclass
class VMSpec:
    """虚拟机运行配置（持久化到 vm.json）"""

    name: str
    image_key: str
    arch: str = "amd64"
    memory_mb: int = 2048
    cpus: int = 2
    disk_gb: int = 20
    username: str = "dev"
    hostname: str = ""
    # 端口转发
    forwards: list[PortForward] = field(default_factory=list)
    # 共享目录：宿主机路径 → VM 内挂载点
    shares: list[tuple[str, str]] = field(default_factory=list)
    # 是否开机自启（随应用启动）
    autostart: bool = False
    # 创建时间
    created_at: float = 0.0

    def __post_init__(self):
        if not self.hostname:
            self.hostname = self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "image_key": self.image_key,
            "arch": self.arch,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "disk_gb": self.disk_gb,
            "username": self.username,
            "hostname": self.hostname,
            "forwards": [
                {
                    "host_port": f.host_port,
                    "guest_port": f.guest_port,
                    "protocol": f.protocol,
                    "host_addr": f.host_addr,
                }
                for f in self.forwards
            ],
            "shares": [list(s) for s in self.shares],
            "autostart": self.autostart,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VMSpec":
        return cls(
            name=d["name"],
            image_key=d["image_key"],
            arch=d.get("arch", "amd64"),
            memory_mb=d.get("memory_mb", 2048),
            cpus=d.get("cpus", 2),
            disk_gb=d.get("disk_gb", 20),
            username=d.get("username", "dev"),
            hostname=d.get("hostname", d["name"]),
            forwards=[
                PortForward(
                    host_port=f["host_port"],
                    guest_port=f["guest_port"],
                    protocol=f.get("protocol", "tcp"),
                    host_addr=f.get("host_addr", "127.0.0.1"),
                )
                for f in d.get("forwards", [])
            ],
            shares=[tuple(s) for s in d.get("shares", [])],
            autostart=d.get("autostart", False),
            created_at=d.get("created_at", 0.0),
        )


# ─────────────────────────────────────────
# 命令行构建
# ─────────────────────────────────────────
def qemu_binary(arch: str = "amd64") -> str | None:
    """获取 QEMU 二进制路径"""
    name = QEMU_BINARIES.get(arch)
    return shutil.which(name) if name else None


def has_kvm() -> bool:
    """KVM 是否可用"""
    p = Path("/dev/kvm")
    return p.exists() and os.access(p, os.R_OK | os.W_OK)


def build_command(
    spec: VMSpec,
    vm_dir: Path,
    *,
    headless: bool = True,
    vnc_display: int | None = None,
) -> list[str]:
    """构建 QEMU 启动命令

    headless=True 时用串口输出到日志文件（无图形界面）
    vnc_display 指定时额外开启 VNC（:N 端口 5900+N）
    """
    binary = qemu_binary(spec.arch)
    if not binary:
        raise RuntimeError(
            f"未找到 {QEMU_BINARIES.get(spec.arch, 'qemu')}，"
            f"请先安装 QEMU"
        )

    disk = vm_dir / "disk.qcow2"
    seed = vm_dir / "seed.iso"
    console_log = vm_dir / "console.log"
    pid_file = vm_dir / "qemu.pid"
    monitor = vm_dir / "monitor.sock"

    cmd: list[str] = [
        binary,
        "-name", spec.name,
        # mem-merge=on 让 KSM 可以合并该 VM 的内存页
        "-machine", f"{MACHINE_TYPES.get(spec.arch, 'q35')},mem-merge=on",
        "-m", str(spec.memory_mb),
        "-smp", str(spec.cpus),
    ]

    # ── CPU / 加速 ──
    if has_kvm():
        cmd += ["-enable-kvm", "-cpu", "host"]
    else:
        # 无 KVM 时用软件模拟（很慢，仅用于测试）
        cmd += ["-cpu", "max"]

    # ── UEFI 固件（Alpine 等必须）──
    if needs_uefi(spec):
        ovmf = find_ovmf()
        if not ovmf:
            raise RuntimeError(
                "该镜像需要 UEFI 启动，但未找到 OVMF 固件。\n"
                "请安装：sudo pacman -S edk2-ovmf"
            )
        # 每个 VM 独立的 NVRAM（保存 UEFI 变量）
        nvram = vm_dir / "efi_vars.fd"
        if not nvram.exists():
            # 从模板复制一份可写副本
            import shutil as _sh
            template = ovmf.replace("CODE", "VARS")
            if Path(template).exists():
                _sh.copy2(template, nvram)
            else:
                # 无模板时创建空白（4MB）
                with open(nvram, "wb") as f:
                    f.write(b"\x00" * (4 * 1024 * 1024))

        cmd += [
            "-drive", f"if=pflash,format=raw,readonly=on,file={ovmf}",
            "-drive", f"if=pflash,format=raw,file={nvram}",
        ]

    # ── 系统盘 ──
    # 必须用 -blockdev + -device virtio-blk-pci 而不是 -drive if=virtio，
    # 因为只有 -device 形式能指定 bootindex。
    #
    # 背景：启用 9p 共享（-virtfs）后，QEMU 会自动插入 virtio-9p-pci 设备，
    # 改变 PCI 设备枚举顺序，导致 OVMF 的启动项失效、回落到 PXE 网络启动。
    # 给磁盘显式 bootindex=1、seed bootindex=2 后启动顺序即恢复确定。
    cmd += [
        "-blockdev", f"driver=file,node-name=disk_file,filename={disk}",
        "-blockdev", "driver=qcow2,node-name=disk_disk,file=disk_file",
        "-device", "virtio-blk-pci,drive=disk_disk,bootindex=1",
    ]

    # ── cloud-init seed ──
    if seed.exists():
        cmd += [
            "-blockdev",
            f"driver=file,node-name=seed_file,read-only=on,"
            f"filename={seed}",
            "-blockdev",
            "driver=raw,node-name=seed_disk,file=seed_file,read-only=on",
            "-device", "virtio-blk-pci,drive=seed_disk,bootindex=2",
        ]

    # ── 内存气球（动态回收空闲内存）──
    # free-page-reporting 让 guest 主动报告空闲页，宿主机可回收
    cmd += [
        "-device",
        "virtio-balloon-pci,id=balloon0,free-page-reporting=on",
    ]

    # ── 网络 ──
    netdev_parts = ["user", "id=net0"]
    for fwd in spec.forwards:
        netdev_parts.append(fwd.to_qemu())
    cmd += [
        "-netdev", ",".join(netdev_parts),
        "-device", "virtio-net-pci,netdev=net0",
    ]

    # ── 共享目录（9p）──
    # 用 passthrough 而非 mapped-xattr：
    #   mapped-xattr 会把 VM 内用户映射到宿主机某 UID，导致 VM 内
    #   非 root 用户写宿主机文件时 Permission denied。
    #   passthrough 让 VM 内直接以宿主机 UID/GID 访问，
    #   宿主机与 VM 双向读写都正常（需 VM 内 UID 与宿主机一致时才最直观）。
    for i, (host_path, mount_tag) in enumerate(spec.shares):
        if not Path(host_path).is_dir():
            continue
        cmd += [
            "-virtfs",
            f"local,path={host_path},mount_tag={mount_tag},"
            f"security_model=none,id=fsdev{i}",
        ]
        # security_model 三种取值的区别（实测结论）：
        #   mapped-xattr（默认）→ VM 内 UID 被映射到宿主机未占用 UID，
        #     VM 内非 root 用户写宿主机文件 Permission denied。
        #   passthrough        → VM 内客户端创建文件时 QEMU 会 chown 成
        #     VM 内 UID；VM 内 UID 与宿主机不一致时 QEMU 无 CAP_CHOWN，
        #     报 EPERM（Operation not permitted），连 root 都写不了。
        #   none               → 不做 UID 映射、不 chown，文件统一归
        #     运行 QEMU 的宿主机用户。读写都正常，是唯一跨发行版通用的选择。
        #
        # 宿主机侧仍放开目录权限，避免 QEMU 用户无写权限的场景。
        try:
            os.chmod(host_path, 0o777)
        except OSError:
            pass

    # ── 显示 ──
    if vnc_display is not None:
        cmd += ["-vnc", f"127.0.0.1:{vnc_display}"]
        cmd += ["-device", "virtio-vga"]
    if headless:
        # 无图形界面时彻底禁用显卡，省内存
        cmd += ["-display", "none", "-vga", "none"]
        cmd += ["-serial", f"file:{console_log}"]
        # 让内核输出到串口（cloud image 默认 ttyS0）
        cmd += ["-append", "console=ttyS0,115200n8"] if False else []
        # 注：cloud image 用 GRUB/systemd-boot，不能直接 -append
        # 串口输出依赖镜像自身的 console 配置

    # ── 进程管理 ──
    cmd += [
        "-pidfile", str(pid_file),
        "-monitor", f"unix:{monitor},server,nowait",
        "-daemonize",
    ]

    return cmd


# ─────────────────────────────────────────
# 进程状态
# ─────────────────────────────────────────
def read_pid(vm_dir: Path) -> int | None:
    """读取 PID 文件"""
    pid_file = vm_dir / "qemu.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if pid > 0 else None


def is_running(vm_dir: Path) -> bool:
    """VM 是否在运行（校验 PID 对应的进程确实是 qemu）"""
    pid = read_pid(vm_dir)
    if pid is None:
        return False

    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        # 进程已退出，清理残留 PID 文件
        (vm_dir / "qemu.pid").unlink(missing_ok=True)
        return False

    # 校验 cmdline 确实是 qemu（防止 PID 被复用）
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="ignore"
        )
        if "qemu-system" not in cmdline:
            (vm_dir / "qemu.pid").unlink(missing_ok=True)
            return False
    except OSError:
        return False

    return True


def uptime_seconds(vm_dir: Path) -> int:
    """运行时长（秒）"""
    pid = read_pid(vm_dir)
    if pid is None:
        return 0
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return 0
    try:
        stat = (proc / "stat").read_text()
        # 第 22 个字段是启动时间（jiffies since boot）
        fields = stat.rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        boot_time = 0.0
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime"):
                boot_time = float(line.split()[1])
                break
        if boot_time:
            hz = os.sysconf("SC_CLK_TCK")
            started = boot_time + start_ticks / hz
            return max(0, int(time.time() - started))
    except (OSError, ValueError, IndexError):
        pass
    return 0


# ─────────────────────────────────────────
# 启停
# ─────────────────────────────────────────
def start(spec: VMSpec, vm_dir: Path, *, vnc: int | None = None) -> tuple[bool, str]:
    """启动 VM

    返回 (成功, 错误信息)
    """
    if is_running(vm_dir):
        return True, "已在运行"

    disk = vm_dir / "disk.qcow2"
    if not disk.exists():
        return False, f"磁盘不存在: {disk}"

    try:
        cmd = build_command(spec, vm_dir, headless=(vnc is None), vnc_display=vnc)
    except RuntimeError as e:
        return False, str(e)

    # 清空上次日志
    console = vm_dir / "console.log"
    if console.exists():
        console.unlink()

    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "启动超时"
    except OSError as e:
        return False, str(e)

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:500]

    # daemonize 后稍等，确认进程真的起来了
    for _ in range(20):
        if is_running(vm_dir):
            return True, ""
        time.sleep(0.1)

    return False, "进程未启动（查看 console.log）"


def stop(vm_dir: Path, timeout: int = 30) -> tuple[bool, str]:
    """优雅停止（先 ACPI 关机，超时后强制终止）"""
    if not is_running(vm_dir):
        return True, "未在运行"

    pid = read_pid(vm_dir)
    if pid is None:
        return True, "未在运行"

    # 1. 尝试通过 monitor 发送 ACPI 关机
    monitor = vm_dir / "monitor.sock"
    if monitor.exists():
        try:
            import socket

            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(str(monitor))
            s.sendall(b"system_powerdown\n")
            s.close()
        except OSError:
            pass

    # 2. 等待退出
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(vm_dir):
            return True, ""
        time.sleep(0.3)

    # 3. 超时，发 SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, ""
    except OSError as e:
        return False, str(e)

    for _ in range(20):
        if not is_running(vm_dir):
            return True, ""
        time.sleep(0.3)

    # 4. 还活着，SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as e:
        return False, str(e)

    time.sleep(0.5)
    return (not is_running(vm_dir)), "已强制终止"


def kill(vm_dir: Path) -> tuple[bool, str]:
    """强制终止"""
    pid = read_pid(vm_dir)
    if pid is None:
        return True, "未在运行"
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, ""
    except OSError as e:
        return False, str(e)
    time.sleep(0.3)
    (vm_dir / "qemu.pid").unlink(missing_ok=True)
    return True, ""


# ─────────────────────────────────────────
# 日志
# ─────────────────────────────────────────
def read_console(vm_dir: Path, max_bytes: int = 512 * 1024) -> str:
    """读取串口日志（尾部）"""
    log = vm_dir / "console.log"
    if not log.exists():
        return ""
    try:
        size = log.stat().st_size
        with open(log, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def wait_for_ssh(vm_dir: Path, host_port: int, timeout: int = 120) -> bool:
    """等待 SSH 端口可用（VM 启动完成的标志）"""
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(vm_dir):
            return False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect(("127.0.0.1", host_port))
            # 读 SSH banner 确认真的是 SSH
            banner = s.recv(64)
            s.close()
            if b"SSH-" in banner:
                return True
        except (OSError, socket.timeout):
            pass
        time.sleep(1)
    return False
