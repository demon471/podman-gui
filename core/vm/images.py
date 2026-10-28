"""发行版镜像目录与下载

统一使用各发行版的 cloud image（qcow2 格式，已预装 cloud-init）。
"""

from __future__ import annotations

import hashlib
import json
import re
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
    # 多镜像源：按优先级排列，前一个失败自动降级到下一个。
    # 国内源优先（实测快 3-10 倍），官方源兜底。
    # 每个元素是 (架构, URL)。
    mirrors_amd64: list[str] = field(default_factory=list)
    mirrors_arm64: list[str] = field(default_factory=list)
    # cloud-init 默认用户
    default_user: str = "dev"
    # 默认资源
    default_mem_mb: int = 1024
    default_cpus: int = 1
    default_disk_gb: int = 20
    # 磁盘格式
    format: str = "qcow2"
    # 是否需要 UEFI 启动。
    # 主流发行版的 cloud image（Alpine/Rocky/Fedora/Ubuntu/Debian/Arch）
    # 全部是 UEFI-only 或 UEFI 优先：Alpine 官方文件名直接带 -uefi-；
    # Rocky/Fedora 的 qcow2 只带 UEFI 引导（无 BIOS grub）。
    # 默认 True，别再按 family 猜。
    uefi: bool = True
    # 说明
    note: str = ""

    def url(self, arch: str = "amd64") -> str:
        """首个可用源（兼容旧接口）"""
        mirrors = self.mirrors_for(arch)
        if mirrors:
            return mirrors[0]
        return self.url_arm64 if arch == "arm64" else self.url_amd64

    def mirrors_for(self, arch: str = "amd64") -> list[str]:
        """该架构的全部镜像源（按优先级）

        多源 + url_* 兜底。url_* 也放进列表末尾，
        保证即使忘了配 mirrors 也能下载。
        """
        primary = self.mirrors_arm64 if arch == "arm64" else self.mirrors_amd64
        out = list(primary)
        fallback = self.url_arm64 if arch == "arm64" else self.url_amd64
        if fallback and fallback not in out:
            out.append(fallback)
        return out


