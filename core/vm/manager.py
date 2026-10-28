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


def _sync_ssh_config() -> None:
    """把 VM 列表同步到 ~/.ssh/config（失败不影响主流程）"""
    try:
        from core.vm import sshconfig

        sshconfig.sync_config(load_all())
    except Exception:
        pass


def used_host_ports() -> set[int]:
    """所有 VM 已占用的宿主机端口

    端口冲突检测不能只靠 bind() 探测：QEMU 的 hostfwd 是在用户态
    转发的，另一个 VM 已经用了 2222 时，本机 bind(2222) 依然成功
    （QEMU 监听的是它自己的 socket），所以必须显式收集已分配端口。
    """
    used: set[int] = set()
    try:
        for vm in load_all():
            for fwd in vm.spec.forwards:
                used.add(fwd.host_port)
    except Exception:
        pass
    return used


def find_free_port(
    start: int = SSH_PORT_BASE,
    count: int = SSH_PORT_RANGE,
    exclude: set[int] | None = None,
) -> int:
    """找一个可用端口（排除已被其它 VM 占用的）"""
    skip = exclude if exclude is not None else used_host_ports()
    for port in range(start, start + count):
        if port in skip:
            continue
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
        """保存配置：SQLite 为主，vm.json 同步写一份兼容旧版本"""
        self.dir.mkdir(parents=True, exist_ok=True)

        data = self.spec.to_dict()

        # SQLite（主存储）
        try:
            from core import state

            state.save_vm(data)
        except Exception as e:
            print(f"[vm] 写入 SQLite 失败，回退 json: {e}")

        # json（兼容 + 便于人工查看/迁移）
        try:
            self.config_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def delete_files(self) -> tuple[bool, str]:
        """删除整个 VM 目录（含 SQLite 记录）"""
        if self.running:
            return False, "请先停止虚拟机"
        try:
            shutil.rmtree(self.dir)
        except OSError as e:
            return False, str(e)

        try:
            from core import state

            state.delete_vm_row(self.spec.name)
        except Exception as e:
            print(f"[vm] 删除 SQLite 记录失败: {e}")

        return True, ""


# ─────────────────────────────────────────
# 加载
# ─────────────────────────────────────────
def load_all() -> list[VM]:
    """加载所有 VM

    SQLite 为主，目录里的 vm.json 作补充（兼容迁移前的数据）。
    两边都有的以 SQLite 为准（内容更新）。
    """
    out: dict[str, VM] = {}

    # 1. SQLite
    #
    # 必须校验磁盘目录仍然存在：SQLite 是主存储，但目录可能被外部
    # 删除（手工清理、脚本、或删除中途失败）。只信 SQLite 会在列表里
    # 留下「幽灵 VM」—— 点启动报目录不存在，也删不掉（看起来像卡死）。
    try:
        from core import state

        for data in state.load_all_vms():
            d = MACHINES_DIR / data["name"]
            if not d.is_dir():
                # 目录已不在，顺手把这条僵尸记录清掉（自愈）
                try:
                    state.delete_vm_row(data["name"])
                except Exception:
                    pass
                continue
            out[data["name"]] = VM(
                spec=qemu.VMSpec.from_dict(data), dir=d
            )
    except Exception as e:
        print(f"[vm] 读取 SQLite 失败: {e}")

    # 2. vm.json 补充（SQLite 里没有的）
    if MACHINES_DIR.exists():
        for d in sorted(MACHINES_DIR.iterdir()):
            if not d.is_dir() or d.name in out:
                continue
            cfg = d / "vm.json"
            if not cfg.exists():
                continue
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                out[data["name"]] = VM(
                    spec=qemu.VMSpec.from_dict(data), dir=d
                )
            except (json.JSONDecodeError, KeyError, OSError):
                continue

    return [out[k] for k in sorted(out)]


def load(name: str) -> VM | None:
    for vm in load_all():
        if vm.name == name:
            return vm
    return None


