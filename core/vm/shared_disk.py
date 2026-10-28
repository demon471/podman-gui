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
增量层仍有一个上限（防止误操作把宿主机磁盘写满），默认 1 TB。

容量统一为 1 TiB（稀疏）
────────────────────────────────────────────
qcow2 是稀疏格式：虚拟大小只是逻辑上限，实际占用按写入量增长，
一台只装了基础系统的 VM 通常只占几十 MB。统一给 1 TiB 是为了让
用户感知不到「磁盘大小」这个概念（不需要创建时选容量，也不会
装几个包就 "No space left on device"）。

两层必须**显式指定同一个尺寸**：

    qemu-img create -b <image>  -F qcow2  <shared>  1024G
    qemu-img create -b <shared> -F qcow2  <disk>    1024G

不要用「先 create 继承小尺寸、再 resize 变大」的两步走法 ——
实测那条路径会让 qcow2 的元数据与实际数据不一致，guest 内表现
为共享库加载失败（libeconf.so.0: Exec format error）→ busybox
mount 挂不上 /run → OpenRC 全线失败 → sshd 从未启动，宿主机侧只
看到 SSH 连接被 RST。

「设备容量 > 分区表记录的大小」是 cloud image 的常态，镜像自带
growpart，cloud-init 开机后会把分区和文件系统撑满。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from core.vm import images

# 增量层与共享基础的统一容量（GB）。
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


# ─────────────────────────────────────────
# 指纹：绑定「共享基础」与「VM 增量层」
# ─────────────────────────────────────────
# 为什么需要指纹
# ──────────────
# qcow2 的增量层记录的是「相对 backing file 的差异块」。
# 一旦 backing file 被替换（重新下载镜像、重建共享基础），
# 差异块就在错误的偏移上生效 —— 新基础的字节 + 旧增量层的补丁
# = 文件系统内容错乱。
#
# 典型症状（真实踩过）：
#   guest 里 /usr/lib 下新旧版本的共享库混搭，
#   libblkid 加载 libeconf 报 "Exec format error" → /run 挂不上
#   → OpenRC 全部失败 → sshd 起不来。
#   而宿主机侧只看到「SSH 连不上」，console 里也只有 initramfs 的输出。
#
# 结论：增量和基础必须成对。创建增量层时记下基础的指纹，
# 启动前比对，不一致就明确报错，而不是静默数据错乱。


def _base_meta_path(spec: "images.ImageSpec", arch: str = "amd64") -> Path:
    return shared_base_path(spec, arch).with_suffix(".meta.json")


