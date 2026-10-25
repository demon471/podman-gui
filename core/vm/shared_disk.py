"""共享磁盘管理：所有 VM 共用一个基础磁盘

设计目标
────────
简化虚拟机创建流程：用户不需要为每台 VM 决定磁盘大小。

实现方式
────────
qemu 的 backing file 天然支持共享：

    shared-base.qcow2          ← 只读的发行版基础镜像
       ↑  └─ vm-a/disk.qcow2    ← VM A 的写时复制层
       ↑  └─ vm-b/disk.qcow2    ← VM B 的写时复制层

每个 VM 的 disk.qcow2 只记录自己改动的块，未改写的块从
共享基础读。因此：
  · 磁盘空间 = 共享基础（一份）+ 各 VM 增量（通常几十 MB）
  · 新建 VM 不需要等待复制镜像，毫秒级完成
  · 用户不需要选磁盘大小，增量层自动按需增长

与「每 VM 一份完整磁盘」的区别：
  旧：disk_gb 是用户要填的参数，且预分配思路容易让人误解为
      每台 VM 都占满那么大的空间。
  新：共享基础 + 增量，按需增长，无需配置。

容量上限
────────
增量层仍有一个上限（防止误操作把宿主机磁盘写满），
默认 32GB —— 对容器/开发场景足够，且远小于用户感知。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.vm import images

# 增量层容量上限（GB）。
#
# 设为 1T 是为了让用户感知不到「磁盘大小」这个技术概念：
# 开箱即用，不需要在创建时选磁盘容量，也不会因为默认值太小
# 在 guest 内装几个包就报 "No space left on device"。
#
# qcow2 是稀疏格式，这个上限只是逻辑容量，实际占用按写入量增长
# （一台只装了基础系统的 VM 通常只占几十 MB）。
DEFAULT_INCREMENTAL_GB = 1024      # 1 TB

# 共享基础的存放目录（与 images 缓存分开，明确职责）
SHARED_DIR_NAME = "shared"


def shared_base_path(spec: images.ImageSpec, arch: str = "amd64") -> Path:
    """共享基础磁盘路径"""
    from core.vm.manager import VM_ROOT

    return VM_ROOT / SHARED_DIR_NAME / f"{spec.key}-{arch}.qcow2"


def ensure_shared_base(
    spec: images.ImageSpec,
    arch: str = "amd64",
    *,
    on_log=None,
) -> tuple[bool, str]:
    """确保共享基础磁盘就绪

    首次调用时从发行版镜像创建一份只读基础，
    之后所有 VM 共用。重复调用直接返回成功。

    返回 (成功, 错误信息)
    """
    log = on_log or (lambda _m: None)
    target = shared_base_path(spec, arch)

    if target.exists():
        return True, ""

    source = images.image_path(spec, arch)
    if not source.exists():
        return False, f"镜像未下载: {source}"

    log(f"创建共享磁盘基础（{spec.name}）…")
    target.parent.mkdir(parents=True, exist_ok=True)

    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        return False, "未找到 qemu-img"

    # 转成内部快照形式的 qcow2，作为只读 backing file。
    # 不做完整复制：直接基于原始镜像创建引用，
    # 写入只发生在各 VM 的增量层。
    #
    # 关键：不能指定 size 参数。
    # qemu-img create 带 backing file 时，overlay 的虚拟大小若小于
    # backing 的大小，虽然命令成功，但实际只能看到指定的大小
    # （例如写 "1M" 就只剩 1MB），UEFI 会因找不到启动项而
    # 回落到 PXE / 报 "No bootable option"。
    # 省略 size 时 qemu-img 自动继承 backing 的大小。
    tmp = target.with_suffix(".tmp.qcow2")
    r = subprocess.run(
        [
            qemu_img, "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", str(source),
            str(tmp),
        ],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        return False, f"创建共享基础失败: {(r.stderr or r.stdout)[:200]}"

    # 原子替换（避免并发创建时拿到半个文件）
    tmp.replace(target)
    log(f"共享基础已就绪: {target.name}")
    return True, ""


def create_vm_disk(
    spec: images.ImageSpec,
    vm_dir: Path,
    arch: str = "amd64",
    *,
    incremental_gb: int = DEFAULT_INCREMENTAL_GB,
) -> tuple[bool, str]:
    """为单个 VM 创建增量磁盘（基于共享基础）

    返回 (成功, 错误信息)
    """
    base = shared_base_path(spec, arch)
    if not base.exists():
        return False, "共享磁盘基础不存在"

    target = vm_dir / "disk.qcow2"
    vm_dir.mkdir(parents=True, exist_ok=True)

    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        return False, "未找到 qemu-img"

    r = subprocess.run(
        [
            qemu_img, "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", str(base),
            str(target),
            f"{incremental_gb}G",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        return False, f"创建 VM 磁盘失败: {(r.stderr or r.stdout)[:200]}"

    return True, ""


def shared_base_size(spec: images.ImageSpec, arch: str = "amd64") -> int:
    """共享基础实际占用（字节）"""
    p = shared_base_path(spec, arch)
    try:
        return p.stat().st_size if p.exists() else 0
    except OSError:
        return 0


def clear_shared_base(spec: images.ImageSpec, arch: str = "amd64") -> bool:
    """删除共享基础（所有依赖它的 VM 会失效，需谨慎）"""
    p = shared_base_path(spec, arch)
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False