def name_taken(name: str) -> bool:
    """名称是否已被一台**有效**的虚拟机占用

    只认有 vm.json 的目录。创建中途失败可能留下空壳目录，
    旧版把空壳也算占用，导致用户换个名字也没用（每次都报
    「名称已被占用」），且无法自愈。
    空壳目录会在 create_vm 里被复用并重新填充。
    """
    return (MACHINES_DIR / name / "vm.json").exists()


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
    # 已存在说明是上次创建失败留下的空壳（无 vm.json，
    # name_taken 已确认不是有效 VM）。直接复用，rmtree 清底
    # 避免残留半个 seed.iso / disk 干扰本次创建。
    if vm_dir.exists():
        shutil.rmtree(vm_dir, ignore_errors=True)
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
    # 必须排除其它 VM 已占用的宿主机端口，否则多台 VM 会分到同一个
    # 端口（QEMU hostfwd 是用户态转发，bind() 探测不到已占用）。
    fwd_list = list(forwards or [])
    if not any(f.guest_port == 22 for f in fwd_list):
        taken = used_host_ports()
        # 用户显式指定的端口也要算占用
        for f in fwd_list:
            taken.add(f.host_port)
        try:
            ssh_port = find_free_port(exclude=taken)
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
    ok, err = shared_disk.ensure_shared_base(
        spec_img, arch, on_log=log
    )
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

    # 同步 SSH 客户端配置，让外部能直接 ssh <名字>.vm.local
    _sync_ssh_config()

    log(f"创建完成：{name}")
    return vm, ""


def cleanup_orphan_dirs() -> int:
    """清理残留状态（启动时调一次，返回处理掉的条目数）

    两个方向都要清：
      · 空壳目录 —— 目录在、没有 vm.json，也没有活着的 QEMU 进程
      · 幽灵记录 —— SQLite 里有行，磁盘目录却已被删除
    只清一个方向会让界面和磁盘长期不一致（列表里点不动的死条目，
    或永远不出现的目录）。
    """
    n = 0

    # ── 幽灵记录：SQLite 有行但目录没了 ──
    try:
        from core import state

        for data in state.load_all_vms():
            if not (MACHINES_DIR / data["name"]).is_dir():
                try:
                    state.delete_vm_row(data["name"])
                    print(f"[vm] 清理幽灵记录: {data['name']}")
                    n += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"[vm] 清理幽灵记录失败: {e}")

    if not MACHINES_DIR.exists():
        return n
    for d in MACHINES_DIR.iterdir():
        if not d.is_dir():
            continue
        # 有效 VM：有 vm.json 或有 QEMU 进程（异常情况保底）
        if (d / "vm.json").exists():
            continue
        pid_file = d / "qemu.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)   # 探活，不杀
                continue          # 进程还在，别动
            except (ValueError, ProcessLookupError, PermissionError):
                pass              # 进程已死，按空壳处理
            except OSError:
                continue
        shutil.rmtree(d, ignore_errors=True)
        print(f"[vm] 清理空壳目录: {d.name}")
        n += 1
    return n


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

    # 端口已被占用 = 很可能有残留的 QEMU 在跑（pidfile 丢了、
    # 或上一轮启动的进程还没完全退出）。
    # 直接再启动会报 "Could not set up host forwarding rule"，
    # 用户看到的是「启动失败」，但机器其实在工作 —— 极易误导。
    # 这里先确认一下：如果端口上已经有 SSH banner，就当它已启动。
    if vm.ssh_port and not port_available(vm.ssh_port):
        import socket as _socket

        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect(("127.0.0.1", vm.ssh_port))
            banner = s.recv(64)
            s.close()
            if b"SSH-" in banner:
                return True, "已在运行"
        except OSError:
            pass

    # ── 增量层与共享基础的指纹校验 ──
    # qcow2 增量层只能配它创建时的那份基础。
    # 基础被替换过（重新下载镜像、清理共享缓存）时，
    # 新旧错位会让 guest 的文件系统内容错乱 —— 表现为能开机但
    # 服务起不来、SSH 连不上，从宿主机侧极难定位。
    # 这里提前拦下并给出可执行的提示。
    spec_img = images.get_spec(vm.spec.image_key)
    if spec_img is not None:
        matched, why = shared_disk.check_vm_disk_base(
            spec_img, vm.dir, vm.spec.arch
        )
        if not matched:
            return False, why

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


def _ask_daemon_release(vm: VM, wait: float = 3.0) -> None:
    """请守护进程释放该 VM 的唤醒代理端口

    GUI 要启动 VM 时，守护进程可能正用代理占着同一端口。
    不先释放，QEMU 启动会报
    "Could not set up host forwarding rule"。

    做法：
      1. 往 state 写一个「交接中」标记，守护进程下一轮巡检会看到
      2. 轮询等待端口真正空出来（最多 wait 秒）
    """
    import socket as _socket
    import time as _t

    name = vm.spec.name
    port = vm.ssh_port
    if not port:
        return

    try:
        from core import state

        state.set_setting(f"vm.handoff.{name}", _t.time())
    except Exception:
        pass

    deadline = _t.time() + wait
    while _t.time() < deadline:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            return          # 端口已空
        except OSError:
            _t.sleep(0.2)
        finally:
            s.close()


