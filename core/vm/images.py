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
IMAGES_DIR = Path.home() / ".local" / "share" / "podbox" / "vm" / "images"


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
# 两层结构：
#   1. CATALOG —— 精选静态条目（友好名、推荐位、排序）
#   2. 动态发现 —— 向导打开时从镜像站索引拉全部
#      发行版/版本组合（24+ 个），精选之外自动补齐。
# key 约定：<distro>-<version>-lxc，download_rootfs 按 key
# 解析 distro/version 查索引。family 决定包管理器/sshd 安装命令。

# 发行版元数据：显示名、家族（包管理器归类）、是否推荐
DISTRO_META: dict[str, tuple[str, str]] = {
    # distro: (显示名, family)
    "alpine": ("Alpine", "alpine"),
    "ubuntu": ("Ubuntu", "debian"),
    "debian": ("Debian", "debian"),
    "rockylinux": ("Rocky Linux", "rocky"),
    "almalinux": ("AlmaLinux", "rocky"),
    "fedora": ("Fedora", "fedora"),
    "archlinux": ("Arch Linux", "arch"),
    "cachyos": ("CachyOS", "arch"),
    "opensuse": ("openSUSE", "suse"),
    "centos": ("CentOS Stream", "rocky"),
    "oracle": ("Oracle Linux", "rocky"),
    "amazonlinux": ("Amazon Linux", "fedora"),
    "openeuler": ("openEuler", "fedora"),
    "kali": ("Kali", "debian"),
    "mint": ("Linux Mint", "debian"),
    "devuan": ("Devuan", "debian"),
    "voidlinux": ("Void", "void"),
    "alt": ("ALT Linux", "fedora"),
    "slackware": ("Slackware", "slackware"),
    "plamo": ("Plamo", "fedora"),
    "nixos": ("NixOS", "nixos"),
    "openwrt": ("OpenWrt", "openwrt"),
    "busybox": ("BusyBox", "busybox"),
}

# 版本代号 → 人类可读（Debian/Ubuntu 用代号，其它是数字本身可读）
VERSION_NAMES = {
    "trixie": "13 (trixie)", "bookworm": "12 (bookworm)",
    "forky": "14 (forky)",
    "noble": "24.04 LTS", "jammy": "22.04 LTS",
    "resolute": "25.10", "stonking": "26.04",
    "tumbleweed": "Tumbleweed", "current": "当前版",
    "edge": "edge(滚动)", "unstable": "unstable",
    "Sisyphus": "Sisyphus", "p11": "p11",
}

# 推荐排序（向导里靠前）
PREFERRED_ORDER = [
    "alpine", "debian", "ubuntu", "rockylinux", "fedora",
    "archlinux", "cachyos", "opensuse",
]


def _make_spec(distro: str, version: str) -> ImageSpec:
    """从 distro/version 构造 ImageSpec"""
    name, family = DISTRO_META.get(
        distro, (distro.capitalize(), "debian")
    )
    ver_disp = VERSION_NAMES.get(version, version)
    return ImageSpec(
        key=f"{distro}-{version}-lxc",
        name=f"{name} {ver_disp}",
        family=family,
        default_user="dev",
        format="rootfs",
        uefi=False,
    )


# 精选静态条目（无索引也能列表/创建；有索引时被动态版合并）
# key 与动态生成的完全一致（<distro>-<version>-lxc），避免同一
# 发行版出现两条（曾出现过 alpine-3.22-lxc / alpine-3.22-lxc 重复）。
# 老机器的 image_key 可能还是 alpine-3.22-lxc 形式 → get_spec 时做别名映射。
CATALOG: dict[str, ImageSpec] = {
    _make_spec(d, v).key: _make_spec(d, v)
    for d, v in [
        ("alpine", "3.22"),
        ("debian", "trixie"),
        ("ubuntu", "noble"),
        ("rockylinux", "10"),
        ("fedora", "43"),
        ("archlinux", "current"),
    ]
}

# 老 key → 新 key（VM 档时代的静态目录遗留）
_LEGACY_KEYS = {
    "alpine-3.22-lxc": "alpine-3.22-lxc",
    "rocky-10-lxc": "rockylinux-10-lxc",
    "debian-13-lxc": "debian-trixie-lxc",
}


