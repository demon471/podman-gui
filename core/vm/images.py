"""发行版镜像目录与下载

统一使用各发行版的 cloud image（qcow2 格式，已预装 cloud-init）。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.config import ROOT

# 镜像缓存目录
IMAGES_DIR = Path.home() / ".local" / "share" / "podman-gui" / "vm" / "images"


@dataclass
class ImageSpec:
    """发行版镜像规格"""

    key: str                      # 唯一标识，如 ubuntu-24.04
    name: str                     # 显示名
    family: str                   # debian | fedora | alpine | arch | suse
    url_amd64: str = ""
    url_arm64: str = ""
    # cloud-init 默认用户
    default_user: str = "dev"
    # 默认资源
    default_mem_mb: int = 1024
    default_cpus: int = 1
    default_disk_gb: int = 20
    # 磁盘格式
    format: str = "qcow2"
    # 说明
    note: str = ""

    def url(self, arch: str = "amd64") -> str:
        return self.url_arm64 if arch == "arm64" else self.url_amd64


# ─────────────────────────────────────────
# 发行版目录
# ─────────────────────────────────────────
CATALOG: dict[str, ImageSpec] = {
    # ── Debian 系 ──
    "ubuntu-24.04": ImageSpec(
        key="ubuntu-24.04",
        name="Ubuntu 24.04 LTS (Noble)",
        family="debian",
        url_amd64="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        url_arm64="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img",
        default_user="ubuntu",
        note="长期支持，软件生态最全",
    ),
    "ubuntu-22.04": ImageSpec(
        key="ubuntu-22.04",
        name="Ubuntu 22.04 LTS (Jammy)",
        family="debian",
        url_amd64="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        url_arm64="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-arm64.img",
        default_user="ubuntu",
        note="更保守，兼容老项目",
    ),
    "debian-12": ImageSpec(
        key="debian-12",
        name="Debian 12 (Bookworm)",
        family="debian",
        url_amd64="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        url_arm64="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2",
        default_user="debian",
        note="最稳定，适合服务器场景",
    ),

    # ── Fedora 系 ──
    "fedora-41": ImageSpec(
        key="fedora-41",
        name="Fedora 41 Cloud",
        family="fedora",
        url_amd64="https://download.fedoraproject.org/pub/fedora/linux/releases/41/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-41-1.4.x86_64.qcow2",
        url_arm64="https://download.fedoraproject.org/pub/fedora/linux/releases/41/Cloud/aarch64/images/Fedora-Cloud-Base-Generic-41-1.4.aarch64.qcow2",
        default_user="fedora",
        note="最新内核与新特性",
    ),
    "rocky-9": ImageSpec(
        key="rocky-9",
        name="Rocky Linux 9",
        family="fedora",
        url_amd64="https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        url_arm64="https://download.rockylinux.org/pub/rocky/9/images/aarch64/Rocky-9-GenericCloud-Base.latest.aarch64.qcow2",
        default_user="rocky",
        note="RHEL 9 兼容，企业环境常用",
    ),

    # ── Alpine ──
    "alpine-3.20": ImageSpec(
        key="alpine-3.20",
        name="Alpine Linux 3.20",
        family="alpine",
        url_amd64="https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/cloud/nocloud_alpine-3.20.3-x86_64-uefi-cloudinit-r0.qcow2",
        url_arm64="https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/cloud/nocloud_alpine-3.20.3-aarch64-uefi-cloudinit-r0.qcow2",
        default_user="alpine",
        default_mem_mb=256,
        default_disk_gb=8,
        note="极轻量（~50MB 内存），OpenRC 而非 systemd",
    ),

    # ── Arch ──
    "arch": ImageSpec(
        key="arch",
        name="Arch Linux Cloud",
        family="arch",
        url_amd64="https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2",
        default_user="arch",
        note="滚动更新，AUR 生态",
    ),

    # ── openSUSE ──
    "opensuse-15": ImageSpec(
        key="opensuse-15",
        name="openSUSE Leap 15.6",
        family="suse",
        url_amd64="https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2",
        default_user="opensuse",
        note="YaST 管理，企业级",
    ),
}


def list_images(arch: str = "amd64") -> list[ImageSpec]:
    """列出当前架构可用的镜像"""
    out = []
    for spec in CATALOG.values():
        if spec.url(arch):
            out.append(spec)
    return out


def get_spec(key: str) -> ImageSpec | None:
    return CATALOG.get(key)


# ─────────────────────────────────────────
# 下载
# ─────────────────────────────────────────
def image_path(spec: ImageSpec, arch: str = "amd64") -> Path:
    """本地缓存路径"""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGES_DIR / f"{spec.key}-{arch}.qcow2"


def is_downloaded(spec: ImageSpec, arch: str = "amd64") -> bool:
    p = image_path(spec, arch)
    return p.exists() and p.stat().st_size > 1024 * 1024      # > 1MB 视为有效


def download_image(
    spec: ImageSpec,
    arch: str = "amd64",
    on_progress: Callable[[int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """下载镜像

    on_progress(已下载字节, 总字节)
    on_log(消息)
    cancel_flag() 返回 True 时中止
    """
    url = spec.url(arch)
    if not url:
        return False, f"{spec.name} 不支持 {arch} 架构"

    target = image_path(spec, arch)
    tmp = target.with_suffix(".part")

    if on_log:
        on_log(f"开始下载 {spec.name}")
        on_log(f"URL: {url}")
        on_log(f"目标: {target}")

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "podman-gui/1.0"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            done = 0
            chunk = 256 * 1024

            with open(tmp, "wb") as f:
                while True:
                    if cancel_flag and cancel_flag():
                        tmp.unlink(missing_ok=True)
                        return False, "已取消"

                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if on_progress:
                        on_progress(done, total)

    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        return False, f"网络错误: {e.reason}"
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return False, f"写入失败: {e}"

    # 原子替换
    tmp.replace(target)

    if on_log:
        on_log(f"下载完成: {target.stat().st_size / 1024**2:.1f} MB")

    return True, str(target)


def delete_image(spec: ImageSpec, arch: str = "amd64") -> bool:
    """删除缓存的镜像"""
    p = image_path(spec, arch)
    if p.exists():
        p.unlink()
        return True
    return False


def cache_size() -> int:
    """缓存总大小（字节）"""
    if not IMAGES_DIR.exists():
        return 0
    return sum(f.stat().st_size for f in IMAGES_DIR.glob("*.qcow2") if f.is_file())


# ─────────────────────────────────────────
# 校验
# ─────────────────────────────────────────
def sha256_of(path: Path, on_progress=None) -> str:
    """计算文件 SHA256（大文件分块读）"""
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while True:
            buf = f.read(1024 * 1024)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            if on_progress:
                on_progress(done, total)
    return h.hexdigest()


def verify_qcow2(path: Path) -> tuple[bool, str]:
    """用 qemu-img 校验镜像完整性"""
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        return True, "qemu-img 不可用，跳过校验"

    try:
        r = subprocess.run(
            [qemu_img, "check", str(path)],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode == 0:
            return True, "镜像完整"
        return False, (r.stderr or r.stdout).strip()[:300]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