def soft_stop(vm: VM, timeout: int = 30) -> tuple[bool, str]:
    """休眠：把内存状态存到磁盘，然后退出 QEMU

    这是真休眠，不是关机 —— guest 的进程、内存、打开的文件
    全部冻结进快照，恢复时原样继续。

    与 stop_vm 的区别：
      · stop_vm  = 用户主动停止（ACPI 关机，意图变为「不要运行」）
      · soft_stop = 系统自动休眠（保存内存快照，意图仍是「要运行」）

    实测（1GB Rocky）：保存约 5–9s，恢复约 1s；
    对比冷启动 19s，恢复快约 20 倍。

    快照失败时退化为普通关机，不影响正确性（只是下次要冷启动）。
    """
    if not vm.running:
        return True, "未在运行"

    # 快照前必须把共享目录卸掉。
    #
    # 原因：virtiofs 的 inode 编号由 virtiofsd 进程维护，而快照
    # 只保存 guest 内存，不保存 virtiofsd 的内部状态。恢复后
    # guest 仍拿旧的 inode 去 lookup，新的 virtiofsd 会解析成
    # 完全不同的文件 —— 实测 /mnt/host/download 里显示出了
    # /tmp 的 .X11-unix，属于静默的数据错乱，比挂载失败更危险。
    #
    # 卸载后 guest 的 dentry/inode 缓存被丢弃，恢复后重新挂载
    # 就是一次全新的会话，编号自然对齐。
    if vm.spec.shares:
        try:
            from core.vm import ssh as _ssh

            _ssh.unmount_all_shares(vm)
        except Exception:
            pass

    ok, msg = qemu.snapshot_save(vm.dir, timeout=max(timeout, 180))
    if ok:
        return True, ""

    # 快照不可用（内核不支持迁移、磁盘满等）→ 退化为正常关机
    import logging

    logging.getLogger(__name__).warning(
        "VM %s 内存快照失败，退化为关机: %s", vm.spec.name, msg
    )
    qemu.drop_snapshot(vm.dir)
    return qemu.stop(vm.dir, timeout=timeout)


def resume_vm(vm: VM) -> tuple[bool, str]:
    """从休眠恢复

    有内存快照 → 秒级恢复（约 1s）；
    没有快照（或快照失效）→ 退回冷启动（约 19s）。
    """
    if vm.running:
        return True, "已在运行"

    # 守护进程可能正用唤醒代理占着端口，先让它释放
    _ask_daemon_release(vm)

    if qemu.has_snapshot(vm.dir):
        ok, msg = qemu.snapshot_restore(vm.dir)
        if ok:
            # 快照前卸载过共享目录，恢复后要重新挂上
            # （见 soft_stop 里的说明：不重挂会读到错乱的文件）
            if vm.spec.shares:
                try:
                    from core.vm import ssh as _ssh

                    for _ in range(30):
                        o, _e = _ssh.test_connection(vm, timeout=5)
                        if o:
                            break
                        time.sleep(1)
                    _ssh.mount_all_shares(vm)
                except Exception:
                    pass
            return True, ""
        # 快照坏了 → 丢掉，冷启动兜底
        import logging

        logging.getLogger(__name__).warning(
            "VM %s 从快照恢复失败，改为冷启动: %s", vm.spec.name, msg
        )
        qemu.drop_snapshot(vm.dir)

    return start_vm(vm)


def desired_running_vms() -> list[VM]:
    """列出「用户希望其运行」的 VM

    用于应用启动时自动恢复上次的运行状态：
    QEMU 用 -daemonize 独立运行，应用退出不会停 VM，
    因此正常退出再启动时这些 VM 仍在跑；只有应用被强杀、
    或系统重启后，才需要在这里重新拉起。
    """
    from core import state

    out = []
    for vm in load_all():
        try:
            want = state.get_setting(f"vm.desired_running.{vm.name}", False)
        except Exception:
            want = False
        if want:
            out.append(vm)
    return out


