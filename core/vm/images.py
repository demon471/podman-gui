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
# 纯容器目录（rootfs tarball，QEMU 云镜像条目已随 VM 档删除）：
# format="rootfs"，key 以 -lxc 结尾（download_rootfs 按 key
# 去掉 -lxc 后解析 distro/version 查索引）
CATALOG: dict[str, ImageSpec] = {
    "alpine-3.22-lxc": ImageSpec(
        key="alpine-3.22-lxc",
        name="Alpine 3.22",
        family="alpine",
        default_user="dev",
        format="rootfs",
        uefi=False,
        note="最轻，~60MB，秒级启动（推荐）",
    ),
    "rocky-10-lxc": ImageSpec(
        key="rocky-10-lxc",
        name="Rocky Linux 10",
        family="rocky",
        default_user="dev",
        format="rootfs",
        uefi=False,
        note="RHEL 兼容，企业生态",
    ),
    "debian-13-lxc": ImageSpec(
        key="debian-13-lxc",
        name="Debian 13",
        family="debian",
        default_user="dev",
        format="rootfs",
        uefi=False,
        note="最稳的通用发行版",
    ),
}


def list_images(arch: str = "amd64") -> list[ImageSpec]:
    """列出当前架构可用的镜像

    rootfs 条目（容器档）不能按静态 url 过滤：它的下载地址
    是从 meta 索引**动态解析**的（resolve_rootfs_url），
    ImageSpec 里没有 url_amd64 字段，按 qcow2 时代的
    spec.url() 判断会把三个 rootfs 条目全部过滤掉 ——
    向导里就没有可下载的镜像了。
    """
    out = []
    for spec in CATALOG.values():
        if spec.format == "rootfs" or spec.url(arch):
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


LXC_MIRRORS = [
    # 国内优先（实测快 3-10 倍）
    ("https://mirrors.tuna.tsinghua.edu.cn/lxc-images/meta/1.0/index-system",
     "https://mirrors.tuna.tsinghua.edu.cn/lxc-images"),
    ("https://images.linuxcontainers.org/meta/1.0/index-system",
     "https://images.linuxcontainers.org"),
]

# distro 在 index 里的目录名（含别名尝试顺序）
_LXC_DISTRO_ALIASES = {
    "rocky": ("rockylinux", "rocky"),
    "alpine": ("alpine",),
    "debian": ("debian",),
}

# 版本号 → 索引里的真实版本串
#
# Debian 在索引里用代号（trixie），不是数字（13）。
# 用户记数字更自然，所以在这一层做映射。
_LXC_VERSION_ALIASES = {
    ("debian", "13"): "trixie",
    ("debian", "12"): "bookworm",
    ("debian", "14"): "forky",
}

# index 行字段位置：distro;version;arch;variant;date;path;...
_IDX_DISTRO, _IDX_VER, _IDX_ARCH, _IDX_VARIANT, _IDX_DATE, _IDX_PATH = (
    0, 1, 2, 3, 4, 5
)


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


def _fetch_index() -> tuple[list[list[str]], str]:
    """拉取发行版索引（多源降级）

    返回 (行列表, 资源基址)。行按日期倒序，取第一个即最新。
    """
    import urllib.request

    for meta, base in LXC_MIRRORS:
        try:
            with urllib.request.urlopen(meta, timeout=15) as r:
                text = r.read().decode("utf-8", "replace")
            rows = [
                l.split(";")
                for l in text.splitlines()
                if l.strip() and not l.startswith("#")
            ]
            rows = [r for r in rows if len(r) > _IDX_PATH]
            rows.sort(key=lambda r: r[_IDX_DATE], reverse=True)
            if rows:
                return rows, base
        except (OSError, ValueError):
            continue
    return [], ""


def resolve_rootfs_url(distro: str, version: str, arch: str = "amd64",
                       variant: str = "default") -> str:
    """解析最新 rootfs.tar.xz 的 URL

    走 meta 索引：distro;version;arch;variant;date;path，
    挑日期最新的一个。找不到返回空串。
    """
    aliases = _LXC_DISTRO_ALIASES.get(distro, (distro,))
    version = _LXC_VERSION_ALIASES.get((distro, version), version)
    rows, base = _fetch_index()
    for row in rows:
        if row[_IDX_ARCH] != arch or row[_IDX_VARIANT] != variant:
            continue
        if row[_IDX_DISTRO] not in aliases:
            continue
        # 版本前缀匹配（3.22 匹配 3.22；10 匹配 10）
        if not row[_IDX_VER].startswith(version):
            continue
        # path 是站点相对路径，必须拼基址
        rel = row[_IDX_PATH].rstrip("/")
        return base + rel + "/rootfs.tar.xz"
    return ""