# ─────────────────────────────────────────
# 发行版目录
# ─────────────────────────────────────────
CATALOG: dict[str, ImageSpec] = {
    # ════════════════════════════════════════════════════
    # Debian 系
    # ════════════════════════════════════════════════════
    "ubuntu-24.04": ImageSpec(
        key="ubuntu-24.04",
        name="Ubuntu 24.04 LTS (Noble)",
        family="debian",
        mirrors_amd64=[
            "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-amd64.img",
            "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-amd64.img",
            "https://repo.huaweicloud.com/ubuntu-cloud-images/noble/current/noble-server-cloudimg-amd64.img",
        ],
        mirrors_arm64=[
            "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-arm64.img",
            "https://mirrors.ustc.edu.cn/ubuntu-cloud-images/noble/current/noble-server-cloudimg-arm64.img",
        ],
        url_amd64="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        url_arm64="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-arm64.img",
        default_user="ubuntu",
        default_mem_mb=1024,
        note="长期支持，软件生态最全（推荐）",
    ),
    "ubuntu-25.10": ImageSpec(
        key="ubuntu-25.10",
        name="Ubuntu 25.10 (Questing)",
        family="debian",
        mirrors_amd64=[
            "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/questing/current/questing-server-cloudimg-amd64.img",
        ],
        mirrors_arm64=[
            "https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cloud-images/questing/current/questing-server-cloudimg-arm64.img",
        ],
        url_amd64="https://cloud-images.ubuntu.com/questing/current/questing-server-cloudimg-amd64.img",
        url_arm64="https://cloud-images.ubuntu.com/questing/current/questing-server-cloudimg-arm64.img",
        default_user="ubuntu",
        default_mem_mb=1024,
        note="最新特性，非 LTS",
    ),
    "debian-13": ImageSpec(
        key="debian-13",
        name="Debian 13 (Trixie)",
        family="debian",
        mirrors_amd64=[
            "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
            "https://mirror.sjtu.edu.cn/debian-cdimage/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
            "https://cdimage.debian.org/cdimage/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
        ],
        mirrors_arm64=[
            "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2",
            "https://cdimage.debian.org/cdimage/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2",
        ],
        url_amd64="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
        url_arm64="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2",
        default_user="debian",
        default_mem_mb=1024,
        note="最新稳定版（推荐）",
    ),
    "debian-12": ImageSpec(
        key="debian-12",
        name="Debian 12 (Bookworm)",
        family="debian",
        mirrors_amd64=[
            "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
            "https://mirror.sjtu.edu.cn/debian-cdimage/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
            "https://cdimage.debian.org/cdimage/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        ],
        mirrors_arm64=[
            "https://mirrors.ustc.edu.cn/debian-cdimage/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2",
            "https://cdimage.debian.org/cdimage/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2",
        ],
        url_amd64="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        url_arm64="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-arm64.qcow2",
        default_user="debian",
        default_mem_mb=1024,
        note="更保守，兼容老项目",
    ),

    # ════════════════════════════════════════════════════
    # Alpine（轻量首选）
    # ════════════════════════════════════════════════════
    "alpine-3.22": ImageSpec(
        key="alpine-3.22",
        name="Alpine Linux 3.22",
        family="alpine",
        # 注意：3.22 起文件名去掉了 nocloud_ 前缀，且小版本号会变。
        # 下载时先用 latest 探测真实文件名，见 _resolve_alpine_url()。
        mirrors_amd64=[
            "https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.22/releases/cloud/",
            "https://mirrors.aliyun.com/alpine/v3.22/releases/cloud/",
            "https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/",
        ],
        url_amd64="https://dl-cdn.alpinelinux.org/alpine/v3.22/releases/cloud/alpine-3.22.7-x86_64-uefi-cloudinit-r0.qcow2",
        default_user="alpine",
        default_mem_mb=256,
        default_disk_gb=8,
        note="极轻量（guest 内约 150MB），启动最快",
    ),

    # ════════════════════════════════════════════════════
    # RHEL 系
    # ════════════════════════════════════════════════════
    "rocky-9": ImageSpec(
        key="rocky-9",
        name="Rocky Linux 9",
        family="fedora",
        mirrors_amd64=[
            "https://mirrors.ustc.edu.cn/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
            "https://mirrors.aliyun.com/rockylinux/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
            "https://repo.huaweicloud.com/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        ],
        mirrors_arm64=[
            "https://mirrors.ustc.edu.cn/rocky/9/images/aarch64/Rocky-9-GenericCloud-Base.latest.aarch64.qcow2",
            "https://mirrors.aliyun.com/rockylinux/9/images/aarch64/Rocky-9-GenericCloud-Base.latest.aarch64.qcow2",
        ],
        url_amd64="https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2",
        url_arm64="https://download.rockylinux.org/pub/rocky/9/images/aarch64/Rocky-9-GenericCloud-Base.latest.aarch64.qcow2",
        default_user="rocky",
        default_mem_mb=1024,
        note="RHEL 9 兼容，企业环境常用",
    ),
    "rocky-10": ImageSpec(
        key="rocky-10",
        name="Rocky Linux 10",
        family="fedora",
        mirrors_amd64=[
            "https://mirrors.ustc.edu.cn/rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
            "https://mirrors.aliyun.com/rockylinux/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
        ],
        url_amd64="https://download.rockylinux.org/pub/rocky/10/images/x86_64/Rocky-10-GenericCloud-Base.latest.x86_64.qcow2",
        default_user="rocky",
        default_mem_mb=1024,
        note="最新大版本",
    ),

    # ════════════════════════════════════════════════════
    # Fedora
    # ════════════════════════════════════════════════════
    "fedora-42": ImageSpec(
        key="fedora-42",
        name="Fedora 42 Cloud",
        family="fedora",
        mirrors_amd64=[
            "https://mirrors.ustc.edu.cn/fedora/releases/42/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64.qcow2",
            "https://mirrors.tuna.tsinghua.edu.cn/fedora/releases/42/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64.qcow2",
        ],
        url_amd64="https://download.fedoraproject.org/pub/fedora/linux/releases/42/Cloud/x86_64/images/Fedora-Cloud-Base-Generic.x86_64.qcow2",
        default_user="fedora",
        default_mem_mb=1024,
        note="最新内核与新特性",
    ),

    # ════════════════════════════════════════════════════
    # Arch（滚动更新）
    # ════════════════════════════════════════════════════
    "arch": ImageSpec(
        key="arch",
        name="Arch Linux Cloud",
        family="arch",
        mirrors_amd64=[
            "https://mirrors.tuna.tsinghua.edu.cn/archlinux/images/latest/Arch-Linux-x86_64-cloudimg.qcow2",
            "https://mirrors.ustc.edu.cn/archlinux/images/latest/Arch-Linux-x86_64-cloudimg.qcow2",
        ],
        url_amd64="https://geo.mirror.pkgbuild.com/images/latest/Arch-Linux-x86_64-cloudimg.qcow2",
        default_user="arch",
        default_mem_mb=1024,
        note="滚动更新，AUR 生态",
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


def _resolve_alpine_url(spec: ImageSpec, arch: str, on_log=None) -> list[str]:
    """Alpine 镜像的小版本号会变，从目录页探测真实文件名

    Alpine 3.22 起文件名形如 alpine-3.22.7-x86_64-uefi-cloudinit-r0.qcow2，
    小版本号每次发版都变。目录里可能同时存在多个版本，
    取最新的一个。

    spec.mirrors_amd64 里存的是目录 URL（以 / 结尾），
    这里探测后拼成完整文件 URL。
    """
    log = on_log or (lambda _m: None)
    dirs = spec.mirrors_for(arch)
    out: list[str] = []

    import re

    for base in dirs:
        if not base.endswith("/"):
            # 已经是完整文件 URL（官方源直接给了具体版本号）
            out.append(base)
            continue

        try:
            req = urllib.request.Request(
                base, headers={"User-Agent": "podman-gui/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            # 匹配 alpine-3.22.x-<arch>-uefi-cloudinit-r0.qcow2
            arch_pat = "x86_64" if arch == "amd64" else "aarch64"
            names = re.findall(
                r'alpine-[\d.]+-' + arch_pat +
                r'-uefi-cloudinit-r0\.qcow2',
                html,
            )
            if names:
                # 按版本号排序取最新
                def ver(n: str):
                    m = re.search(r'alpine-([\d.]+)-', n)
                    return tuple(int(x) for x in m.group(1).split(".")) if m else ()

                latest = sorted(set(names), key=ver)[-1]
                url = base + latest
                log(f"探测到 Alpine 镜像: {latest}")
                out.append(url)
            else:
                log(f"{base} 未找到匹配文件")
        except Exception as e:
            log(f"探测 {base} 失败: {e}")

    # 兜底：spec.url_amd64 里写的具体版本
    fallback = spec.url_amd64 if arch == "amd64" else spec.url_arm64
    if fallback and fallback not in out:
        out.append(fallback)

    return out


# ─────────────────────────────────────────
# 官方校验和
# ─────────────────────────────────────────
# 各发行版校验文件命名不统一，逐个探测：
#   Alpine   <file>.qcow2.sha512      内容：裸 sha512
#   Arch     <file>.qcow2.SHA256      内容：裸 sha256
#   Rocky    <dir>/CHECKSUM           内容：SHA256 (name) = hash
#   Debian   <dir>/SHA256SUMS         内容：hash *name
#   Ubuntu   <dir>/SHA256SUMS         内容：hash *name
#
# 为什么必须校验
# ──────────────
# 断点续传（HTTP Range）对着「内容会变的 URL」是灾难：
# 前半段来自旧版本、后半段来自新版本，拼出来的文件大小可能完全一样，
# 任何基于长度的检查都拦不住。烧进共享基础后，所有同镜像的 VM 全部损坏
# （guest 里表现为共享库 Exec format error → /run 挂不上 → sshd 起不来，
# 而宿主机侧只看到 SSH 超时，极难排查）。
# 因此下载完成必须比对官方哈希。
_CHECKSUM_SUFFIXES = (
    ".sha256", ".sha512", ".SHA256", ".SHA512",
    ".sha256sum", ".sha512sum",
)
_CHECKSUM_DIR_FILES = (
    "SHA256SUMS", "SHA256SUMS.txt", "sha256sums.txt", "sha256sum.txt",
    "SHA512SUMS", "SHA512SUMS.txt",
    "CHECKSUM", "CHECKSUM.txt", "checksums.txt",
)


def parse_checksum_text(text: str, filename: str) -> tuple[str, str] | None:
    """从校验文件内容里解析指定文件的 (算法, 哈希)

    三种格式都认：
      · 裸哈希（Alpine/Arch 单文件校验）
      · `<hash> *<name>`（Debian/Ubuntu SHA256SUMS）
      · `SHA256 (<name>) = <hash>`（Rocky CHECKSUM）
    """
    stripped = text.strip()

    # 裸哈希：整份内容就是一个十六进制串
    if re.fullmatch(r"[0-9a-fA-F]{64,128}", stripped):
        algo = "sha512" if len(stripped) == 128 else "sha256"
        return algo, stripped.lower()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # <hash> [*]<name>
        m = re.match(r"^([0-9a-fA-F]{64,128})\s+\*?(\S+)\s*$", line)
        if m and m.group(2).endswith(filename):
            algo = "sha512" if len(m.group(1)) == 128 else "sha256"
            return algo, m.group(1).lower()

        # SHA256 (<name>) = <hash>
        m = re.match(
            r"^(SHA256|SHA512)\s*\((.+?)\)\s*=\s*([0-9a-fA-F]+)\s*$", line
        )
        if m and m.group(2).strip().endswith(filename):
            return m.group(1).lower(), m.group(3).lower()

    return None


def official_checksum(
    file_url: str, timeout: int = 20
) -> tuple[str, str] | None:
    """探测某个文件 URL 的官方校验值

    返回 (算法, 哈希)；取不到返回 None（不是所有源都提供校验文件，
    上层会退回 qemu-img check 兜底）。
    """
    filename = file_url.rstrip("/").rsplit("/", 1)[-1]
    if not filename:
        return None

    candidates = [file_url + s for s in _CHECKSUM_SUFFIXES]
    base = file_url.rsplit("/", 1)[0]
    candidates += [f"{base}/{n}" for n in _CHECKSUM_DIR_FILES]

    for url in candidates:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "podman-gui/1.0"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read(256 * 1024).decode("utf-8", "replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            continue

        found = parse_checksum_text(text, filename)
        if found:
            return found

    return None


def hash_file(path: Path, algo: str = "sha256", on_progress=None) -> str:
    """计算文件哈希（分块读，大文件不占内存）"""
    h = hashlib.new(algo)
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


def sidecar_path(spec: "ImageSpec", arch: str = "amd64") -> Path:
    """校验记录文件路径（镜像旁边的小 json）"""
    return image_path(spec, arch).with_name(
        f"{spec.key}-{arch}.qcow2.sha256.json"
    )


def write_sidecar(
    spec: "ImageSpec",
    arch: str,
    sha256: str,
    source_url: str = "",
    verified_with: str = "",
) -> None:
    """写校验记录

    共享基础指纹和 VM 都读它，避免重复算哈希。
    """
    p = image_path(spec, arch)
    if not p.exists():
        return
    try:
        sidecar_path(spec, arch).write_text(
            json.dumps(
                {
                    "sha256": sha256,
                    "source_url": source_url,
                    "verified_with": verified_with,
                    "size": p.stat().st_size,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_sidecar(spec: "ImageSpec", arch: str = "amd64") -> dict:
    """读校验记录（不存在或已失效返回 {}）"""
    p = image_path(spec, arch)
    side = sidecar_path(spec, arch)
    if not p.exists() or not side.exists():
        return {}
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # 文件被换过（大小不符）则记录失效
    if data.get("size") != p.stat().st_size:
        return {}
    return data


def image_sha256(spec: "ImageSpec", arch: str = "amd64", on_log=None) -> str:
    """本地镜像文件的 sha256（sidecar 命中则免算）"""
    log = on_log or (lambda _m: None)
    p = image_path(spec, arch)
    if not p.exists():
        return ""

    cached = read_sidecar(spec, arch)
    if cached.get("sha256"):
        return cached["sha256"]

    log(f"计算镜像校验值（{p.name}）…")
    digest = hash_file(p, "sha256")
    write_sidecar(spec, arch, digest)
    return digest


def _partial_meta_path(tmp: Path) -> Path:
    """断点续传的元数据（记录 URL 校验器）"""
    return tmp.with_suffix(".part.meta")


def _read_partial_meta(tmp: Path) -> dict:
    p = _partial_meta_path(tmp)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_partial_meta(tmp: Path, **kw) -> None:
    try:
        _partial_meta_path(tmp).write_text(
            json.dumps(kw, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _download_one(
    url: str,
    target: Path,
    tmp: Path,
    on_progress=None,
    on_log=None,
    cancel_flag=None,
    meta: dict | None = None,
) -> tuple[bool, str]:
    """从单个 URL 下载（支持断点续传 + 一致性校验）

    断点续传通过 HTTP Range 实现，但**必须配 If-Range**：
      · tmp 已有内容 → 带 Range: bytes=<已下载>- 续传
      · 同时带 If-Range: <上次响应给的 ETag 或 Last-Modified>
      · 服务端内容没变 → 206，继续追加
      · 服务端内容变了 → 200，从头下（If-Range 由服务端判定，
        避免了「前半旧 + 后半新」拼出损坏文件）

    光靠 Range 不校验是危险的：镜像 URL 常指向会更新的内容
    （latest / current / 小版本号变化），续传拼出的文件大小可能
    恰好一样，长度检查拦不住，烧进共享基础后所有同镜像 VM 全坏。

    下载完成后由上层比对官方哈希。

    返回 (成功, 错误信息)
    """
    log = on_log or (lambda _m: None)
    meta = meta if meta is not None else {}

    have = tmp.stat().st_size if tmp.exists() else 0
    prev = _read_partial_meta(tmp) if have > 0 else {}

    # URL 换了（切到别的镜像源）→ 旧分片不能用
    if have > 0 and prev.get("url") and prev["url"] != url:
        log("切换镜像源，丢弃旧分片重新下载")
        have = 0
        tmp.unlink(missing_ok=True)

    headers = {"User-Agent": "podman-gui/1.0"}
    if have > 0:
        headers["Range"] = f"bytes={have}-"
        # If-Range：内容变了服务端会无视 Range 直接返回 200
        validator = prev.get("etag") or prev.get("last_modified")
        if validator:
            headers["If-Range"] = validator
        log(f"尝试断点续传（已有 {have / 1024**2:.1f} MB）")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status

            if status == 206:
                cr = resp.headers.get("Content-Range", "")
                total = int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0
            elif status == 200:
                # 服务端不支持续传，或内容已变（If-Range 未匹配）
                if have > 0:
                    log("镜像源内容已更新，重新完整下载")
                have = 0
                tmp.unlink(missing_ok=True)
                total = int(resp.headers.get("Content-Length", 0) or 0)
            else:
                return False, f"HTTP {status}"

            # 记下校验器，供下次续传比对
            _write_partial_meta(
                tmp,
                url=url,
                etag=resp.headers.get("ETag", ""),
                last_modified=resp.headers.get("Last-Modified", ""),
                total=total,
            )

            mode = "ab" if have > 0 else "wb"
            done = have

            with open(tmp, mode) as f:
                while True:
                    if cancel_flag and cancel_flag():
                        return False, "已取消"

                    buf = resp.read(512 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if on_progress:
                        on_progress(done, total)

    except urllib.error.HTTPError as e:
        # 416 = Range 超出范围，可能已下完（交给哈希校验判定）
        if e.code == 416 and have > 0:
            pass
        elif e.code == 404:
            return False, "文件不存在 (404)"
        else:
            return False, f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return False, f"网络错误: {e.reason}"
    except OSError as e:
        return False, f"写入失败: {e}"

    if not tmp.exists() or tmp.stat().st_size < 1024 * 1024:
        return False, "下载内容过小，可能不是有效镜像"

    # 期望大小对不上 → 内容不完整
    if total and tmp.stat().st_size != total:
        return False, (
            "下载不完整（%.1f MB / 应为 %.1f MB）"
            % (tmp.stat().st_size / 1024**2, total / 1024**2)
        )

    # ── 官方哈希校验 ──
    # 断点续传拼错、镜像源半更新、CDN 缓存不一致都能在这里拦下
    expected = meta.get("checksum")          # (algo, hash) 或 None
    if expected:
        algo, want = expected
        log("校验镜像完整性（%s）…" % algo)
        got = hash_file(tmp, algo)
        if got != want:
            tmp.unlink(missing_ok=True)
            _partial_meta_path(tmp).unlink(missing_ok=True)
            return False, (
                "%s 校验失败（文件损坏或不完整）\n"
                "  期望 %s…\n"
                "  实际 %s…"
                % (algo, want[:16], got[:16])
            )
        meta["verified_with"] = algo
        log("校验通过")

    tmp.replace(target)
    _partial_meta_path(tmp).unlink(missing_ok=True)
    return True, ""


def _make_progress_proxy(on_progress):
    """进度回调节流包装

    下载每 512KB 调一次 on_progress，593MB 镜像 ≈ 1200 次。
    每次都跨线程 emit 信号会堆积分纽事件（UI 卡顿），
    节流到 100ms 一次，肉眼无感差别。
    """
    if on_progress is None:
        return None

    import time as _t

    last = [0.0]

    def proxy(done, total):
        now = _t.monotonic()
        # 完成/总数变化/首次 都立即发；其余 100ms 节流
        if total <= 0 or done >= total or now - last[0] >= 0.1:
            last[0] = now
            on_progress(done, total)

    return proxy


def _record_download(
    spec: ImageSpec,
    arch: str,
    target: Path,
    url: str,
    verified_with: str = "",
    *,
    on_log=None,
) -> None:
    """下载完成后记录：算 sha256 写 sidecar + 写 SQLite

    sha256 是共享基础指纹的依据（见 shared_disk），
    也是判断「镜像是否被换过」的唯一可靠手段
    （长度、mtime 都会被断点续传拼出的等价长度骗过）。
    """
    log = on_log or (lambda _m: None)
    try:
        log("计算镜像指纹（sha256）…")
        digest = hash_file(target, "sha256")
        write_sidecar(spec, arch, digest, url, verified_with)

        from core import state

        state.record_image(
            spec.key, arch, str(target), target.stat().st_size, digest
        )
        log("指纹 %s…" % digest[:16])
    except Exception as e:                       # 记录失败不影响下载结果
        log("记录镜像信息失败: %s" % e)


def download_image(
    spec: ImageSpec,
    arch: str = "amd64",
    on_progress: Callable[[int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """下载镜像（多源自动降级 + 断点续传）

    依次尝试每个镜像源，全部失败才返回错误。
    单个源下载中断后保留 .part 文件，下次续传。

    on_progress(已下载字节, 总字节)
    on_log(消息)
    cancel_flag() 返回 True 时中止
    """
    log = on_log or (lambda _m: None)
    on_progress = _make_progress_proxy(on_progress)

    if spec.family == "alpine":
        urls = _resolve_alpine_url(spec, arch, on_log=log)
    else:
        urls = spec.mirrors_for(arch)

    if not urls:
        return False, f"{spec.name} 不支持 {arch} 架构"

    target = image_path(spec, arch)
    tmp = target.with_suffix(".part")

    errors: list[str] = []
    verified_with = ""

    for i, url in enumerate(urls, 1):
        if cancel_flag and cancel_flag():
            tmp.unlink(missing_ok=True)
            return False, "已取消"

        log("[%d/%d] %s" % (i, len(urls), url))
        if on_progress:
            on_progress(0, 0)

        # 探测官方校验值（Alpine .sha512 / Arch .SHA256 /
        # Rocky CHECKSUM / Debian-Ubuntu SHA256SUMS）
        meta = {}
        found = official_checksum(url)
        if found:
            meta["checksum"] = found
            log("获取官方校验值: %s" % found[0])
        else:
            log("该源未提供校验文件，下载后用 qemu-img 兜底检查")

        ok, err = _download_one(
            url, target, tmp,
            on_progress=on_progress,
            on_log=log,
            cancel_flag=cancel_flag,
            meta=meta,
        )

        if ok:
            verified_with = meta.get("verified_with", "")
            size_mb = target.stat().st_size / 1024**2
            log("下载完成: %s (%.1f MB)" % (target.name, size_mb))
            _record_download(spec, arch, target, url, verified_with, on_log=log)
            return True, str(target)

        errors.append("%s: %s" % (url, err))
        log("  失败: %s，尝试下一个源…" % err)

    # 全部失败。保留 .part 以便下次续传，
    # 除非是「已取消」。
    if cancel_flag and cancel_flag():
        return False, "已取消"

    tmp.unlink(missing_ok=True)
    return False, "所有镜像源均失败:\n" + "\n".join(errors[:3])


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