def restore_desired_vms() -> list[tuple[str, bool, str]]:
    """应用启动时调用：把「希望运行但当前没跑」的 VM 拉起来

    返回 [(名字, 是否成功, 消息)]
    已在运行的会被跳过（不产生返回值），避免打断用户正在用的 VM。
    """
    results: list[tuple[str, bool, str]] = []

    for vm in desired_running_vms():
        if vm.running:
            continue
        ok, err = start_vm(vm)
        results.append((vm.name, ok, err))

    return results


def restart_vm(vm: VM, timeout: int = 30) -> tuple[bool, str]:
    """重启虚拟机：先停止再启动

    返回 (成功, 错误消息)。成功时消息为空。
    """
    if not vm.running:
        # 没运行就当直接启动
        return start_vm(vm)

    ok, err = stop_vm(vm, timeout=timeout)
    if not ok:
        return False, f"停止失败: {err}"

    ok, err = start_vm(vm)
    if not ok:
        return False, f"启动失败: {err}"

    return True, ""


def delete_vm(vm: VM, *, remove_disk: bool = True) -> tuple[bool, str]:
    """删除 VM"""
    if vm.running:
        ok, err = qemu.kill(vm.dir)
        if not ok:
            return False, f"无法停止: {err}"

    if remove_disk:
        ok, err = vm.delete_files()
        if ok:
            _sync_ssh_config()
        return ok, err

    # 只删配置，保留磁盘
    try:
        vm.config_file.unlink(missing_ok=True)
        (vm.dir / "seed.iso").unlink(missing_ok=True)
        (vm.dir / "qemu.pid").unlink(missing_ok=True)
        _sync_ssh_config()
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
    _sync_ssh_config()
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
    _sync_ssh_config()
    return True, ""


# ─────────────────────────────────────────
# 共享目录
# ─────────────────────────────────────────
def add_share(vm: VM, host_path: str, tag: str) -> tuple[bool, str]:
    """添加共享目录（需重启 VM 生效）"""
    # 严格校验：必须是绝对路径、真实存在的目录。
    # 相对路径（如用户手输的 "download"）会被解析到进程 cwd，
    # 结果挂进 VM 的是一个不存在的路径 —— 表现为「挂载成功但里面是空的」。
    if not host_path or not host_path.startswith("/"):
        return False, f"必须是绝对路径（如 /home/{Path.home().name}/下载）"

    p = Path(host_path).expanduser().resolve()
    if not p.is_dir():
        return False, f"目录不存在: {host_path}"

    host_path = str(p)

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
def rename_vm(vm: VM, new_name: str) -> tuple[bool, str]:
    """重命名虚拟机

    需要同步四处，漏一处就会状态不一致：
      1. 机器目录名 machines/<old> → machines/<new>
      2. VMSpec.name 与 vm.json
      3. SQLite 记录（主键是 name，得删旧插新）
      4. ~/.ssh/config 里的标记段
    """
    import re
    import shutil

    new_name = (new_name or "").strip()
    if not new_name:
        return False, "名称不能为空"

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", new_name):
        return False, "名称只能含字母、数字、点、下划线、短横线，且以字母或数字开头"

    if new_name == vm.spec.name:
        return True, ""

    if name_taken(new_name):
        return False, "名称「" + new_name + "」已被占用"

    if vm.running:
        return False, "请先停止虚拟机"

    old_name = vm.spec.name
    old_dir = vm.dir
    new_dir = MACHINES_DIR / new_name

    if new_dir.exists():
        return False, "目录已存在: " + str(new_dir)

    # 1) 目录改名
    try:
        old_dir.rename(new_dir)
    except OSError as e:
        return False, f"目录改名失败: {e}"

    # 2) 配置
    vm.spec.name = new_name
    vm.spec.hostname = new_name        # hostname 跟随名称
    new_vm = VM(spec=vm.spec, dir=new_dir)
    new_vm.save()

    # 3) SQLite：删旧行（新名字已由 save 插入）
    try:
        from core import state

        state.delete_vm_row(old_name)
    except Exception as e:
        print(f"[vm] 清理旧 SQLite 记录失败: {e}")

    # 4) SSH 配置
    _sync_ssh_config()

    return True, ""


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

    _sync_ssh_config()

    return len(vms), freed, "；".join(errors)


def vm_data_size() -> tuple[int, int]:
    """虚拟机相关数据占用

    返回 (VM 总字节, 镜像缓存字节)
    """
    from core.vm import images as _images

    vm_bytes = total_usage()
    img_bytes = _images.cache_size()
    return vm_bytes, img_bytes