def rootfs_path(spec: ImageSpec, arch: str = "amd64") -> Path:
    """rootfs tarball 缓存路径"""
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    return IMAGES_DIR / f"{spec.key}-{arch}.tar.xz"


def rootfs_downloaded(spec: ImageSpec, arch: str = "amd64") -> bool:
    return rootfs_path(spec, arch).exists()


def download_rootfs(
    spec: ImageSpec,
    arch: str = "amd64",
    on_progress: Callable[[int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    """下载发行版 rootfs tarball（多源降级）

    distro/version 从 key 推导（alpine-3.22-lxc → alpine/3.22）。
    """
    log = on_log or (lambda _m: None)

    distro, version = spec.key.removesuffix("-lxc").rsplit("-", 1)
    url = resolve_rootfs_url(distro, version, arch)
    if not url:
        return False, f"索引里找不到 {spec.name} 的 rootfs"

    log(f"源: {url}")
    target = rootfs_path(spec, arch)
    tmp = target.with_suffix(".part")

    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "podbox"})
        with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if cancel_flag and cancel_flag():
                    tmp.unlink(missing_ok=True)
                    return False, "已取消"
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress and total:
                    on_progress(got, total)
        tmp.rename(target)
        return True, ""
    except (OSError, ValueError) as e:
        tmp.unlink(missing_ok=True)
        return False, str(e)


def extract_rootfs(
    spec: ImageSpec, arch: str, target_dir: Path,
) -> tuple[bool, str]:
    """解包 rootfs tarball 到机器目录

    必须带 --numeric-owner：tarball 里的 uid/gid 是发行版约定的
    （root=0），按名字映射在非 root 下解包会错乱。
    """
    tarball = rootfs_path(spec, arch)
    if not tarball.exists():
        return False, f"缓存里没有 {tarball.name}，请先下载"

    target_dir.mkdir(parents=True, exist_ok=True)
    import subprocess as _sp

    try:
        # 不用 --skip-old-files：残留的旧文件（属主可能是上一次
        # 运行遗留的 subuid，普通用户写不动）会让后续播种 EACCES。
        # 重解包覆盖即可，create_vm 已保证目录是新建的。
        r = _sp.run(
            ["tar", "-xJf", str(tarball), "-C", str(target_dir),
             "--numeric-owner", "--overwrite"],
            capture_output=True, text=True, timeout=600,
        )
    except (OSError, _sp.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]

    # 容器 rootfs 需要 /dev/pts 等（lxc autodev 会补），目录先建好
    for sub in ("dev", "proc", "sys"):
        (target_dir / sub).mkdir(exist_ok=True)

    # **普通用户解包会丢失全部 setuid/setgid 位**：内核禁止
    # 非 root 进程设置 setuid（安全规则）。tarball 里 sudo/su/
    # passwd 等都是 rwsr，解包后变 rwxr —— 容器内 sudo 直接报
    # "must be owned by uid 0 and have the setuid bit set"。
    # 解法：在用户命名空间里以 root 身份按 tar 记录补回
    # （userns 的 root 有 CAP_SETUID，可以设 setuid 位）。
    fix_setuid_bits(target_dir)
    return True, ""


def fix_setuid_bits(rootfs: Path) -> None:
    """在用户命名空间里恢复 rootfs 的 setuid/setgid 位

    扫 tar 记录找 mode 带 setuid 的文件太慢；直接对已知清单
    补位（各发行版通用，缺的文件 chmod 自然失败忽略）。
    """
    from core.vm.container import userns_run

    targets = [
        "usr/bin/sudo", "usr/bin/su", "usr/bin/passwd",
        "usr/bin/chsh", "usr/bin/chfn", "usr/bin/newgrp",
        "usr/bin/gpasswd", "usr/bin/chage", "usr/bin/expiry",
        "usr/bin/wall", "usr/bin/mount", "usr/bin/umount",
        "usr/lib/openssh/ssh-keysign", "bin/mount", "bin/umount",
        "bin/su", "usr/sbin/unix_chkpwd",
    ]
    existing = [
        str(rootfs / t) for t in targets if (rootfs / t).exists()
    ]
    if not existing:
        return
    userns_run(["chmod", "u+s", *existing], timeout=60)


def rootfs_cache_size() -> int:
    """rootfs tarball 缓存总字节"""
    total = 0
    if IMAGES_DIR.exists():
        for p in IMAGES_DIR.glob("*.tar.xz"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total
