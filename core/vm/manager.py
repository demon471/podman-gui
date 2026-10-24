"""虚拟机生命周期管理

职责：
  · VM 目录布局与持久化（vm.json / catalog.json）
  · 创建：下载镜像 → 生成密钥 → 生成 seed → 创建磁盘
  · 启动 / 停止 / 删除
  · 端口分配、hosts 域名维护

目录布局：
  ~/.local/share/podman-gui/vm/
  ├── images/                    # 共享的 base image 缓存
  └── machines/
      └── ubuntu-dev/
          ├── vm.json            # 配置
          ├── disk.qcow2         # 系统盘
          ├── seed.iso           # cloud-init
          ├── ssh_key            # 私钥
          ├── ssh_key.pub
          ├── console.log
          └── qemu.pid
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.vm import (
    cloudinit,
    hostinfo,
    images,
    qemu,
    shared_disk,
)

# 数据根目录
VM_ROOT = Path.home() / ".local" / "share" / "podman-gui" / "vm"
MACHINES_DIR = VM_ROOT / "machines"

# SSH 端口起始值（避免与常见端口冲突）
SSH_PORT_BASE = 2222
SSH_PORT_RANGE = 200


# ─────────────────────────────────────────
# 端口工具
# ─────────────────────────────────────────
def port_available(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否可绑定"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
            return True
    except OSError:
        return False


def find_free_port(start: int = SSH_PORT_BASE, count: int = SSH_PORT_RANGE) -> int:
    """找一个可用端口"""
    for port in range(start, start + count):
        if port_available(port):
            return port
    raise RuntimeError(f"{start}~{start + count} 范围内无可用端口")


# ─────────────────────────────────────────
# VM 对象
# ─────────────────────────────────────────
@dataclass
class VM:
    """虚拟机（配置 + 目录）"""

    spec: qemu.VMSpec
    dir: Path

    # ── 路径 ──
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def disk(self) -> Path:
        return self.dir / "disk.qcow2"

    @property
    def seed(self) -> Path:
        return self.dir / "seed.iso"

    @property
    def ssh_key(self) -> Path:
        return self.dir / "ssh_key"

    @property
    def config_file(self) -> Path:
        return self.dir / "vm.json"

    # ── 状态 ──
    @property
    def running(self) -> bool:
        return qemu.is_running(self.dir)

    @property
    def uptime(self) -> int:
        return qemu.uptime_seconds(self.dir)

    @property
    def ssh_port(self) -> int:
        """SSH 转发端口"""
        for f in self.spec.forwards:
            if f.guest_port == 22:
                return f.host_port
        return 0

    def ssh_command(self) -> str:
        port = self.ssh_port
        if not port:
            return ""
        return (
            f"ssh -i {self.ssh_key} -p {port} "
            f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f"{self.spec.username}@127.0.0.1"
        )

    def http_forwards(self) -> list[qemu.PortForward]:
        """Web 类端口（80/443/8080 等）"""
        return [
            f for f in self.spec.forwards
            if f.guest_port in (80, 443, 3000, 8080, 8000, 5000)
        ]

    # ── 持久化 ──
    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(
            json.dumps(self.spec.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete_files(self) -> tuple[bool, str]:
        """删除整个 VM 目录"""
        if self.running:
            return False, "请先停止虚拟机"
        try:
            shutil.rmtree(self.dir)
            return True, ""
        except OSError as e:
            return False, str(e)


# ─────────────────────────────────────────
# 加载
# ─────────────────────────────────────────
def load_all() -> list[VM]:
    """加载所有 VM"""
    if not MACHINES_DIR.exists():
        return []

    out = []
    for d in sorted(MACHINES_DIR.iterdir()):
        if not d.is_dir():
            continue
        cfg = d / "vm.json"
        if not cfg.exists():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            out.append(VM(spec=qemu.VMSpec.from_dict(data), dir=d))
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return out


def load(name: str) -> VM | None:
    for vm in load_all():
        if vm.name == name:
            return vm
    return None


def name_taken(name: str) -> bool:
    return (MACHINES_DIR / name).exists()


# ─────────────────────────────────────────
# 创建
# ─────────────────────────────────────────
def create_vm(
    *,
    name: str,
    image_key: str,
    username: str = "",
    forwards: list[qemu.PortForward] | None = None,
    shares: list[tuple[str, str]] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[VM | None, str]:
    """创建虚拟机

    用户只需提供名称和镜像，其余全部用默认值：
      · 磁盘：所有 VM 共用一个只读基础（稀疏 qcow2 backing file），
        每台 VM 只有一个增量层，按需增长，无需选大小
      · 内存 / CPU：用镜像的推荐值（见 ImageSpec.default_*）

    返回 (VM, 错误信息)
    """
    log = on_log or (lambda _m: None)

    # ── 校验 ──
    if not name or "/" in name or name.startswith("."):
        return None, "名称无效（不能包含 / 或以 . 开头）"

    if name_taken(name):
        return None, f"名称 {name} 已存在"

    spec_img = images.get_spec(image_key)
    if spec_img is None:
        return None, f"未知镜像: {image_key}"

    arch = "amd64"      # TODO: 支持 arm64 宿主机
    if not qemu.qemu_binary(arch):
        return None, "QEMU 未安装，请先安装 qemu-system-x86_64"

    if not cloudinit.iso_tool_available():
        return None, "缺少 ISO 生成工具（xorriso），无法生成 cloud-init 配置"

    vm_dir = MACHINES_DIR / name
    vm_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 准备 base image ──
    log("检查镜像…")
    if not images.is_downloaded(spec_img, arch):
        log(f"下载 {spec_img.name}…")
        ok, err = images.download_image(
            spec_img, arch, on_progress=on_progress, on_log=on_log
        )
        if not ok:
            shutil.rmtree(vm_dir, ignore_errors=True)
            return None, f"镜像下载失败: {err}"
    else:
        log("镜像已缓存")

    # ── 2. 生成 SSH 密钥 ──
    log("生成 SSH 密钥…")
    key_path = vm_dir / "ssh_key"
    ok, err = cloudinit.generate_ssh_key(key_path)
    if not ok:
        shutil.rmtree(vm_dir, ignore_errors=True)
        return None, f"密钥生成失败: {err}"

    pubkey = cloudinit.read_pubkey(key_path)
    if not pubkey:
        shutil.rmtree(vm_dir, ignore_errors=True)
        return None, "读取公钥失败"

    # ── 3. 分配 SSH 端口 ──
    fwd_list = list(forwards or [])
    if not any(f.guest_port == 22 for f in fwd_list):
        try:
            ssh_port = find_free_port()
        except RuntimeError as e:
            shutil.rmtree(vm_dir, ignore_errors=True)
            return None, str(e)
        fwd_list.insert(0, qemu.PortForward(ssh_port, 22))

    # ── 4. 生成 cloud-init seed ──
    # 用户名 / 时区 / locale / DNS 全部从宿主机继承（不写死），
    # 让 VM 与宿主机保持一致的使用习惯。
    log("生成 cloud-init 配置…")
    host = hostinfo.host_profile()
    vm_user = username or host.username
    ok, err = cloudinit.build_seed_iso(
        vm_dir / "seed.iso",
        username=vm_user,
        ssh_pubkey=pubkey,
        hostname=name,
        instance_id=f"{name}-{int(time.time())}",
        family=spec_img.family,
        timezone=host.timezone,
        locale=host.locale,
        dns=host.dns,
    )
    if not ok:
        shutil.rmtree(vm_dir, ignore_errors=True)
        return None, f"cloud-init 生成失败: {err}"

    # ── 5. 准备磁盘（共享基础 + 本机增量层）──
    # 共享基础首次创建，之后所有 VM 复用；
    # 每台 VM 的 disk.qcow2 只记录自己的改动，毫秒级完成。
    ok, err = shared_disk.ensure_shared_base(spec_img, arch, on_log=log)
    if not ok:
        shutil.rmtree(vm_dir, ignore_errors=True)
        return None, f"磁盘准备失败: {err}"

    log("创建虚拟机磁盘…")
    ok, err = shared_disk.create_vm_disk(spec_img, vm_dir, arch)
    if not ok:
        shutil.rmtree(vm_dir, ignore_errors=True)
        return None, f"磁盘创建失败: {err}"

    # ── 6. 保存配置 ──
    spec = qemu.VMSpec(
        name=name,
        image_key=image_key,
        arch=arch,
        memory_mb=spec_img.default_mem_mb,
        cpus=spec_img.default_cpus,
        disk_gb=shared_disk.DEFAULT_INCREMENTAL_GB,
        username=vm_user,
        hostname=name,
        forwards=fwd_list,
        shares=list(shares or []),
        created_at=time.time(),
    )
    vm = VM(spec=spec, dir=vm_dir)
    vm.save()

    log(f"创建完成：{name}")
    return vm, ""


def _create_disk(base: Path, target: Path, size_gb: int) -> tuple[bool, str]:
    """用 qemu-img 基于 base image 创建 COW 磁盘"""
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        return False, "未找到 qemu-img"

    try:
        # 创建基于 base 的 overlay（写时复制，节省空间）
        r = subprocess.run(
            [
                qemu_img, "create",
                "-f", "qcow2",
                "-F", "qcow2",
                "-b", str(base),
                str(target),
                f"{size_gb}G",
            ],
            capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)

    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:300]

    return True, ""


# ─────────────────────────────────────────
# 启动 / 停止
# ─────────────────────────────────────────
def start_vm(vm: VM, *, vnc: int | None = None, check_memory: bool = True) -> tuple[bool, str]:
    """启动 VM

    check_memory=True 时会检查宿主机可用内存，
    不足时返回错误（避免 OOM）。
    """
    if vm.running:
        return True, "已在运行"

    # 内存检查
    if check_memory:
        from core.vm import memory as mem_mod

        ok, msg = mem_mod.check_memory_before_start(vm.spec.memory_mb)
        if not ok:
            return False, msg
        # 警告类消息不阻断，但记日志
        if msg:
            import logging
            logging.getLogger(__name__).warning(msg)

    return qemu.start(vm.spec, vm.dir, vnc=vnc)


def stop_vm(vm: VM, timeout: int = 30) -> tuple[bool, str]:
    """停止 VM"""
    return qemu.stop(vm.dir, timeout=timeout)


def delete_vm(vm: VM, *, remove_disk: bool = True) -> tuple[bool, str]:
    """删除 VM"""
    if vm.running:
        ok, err = qemu.kill(vm.dir)
        if not ok:
            return False, f"无法停止: {err}"

    if remove_disk:
        return vm.delete_files()

    # 只删配置，保留磁盘
    try:
        vm.config_file.unlink(missing_ok=True)
        (vm.dir / "seed.iso").unlink(missing_ok=True)
        (vm.dir / "qemu.pid").unlink(missing_ok=True)
        return True, ""
    except OSError as e:
        return False, str(e)


# ─────────────────────────────────────────
# 端口管理
# ─────────────────────────────────────────
def add_forward(vm: VM, host_port: int, guest_port: int,
                protocol: str = "tcp") -> tuple[bool, str]:
    """添加端口转发（需重启 VM 生效）"""
    if not port_available(host_port):
        return False, f"宿主机端口 {host_port} 已被占用"

    for f in vm.spec.forwards:
        if f.host_port == host_port:
            return False, f"端口 {host_port} 已配置"

    vm.spec.forwards.append(
        qemu.PortForward(host_port, guest_port, protocol)
    )
    vm.save()
    return True, ""


def remove_forward(vm: VM, host_port: int) -> tuple[bool, str]:
    """移除端口转发"""
    before = len(vm.spec.forwards)
    vm.spec.forwards = [
        f for f in vm.spec.forwards if f.host_port != host_port
    ]
    if len(vm.spec.forwards) == before:
        return False, "未找到该端口"
    vm.save()
    return True, ""


# ─────────────────────────────────────────
# 共享目录
# ─────────────────────────────────────────
def add_share(vm: VM, host_path: str, tag: str) -> tuple[bool, str]:
    """添加共享目录（需重启 VM 生效）"""
    p = Path(host_path)
    if not p.is_dir():
        return False, f"目录不存在: {host_path}"

    if any(s[1] == tag for s in vm.spec.shares):
        return False, f"挂载点 {tag} 已存在"

    if not tag.replace("_", "").replace("-", "").isalnum():
        return False, "挂载点只能用字母、数字、下划线和连字符"

    vm.spec.shares.append((str(p), tag))
    vm.save()
    return True, ""


def remove_share(vm: VM, tag: str) -> tuple[bool, str]:
    before = len(vm.spec.shares)
    vm.spec.shares = [s for s in vm.spec.shares if s[1] != tag]
    if len(vm.spec.shares) == before:
        return False, "未找到该共享"
    vm.save()
    return True, ""


# ─────────────────────────────────────────
# 统计
# ─────────────────────────────────────────
def disk_usage(vm: VM) -> int:
    """VM 磁盘实际占用（字节）"""
    total = 0
    for f in vm.dir.rglob("*"):
        if f.is_file() and f.name not in ("qemu.pid",):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def total_usage() -> int:
    """所有 VM 占用"""
    return sum(disk_usage(vm) for vm in load_all())


# ─────────────────────────────────────────
# 一键清空
# ─────────────────────────────────────────
def purge_all(*, include_images: bool = True) -> tuple[int, int, str]:
    """清空所有虚拟机及相关数据

    删除内容：
      · 所有 VM（含磁盘、配置、cloud-init seed、日志）
      · 可选的镜像缓存（include_images=True）
      · 共享磁盘基础

    VM 会先被强制停止。正在运行的 QEMU 进程被 kill 后，
    qcow2 文件才可安全删除。

    返回 (删除的 VM 数, 释放的字节数, 错误信息)
    错误信息非空表示部分删除失败，但不影响已删部分。
    """
    import shutil as _sh

    vms = load_all()
    freed = 0
    errors = []

    for vm in vms:
        if vm.running:
            qemu.kill(vm.dir)
        try:
            freed += disk_usage(vm)
        except Exception:
            pass

        ok, err = delete_vm(vm, remove_disk=True)
        if not ok:
            # delete_vm 内部可能因进程未退出而失败，直接删目录兜底
            _sh.rmtree(vm.dir, ignore_errors=True)
            if vm.dir.exists():
                errors.append(f"{vm.name}: {err}")

    # 共享磁盘基础
    try:
        from core.vm import images as _images

        for spec in _images.list_images():
            shared_disk.clear_shared_base(spec)
    except Exception as e:
        errors.append(f"清理共享磁盘: {e}")

    # 镜像缓存（下载的发行版镜像，可重新下载）
    if include_images:
        try:
            from core.vm import images as _images

            for spec in _images.list_images():
                p = _images.image_path(spec)
                if p.exists():
                    try:
                        freed += p.stat().st_size
                    except OSError:
                        pass
                    _images.delete_image(spec)
        except Exception as e:
            errors.append(f"清理镜像缓存: {e}")

    return len(vms), freed, "；".join(errors)


def vm_data_size() -> tuple[int, int]:
    """虚拟机相关数据占用

    返回 (VM 总字节, 镜像缓存字节)
    """
    from core.vm import images as _images

    vm_bytes = total_usage()
    img_bytes = _images.cache_size()
    return vm_bytes, img_bytes