def _write_base_meta(
    spec: "images.ImageSpec",
    arch: str,
    image_sha256: str,
    target: Path,
) -> None:
    """记录共享基础的指纹（源镜像 sha256 + 基础文件自身 sha256）

    双层指纹：
      · source_sha256 —— 来自哪个镜像（换镜像时立即发现）
      · base_sha256   —— 基础文件本身（基础被单独删改也能发现）
    base_sha256 对 VM 增量层做绑定校验是必需的：
    qcow2 头部里只记 backing 的文件名字符串，不记内容。
    """
    try:
        base_sha = images.hash_file(target, "sha256") if target.exists() else ""
    except OSError:
        base_sha = ""
    try:
        _base_meta_path(spec, arch).write_text(
            json.dumps(
                {
                    "source_sha256": image_sha256,
                    "base_sha256": base_sha,
                    "size": target.stat().st_size if target.exists() else 0,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_base_meta(spec: "images.ImageSpec", arch: str = "amd64") -> dict:
    """读共享基础指纹（不存在返回 {}）"""
    p = _base_meta_path(spec, arch)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def base_fingerprint(spec: "images.ImageSpec", arch: str = "amd64") -> str:
    """共享基础当前的指纹（优先用记录，缺失则现算）

    返回 base_sha256（十六进制）。空串表示无法确定。
    """
    target = shared_base_path(spec, arch)
    if not target.exists():
        return ""

    meta = read_base_meta(spec, arch)
    if meta.get("base_sha256") and meta.get("size") == target.stat().st_size:
        return meta["base_sha256"]

    # 记录缺失或失效：现算一次并补记
    try:
        digest = images.hash_file(target, "sha256")
    except OSError:
        return ""
    src = images.image_sha256(spec, arch)
    _write_base_meta(spec, arch, src, target)
    return digest


def check_base_unchanged(
    spec: "images.ImageSpec", arch: str = "amd64"
) -> bool:
    """共享基础文件本身是否与记录一致（false = 被改动/替换过）

    与 base_fingerprint 的区别：这里**现算文件哈希**再与记录比对。
    base_fingerprint 走记录缓存（便宜但只能反映「记录里写的值」），
    本函数能发现记录之外的真实篡改，代价是读一遍整个文件
    （几百 MB，仅用于显式校验场景）。

    常规启动路径不需要它：VM 增量层与基础的匹配通过比较
    两份记录完成（见 check_vm_disk_base），零成本。
    """
    meta = read_base_meta(spec, arch)
    if not meta.get("base_sha256"):
        return True          # 没有记录可依据，视为通过

    target = shared_base_path(spec, arch)
    if not target.exists():
        return False

    try:
        actual = images.hash_file(target, "sha256")
    except OSError:
        return True          # 读不了就不阻断，交给启动流程报错

    return actual == meta["base_sha256"]


def ensure_shared_base(
    spec: images.ImageSpec,
    arch: str = "amd64",
    *,
    incremental_gb: int = DEFAULT_INCREMENTAL_GB,
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
        # 已存在：确保指纹记录在册（老版本创建的没有记录）

        if not read_base_meta(spec, arch).get("base_sha256"):
            log("为共享基础建立指纹记录…")
            _write_base_meta(
                spec, arch, images.image_sha256(spec, arch), target
            )
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
    # size 必须显式指定。两个坑：
    #   · 省略 size → 继承镜像的原始大小（Alpine 只有 376 MiB），
    #     用户在 guest 里装几个包就 "No space left on device"
    #   · 省略 size 再 resize 放大 → qcow2 元数据与实际数据不一致，
    #     guest 内共享库加载失败（Exec format error）→ /run 挂不上
    #     → OpenRC 全线失败 → sshd 起不来（见文件头说明）
    # 所以这里一步到位，create 时就给最终容量。
    tmp = target.with_suffix(".tmp.qcow2")
    tmp.unlink(missing_ok=True)
    r = subprocess.run(
        [
            qemu_img, "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", str(source),
            str(tmp),
            f"{incremental_gb}G",
        ],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        return False, f"创建共享基础失败: {(r.stderr or r.stdout)[:200]}"

    # 原子替换（避免并发创建时拿到半个文件）
    tmp.replace(target)
    log(f"共享基础容量 {incremental_gb}G（稀疏，按需分配）")

    # 记录指纹：把基础绑定到「源镜像的 sha256」
    # 之后镜像被重下（内容变了）能立刻发现
    _write_base_meta(spec, arch, images.image_sha256(spec, arch), target)

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

    # size 与共享基础**显式取同一个值**（都走 DEFAULT_INCREMENTAL_GB）。
    # 既不能继承基础的原始大小，也不能只写增量层 ——
    # 两层不一致会让 guest 内共享库加载失败（见文件头说明）。
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

    # ── 绑定指纹 ──
    # 增量层只能配它创建时的那个基础。记下基础指纹，启动前校验；
    # 基础被替换（重下镜像 / 手动删了共享基础）时，
    # 新旧错位会让 guest 文件系统内容错乱，且症状极难反查。
    base_sha = base_fingerprint(spec, arch)
    try:
        (vm_dir / "disk.base.json").write_text(
            json.dumps(
                {
                    "base_sha256": base_sha,
                    "image_key": spec.key,
                    "created_at": __import__("time").time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

    return True, ""


def check_vm_disk_base(
    spec: "images.ImageSpec", vm_dir: Path, arch: str = "amd64"
) -> tuple[bool, str]:
    """校验 VM 增量层与其基础是否匹配

    返回 (是否匹配, 说明)。不匹配时调用方应提示用户重建该 VM —— 
    继续启动会得到内容错乱的 guest（SSH 连不上、库加载失败等），
    而不是可用的系统。
    """
    disk = vm_dir / "disk.qcow2"
    if not disk.exists():
        return False, "虚拟机磁盘不存在"

    record = vm_dir / "disk.base.json"
    if not record.exists():
        # 老版本创建的 VM 没有记录，无法判断；不阻断（有一次机会）
        return True, ""

    try:
        want = json.loads(record.read_text(encoding="utf-8")).get(
            "base_sha256", ""
        )
    except (json.JSONDecodeError, OSError):
        return True, ""

    if not want:
        return True, ""

    current = base_fingerprint(spec, arch)
    if not current:
        return False, "共享磁盘基础缺失，无法启动"

    if current != want:
        return False, (
            "共享磁盘基础已被替换（通常是重新下载了镜像或清理过缓存），"
            "本机的增量磁盘不再匹配。\n\n"
            "继续启动会导致系统文件错乱（能开机但服务异常、SSH 连不上）。\n"
            "请删除该虚拟机后重新创建。"
        )

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