def discover_catalog(arch: str = "amd64") -> dict[str, ImageSpec]:
    """从镜像站索引发现全部可用发行版/版本

    索引拉不到（离线/超时）时返回静态 CATALOG。
    结果带模块级缓存，一次会话只拉一遍。
    """
    global _DISCOVERED

    if _DISCOVERED is not None:
        return _DISCOVERED

    rows, base = _fetch_index()
    if not rows:
        _DISCOVERED = dict(CATALOG)
        return _DISCOVERED

    out: dict[str, ImageSpec] = {}
    for r in rows:
        if r[_IDX_ARCH] != arch or r[_IDX_VARIANT] != "default":
            continue
        distro, version = r[_IDX_DISTRO], r[_IDX_VER]
        # 过滤明显不适合当开发环境的
        if distro in ("busybox", "openwrt"):
            continue
        spec = _make_spec(distro, version)
        out[spec.key] = spec

    # 排序：推荐的发行版在前，同发行版新版本在前
    def _sort_key(item):
        key = item[0]
        d = key.removesuffix("-lxc").split("-")[0]
        pri = PREFERRED_ORDER.index(d) if d in PREFERRED_ORDER else 99
        return (pri, d, key)

    _DISCOVERED = dict(sorted(out.items(), key=_sort_key))
    return _DISCOVERED


_DISCOVERED: dict[str, ImageSpec] | None = None


def reset_discovered() -> None:
    """清掉动态目录缓存（测试用/手动刷新）"""
    global _DISCOVERED
    _DISCOVERED = None


def list_images(arch: str = "amd64") -> list[ImageSpec]:
    """列出当前架构可用的镜像

    rootfs 条目（容器档）不能按静态 url 过滤：它的下载地址
    是从 meta 索引**动态解析**的（resolve_rootfs_url），
    ImageSpec 里没有 url_amd64 字段，按 qcow2 时代的
    spec.url() 判断会把三个 rootfs 条目全部过滤掉 ——
    向导里就没有可下载的镜像了。
    """
    # 动态发现优先（24+ 发行版），索引拉不到回落静态精选
    return list(discover_catalog(arch).values())


def get_spec(key: str) -> ImageSpec | None:
    cat = discover_catalog()
    if key in cat:
        return cat[key]
    # 老格式 key（VM 档时代 / 静态目录早期版本）
    if key in _LEGACY_KEYS:
        return cat.get(_LEGACY_KEYS[key]) or CATALOG.get(
            _LEGACY_KEYS[key]
        )
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
    """解包后修权：setuid 位补回 + 播种路径放开

    各发行版 tarball 的权限位差异是**静态可判定**的坑源
    （不用逐个下载测试）：

    1. setuid 位：内核禁止非 root 进程设置 setuid —— 普通用户
       解包后 sudo/su/passwd 全部变普通文件（实测 debian）
    2. 奇异 mode：Rocky 的 etc/shadow 是 0000（----------），
       宿主用户连读都不行，播种直接 EACCES（实测 rocky）

    两类都在用户命名空间里以 root 身份修（CAP_SETUID 可设
    setuid，CAP_DAC_OVERRIDE 无视任何 mode 位）。
    """
    from core.vm.container import userns_run

    # 1) setuid 位清单（各发行版通用，缺的忽略）
    su_targets = [
        "usr/bin/sudo", "usr/bin/su", "usr/bin/passwd",
        "usr/bin/chsh", "usr/bin/chfn", "usr/bin/newgrp",
        "usr/bin/gpasswd", "usr/bin/chage", "usr/bin/expiry",
        "usr/bin/wall", "usr/bin/mount", "usr/bin/umount",
        "usr/lib/openssh/ssh-keysign", "bin/mount", "bin/umount",
        "bin/su", "usr/sbin/unix_chkpwd",
    ]
    existing = [
        str(rootfs / t) for t in su_targets if (rootfs / t).exists()
    ]
    if existing:
        userns_run(["chmod", "u+s", *existing], timeout=60)

    # 2) 播种路径放开：目录 755（宿主可进可写）、
    #    passwd/shadow/hosts 600（宿主可读写）。
    #    属主在 idmap 下已是宿主用户，chmod 后全部可操作。
    etc = rootfs / "etc"
    if etc.exists():
        userns_run([
            "sh", "-c",
            "chmod 755 {etc} && "
            "chmod 600 {etc}/shadow {etc}/gshadow 2>/dev/null; "
            "chmod 644 {etc}/passwd {etc}/group {etc}/hosts "
            "{etc}/hostname 2>/dev/null; true".format(etc=etc),
        ], timeout=60)
    for d in ("root", "home"):
        if (rootfs / d).exists():
            userns_run(["chmod", "755", str(rootfs / d)], timeout=30)


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
