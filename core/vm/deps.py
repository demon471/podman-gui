"""虚拟机依赖检测与安装引导

检测 QEMU 相关工具是否可用，缺失时给出对应发行版的安装命令。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Dependency:
    """单个依赖项"""

    key: str
    name: str
    # 检测方式：可执行文件名 或 文件路径
    check: str
    kind: str = "binary"        # binary | file | kvm
    required: bool = True
    purpose: str = ""
    # 各发行版的包名
    packages: dict[str, str] = field(default_factory=dict)


# ─────────────────────────────────────────
# 依赖清单
# ─────────────────────────────────────────
DEPENDENCIES: list[Dependency] = [
    Dependency(
        key="qemu",
        name="QEMU 系统模拟器",
        check="qemu-system-x86_64",
        purpose="运行虚拟机（必需）",
        packages={
            "arch": "qemu-base",
            "debian": "qemu-system-x86",
            "fedora": "qemu-kvm",
        },
    ),
    Dependency(
        key="qemu-img",
        name="QEMU 镜像工具",
        check="qemu-img",
        purpose="创建/转换 qcow2 磁盘（必需）",
        packages={
            "arch": "qemu-img",
            "debian": "qemu-utils",
            "fedora": "qemu-img",
        },
    ),
    Dependency(
        key="kvm",
        name="KVM 硬件加速",
        check="/dev/kvm",
        kind="kvm",
        purpose="硬件虚拟化，缺失时性能下降 10 倍以上",
        packages={
            "arch": "（内核内置，需 BIOS 开启 VT-x/AMD-V）",
            "debian": "（内核内置，需 BIOS 开启 VT-x/AMD-V）",
            "fedora": "（内核内置，需 BIOS 开启 VT-x/AMD-V）",
        },
    ),
    Dependency(
        key="xorriso",
        name="ISO 生成工具",
        check="xorriso",
        purpose="生成 cloud-init seed 镜像",
        packages={
            "arch": "libisoburn",
            "debian": "xorriso",
            "fedora": "xorriso",
        },
    ),
    Dependency(
        key="ssh",
        name="SSH 客户端",
        check="ssh",
        purpose="连接虚拟机、传输文件",
        packages={
            "arch": "openssh",
            "debian": "openssh-client",
            "fedora": "openssh-clients",
        },
    ),
    Dependency(
        key="ssh-keygen",
        name="SSH 密钥工具",
        check="ssh-keygen",
        purpose="生成虚拟机专属密钥对",
        packages={
            "arch": "openssh",
            "debian": "openssh-client",
            "fedora": "openssh-clients",
        },
    ),
    Dependency(
        key="scp",
        name="SCP 传输",
        check="scp",
        purpose="在宿主与虚拟机间复制文件",
        packages={
            "arch": "openssh",
            "debian": "openssh-client",
            "fedora": "openssh-clients",
        },
    ),
]


# ─────────────────────────────────────────
# 发行版识别
# ─────────────────────────────────────────
def detect_distro() -> str:
    """识别发行版家族

    返回: arch | debian | fedora | unknown
    """
    # Arch 系
    if Path("/etc/arch-release").exists():
        return "arch"

    # Debian 系
    if Path("/etc/debian_version").exists():
        return "debian"

    # Fedora / RHEL 系
    for f in ("/etc/fedora-release", "/etc/redhat-release", "/etc/centos-release"):
        if Path(f).exists():
            return "fedora"

    # 回退：读 os-release
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
        ids = ""
        for line in text.splitlines():
            if line.startswith(("ID=", "ID_LIKE=")):
                ids += " " + line.split("=", 1)[1].strip().strip('"')
        ids = ids.lower()
        if "arch" in ids:
            return "arch"
        if "debian" in ids or "ubuntu" in ids:
            return "debian"
        if "fedora" in ids or "rhel" in ids or "centos" in ids:
            return "fedora"
    except OSError:
        pass

    return "unknown"


DISTRO_LABELS = {
    "arch": "Arch / CachyOS / Manjaro",
    "debian": "Debian / Ubuntu / Mint",
    "fedora": "Fedora / RHEL / Rocky / Alma",
    "unknown": "未知发行版",
}


def install_command(distro: str, packages: list[str]) -> str:
    """生成安装命令"""
    pkgs = " ".join(dict.fromkeys(packages))     # 去重保序

    if distro == "arch":
        return f"sudo pacman -S --needed {pkgs}"
    if distro == "debian":
        return f"sudo apt install -y {pkgs}"
    if distro == "fedora":
        return f"sudo dnf install -y {pkgs}"
    return ""


# ─────────────────────────────────────────
# 检测
# ─────────────────────────────────────────
@dataclass
class CheckResult:
    dep: Dependency
    ok: bool
    detail: str = ""


def _check_one(dep: Dependency) -> CheckResult:
    """检测单个依赖"""
    if dep.kind == "kvm":
        path = Path(dep.check)
        if not path.exists():
            return CheckResult(dep, False, "未找到 /dev/kvm")

        # 检查当前用户是否可访问
        if not os.access(dep.check, os.R_OK | os.W_OK):
            return CheckResult(
                dep, False,
                f"无访问权限（需加入 kvm 组：sudo usermod -aG kvm $USER）"
            )

        # 检查 CPU 虚拟化标志
        try:
            cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
            if "vmx" not in cpuinfo and "svm" not in cpuinfo:
                return CheckResult(dep, False, "CPU 未开启虚拟化（BIOS 中启用 VT-x/AMD-V）")
        except OSError:
            pass

        return CheckResult(dep, True, "硬件加速可用")

    if dep.kind == "file":
        return CheckResult(dep, Path(dep.check).exists())

    # binary
    path = shutil.which(dep.check)
    if not path:
        return CheckResult(dep, False, "未安装")

    # 尝试取版本（部分工具不支持 --version，静默跳过）
    version = ""
    try:
        r = subprocess.run(
            [path, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            first = (r.stdout or "").strip().splitlines()
            if first:
                version = first[0][:60]
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 版本取不到不影响可用性判断
    return CheckResult(dep, True, version or "已安装")


def check_all() -> list[CheckResult]:
    """检测全部依赖"""
    return [_check_one(d) for d in DEPENDENCIES]


def missing_packages(results: list[CheckResult], distro: str) -> list[str]:
    """从检测结果提取需要安装的包名"""
    pkgs = []
    for r in results:
        if r.ok:
            continue
        pkg = r.dep.packages.get(distro, "")
        # 跳过纯说明性条目（括号开头）
        if pkg and not pkg.startswith(("（", "(")):
            pkgs.append(pkg)
    return pkgs


def ready_for_vm(results: list[CheckResult] | None = None) -> bool:
    """是否可以创建虚拟机（必需项全部就绪）"""
    if results is None:
        results = check_all()
    return all(r.ok for r in results if r.dep.required)


def qemu_version() -> str:
    """获取 QEMU 版本（用于显示）"""
    path = shutil.which("qemu-system-x86_64")
    if not path:
        return ""
    try:
        r = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=5
        )
        line = (r.stdout or "").strip().splitlines()
        if line:
            # "QEMU emulator version 11.1.2" → "11.1.2"
            parts = line[0].split()
            for i, p in enumerate(parts):
                if p == "version" and i + 1 < len(parts):
                    return parts[i + 1]
        return line[0] if line else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def host_arch() -> str:
    """宿主机架构（用于选择镜像）"""
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "amd64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    return m
