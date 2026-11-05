"""机器管理（纯容器版）

容器挂在共享网桥 pgbr0 上，每台从 dnsmasq 租约拿到唯一 IP：
  · 宿主机直接 ssh <内部 IP>
  · 机器之间互通
  · 外部客户端走 ~/.ssh/config 里的 <name>.vm.local

没有 QEMU 了：没有端口转发分配、没有磁盘层、没有快照、
没有 balloon。stop 即真休眠（rootfs 保留状态）。
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
    bridge,
    container,
    hostinfo,
    images,
)
from core.vm.spec import DEFAULT_PASSWORD, PortForward, VMSpec

# 数据根目录（默认位置；用户可在「设置 → 存储」里改）
DEFAULT_VM_ROOT = Path.home() / ".local" / "share" / "podbox" / "vm"
KEY_VM_ROOT = "vm.root_dir"


def vm_root() -> Path:
    """机器数据根目录（可配置）"""
    try:
        from core import state

        val = state.get_setting(KEY_VM_ROOT, "")
    except Exception:
        val = ""
    if val:
        try:
            return Path(str(val)).expanduser()
        except (OSError, ValueError):
            pass
    return DEFAULT_VM_ROOT


def machines_dir() -> Path:
    """机器实例目录"""
    return vm_root() / "machines"


def set_vm_root(path: str, move: bool = True) -> tuple[bool, str]:
    """更改数据目录

    move=True 时把现有机器搬到新位置。
    有机器在运行时拒绝操作 —— 搬正在写的 rootfs 必然损坏。
    """
    import shutil

    from core import state

    raw = (path or "").strip()
    if not raw:
        return False, "目录不能为空"

    try:
        target = Path(raw).expanduser()
    except (OSError, ValueError) as e:
        return False, f"路径无效: {e}"

    if not target.is_absolute():
        return False, "请使用绝对路径"

    old_root = vm_root()
    if target == old_root:
        return False, "新目录与当前目录相同"

    for vm in load_all():
        if vm.running:
            return False, f"机器「{vm.spec.name}」正在运行，请先停止"

    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"无法创建目录: {e}"

    if not os.access(target, os.W_OK):
        return False, f"{target} 不可写"

    if move and old_root.exists():
        src_dir = old_root / "machines"
        if src_dir.is_dir():
            dst_dir = target / "machines"
            try:
                dst_dir.mkdir(parents=True, exist_ok=True)
                for item in src_dir.iterdir():
                    dest = dst_dir / item.name
                    if dest.exists():
                        return False, f"{dest} 已存在，请先清理"
                    shutil.move(str(item), str(dest))
            except OSError as e:
                return False, f"迁移 machines 失败: {e}"

    state.set_setting(KEY_VM_ROOT, str(target))
    return True, str(target)


# ─────────────────────────────────────────
# 端口工具（保留：界面端口记录仍可能用到）
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
    """把机器列表同步到 ~/.ssh/config（失败不影响主流程）"""
    try:
        from core.vm import sshconfig

        sshconfig.sync_config(load_all())
    except Exception:
        pass


# ─────────────────────────────────────────
# 机器对象
# ─────────────────────────────────────────
@dataclass
class VM:
    """机器（配置 + 目录）"""

    spec: VMSpec
    dir: Path

    # ── 路径 ──
    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def rootfs(self) -> Path:
        return self.dir / container.ROOTFS_DIR_NAME

    @property
    def lxc_conf(self) -> Path:
        return self.dir / container.LXC_CONF_NAME

    @property
    def ssh_key(self) -> Path:
        return self.dir / "ssh_key"

    @property
    def config_file(self) -> Path:
        return self.dir / "vm.json"

    # ── 状态 ──
    @property
    def running(self) -> bool:
        return container.is_running(machines_dir(), self.spec.name)

    @property
    def uptime(self) -> int:
        return container.uptime_seconds(machines_dir(), self.spec.name)

    @property
    def ssh_port(self) -> int:
        """SSH 端口：容器内 sshd 固定 22（网桥直通，无转发概念）

        不依赖运行时网桥状态：展示地址不该因为宿主网桥
        还没初始化就变空。
        """
        return 22

    @property
    def ssh_host(self) -> str:
        """SSH 目标主机 = 机器自己的 IP（宿主机可达）"""
        if bridge.ready():
            # MAC 优先（租约里主机名常为 "*"），名字兜底
            mac = container.conf_mac(self.dir)
            ip = bridge.lease_ip(mac) if mac else ""
            if not ip:
                ip = bridge.lease_ip(self.spec.name)
            if ip:
                return ip
        return "127.0.0.1"

    def ssh_command(self) -> str:
        """给用户复制用的 SSH 命令：短域名形式

        前提：~/.ssh/config 已由 sshconfig.sync_config 写好
        （HostName=机器 IP / User / IdentityFile 全配置），
        所以命令可以短到 `ssh <名字>.vm.local`。
        之前显示的是一长串 -i + -o 参数 —— 与实际写入的配置
        不一致，用户复制出来和界面上看到的对不上。
        """
        from core.vm.dns import vm_domain

        return f"ssh {vm_domain(self.spec.name)}"

    def http_forwards(self) -> list[PortForward]:
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

        try:
            from core import state

            state.save_vm(data)
        except Exception as e:
            print(f"[vm] 写入 SQLite 失败，回退 json: {e}")

        try:
            self.config_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def delete_files(self) -> tuple[bool, str]:
        """删除整个机器目录（含 SQLite 记录）

        rootfs 里有属于 subuid 段的文件（家目录被 chown 成
        容器内 uid），宿主用户直接 rmtree 会 EACCES。先试普通
        删除，失败就到用户命名空间里删（那里这些文件是「自己的」）。
        """
        if self.running:
            return False, "请先停止机器"

        try:
            shutil.rmtree(self.dir)
        except OSError:
            ok, err = container.rm_in_userns(self.dir)
            if not ok:
                return False, err

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
    """加载所有机器

    SQLite 为主，目录里的 vm.json 作补充（兼容迁移前的数据）。
    目录被外部删除的 SQLite 记录顺手清掉（自愈，防幽灵条目）。
    """
    out: dict[str, VM] = {}

    try:
        from core import state

        for data in state.load_all_vms():
            d = machines_dir() / data["name"]
            if not d.is_dir():
                try:
                    state.delete_vm_row(data["name"])
                except Exception:
                    pass
                continue
            out[data["name"]] = VM(
                spec=VMSpec.from_dict(data), dir=d
            )
    except Exception as e:
        print(f"[vm] 读取 SQLite 失败: {e}")

    if machines_dir().exists():
        for d in sorted(machines_dir().iterdir()):
            if not d.is_dir() or d.name in out:
                continue
            cfg = d / "vm.json"
            if not cfg.exists():
                continue
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                out[data["name"]] = VM(
                    spec=VMSpec.from_dict(data), dir=d
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
    """名称是否已被一台**有效**的机器占用

    只认有 vm.json 的目录。创建中途失败留下的空壳目录
    会在 create_vm 里被复用并重新填充。
    """
    return (machines_dir() / name / "vm.json").exists()


# ─────────────────────────────────────────
# 创建
# ─────────────────────────────────────────
def _generate_ssh_key(key_path: Path) -> tuple[bool, str]:
    """生成 ed25519 SSH 密钥对"""
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-f", str(key_path), "-C", "podbox"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:200]
    return True, ""


def _password_hash(password: str) -> str:
    """生成 shadow 格式的密码哈希（$6$ SHA-512）

    Python 3.13+ 移除了 crypt 模块，走 openssl。
    密码经 stdin 传入，不出现在命令行参数里（ps 可见）。
    """
    r = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=password, capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "openssl 失败").strip()[:200])
    return r.stdout.strip()


def create_vm(
    *,
    name: str,
    image_key: str,
    username: str = "",
    forwards: list[PortForward] | None = None,
    shares: list[tuple[str, str]] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[VM | None, str]:
    """创建机器（容器）

    用户只需提供名称和发行版，其余全部默认：
      · 用户名 = 宿主机用户名（与宿主保持一致的使用习惯）
      · 密码 = DEFAULT_PASSWORD（详情页可改）
      · 网络 = 共享网桥（创建前必须已启用）

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

    ok, err = container.prerequisites()
    if not ok:
        return None, err

    if not bridge.ready():
        return None, (
            "共享网络未启用：容器需要挂在共享网桥上才能拿到 IP。\n"
            "请到 设置 → 服务 → 虚拟机网络 先启用。"
        )

    arch = "amd64"      # TODO: 支持 arm64 宿主机
    vm_dir = machines_dir() / name
    # 空壳目录（上次创建失败残留）复用前清底。
    # 里面可能残留 subuid 属主的文件（上一轮把家目录 chown 给了
    # 容器内 uid），普通 rmtree 删不掉，必须走用户命名空间 ——
    # 否则「创建失败 → 重试」会一直卡在 Permission denied。
    if vm_dir.exists():
        shutil.rmtree(vm_dir, ignore_errors=True)
        if vm_dir.exists():
            container.rm_in_userns(vm_dir)
    vm_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. 下载 / 解包 rootfs ──
    log("准备发行版 rootfs…")
    if not images.rootfs_downloaded(spec_img, arch):
        log(f"下载 {spec_img.name} rootfs…")
        ok, err = images.download_rootfs(
            spec_img, arch, on_progress=on_progress, on_log=on_log
        )
        if not ok:
            _cleanup_partial(vm_dir)
            return None, f"rootfs 下载失败: {err}"

    log("解包 rootfs…")
    ok, err = images.extract_rootfs(
        spec_img, arch, vm_dir / container.ROOTFS_DIR_NAME
    )
    if not ok:
        _cleanup_partial(vm_dir)
        return None, f"rootfs 解包失败: {err}"

    # ── 2. SSH 密钥 ──
    log("生成 SSH 密钥…")
    key_path = vm_dir / "ssh_key"
    ok, err = _generate_ssh_key(key_path)
    if not ok:
        _cleanup_partial(vm_dir)
        return None, f"密钥生成失败: {err}"

    pubkey = key_path.with_suffix(".pub").read_text(encoding="utf-8")

    # ── 3. 播种（用户 / 密码 / hostname / ssh 公钥）──
    host = hostinfo.host_profile()
    vm_user = username or host.username
    log("配置系统（用户 / 密码 / SSH）…")
    try:
        pwhash = _password_hash(DEFAULT_PASSWORD)
    except RuntimeError as e:
        _cleanup_partial(vm_dir)
        return None, f"密码哈希生成失败: {e}"

    ok, err = container.seed_rootfs(
        vm_dir / container.ROOTFS_DIR_NAME,
        hostname=name,
        username=vm_user,
        ssh_pubkey=pubkey.strip(),
        password_hash=pwhash,
    )
    if not ok:
        _cleanup_partial(vm_dir)
        return None, f"系统配置失败: {err}"

    # ── 4. lxc 配置 ──
    spec = VMSpec(
        name=name,
        image_key=image_key,
        arch=arch,
        username=vm_user,
        hostname=name,
        forwards=list(forwards or []),
        shares=list(shares or []),
        created_at=time.time(),
    )
    vm = VM(spec=spec, dir=vm_dir)
    try:
        container.write_conf(vm_dir, spec)
    except RuntimeError as e:
        _cleanup_partial(vm_dir)
        return None, str(e)
    vm.save()

    # 同步 SSH 客户端配置，让外部能直接 ssh <名字>.vm.local
    _sync_ssh_config()

    log(f"创建完成：{name}")
    return vm, ""


def _cleanup_partial(vm_dir: Path) -> None:
    """创建中途失败后的目录清理

    rootfs 里已有 userns 属主（uid 1000）文件时，普通 rmtree
    会 EACCES —— ignore_errors=True 会把失败静默吞掉，留下
    残缺空壳：用户再创建/启动这台「机器」会遇到莫名其妙的
    半成品状态（曾表现为「挂载配置不生效」）。
    """
    shutil.rmtree(vm_dir, ignore_errors=True)
    if vm_dir.exists():
        container.rm_in_userns(vm_dir)


def cleanup_orphan_dirs() -> int:
    """清理残留状态（启动时调一次，返回处理掉的条目数）

    · 空壳目录 —— 目录在、没有 vm.json，容器也没在跑
    · 幽灵记录 —— SQLite 里有行，磁盘目录却已被删除（load_all 自愈）
    """
    n = 0

    try:
        from core import state
    except Exception:
        return 0

    if machines_dir().exists():
        for d in sorted(machines_dir().iterdir()):
            if not d.is_dir():
                continue
            if (d / "vm.json").exists():
                continue
            # 空壳：容器在跑的话不能删（创建流程中途除外）
            if container.is_running(machines_dir(), d.name):
                continue
            try:
                shutil.rmtree(d)
                n += 1
            except OSError:
                pass

    return n


# ─────────────────────────────────────────
# 生命周期
# ─────────────────────────────────────────
def start_vm(vm: VM, *, vnc: int | None = None,
             check_memory: bool = True) -> tuple[bool, str]:
    """启动机器

    vnc / check_memory 参数保留（UI 调用点兼容），容器档忽略。
    """
    if vm.running:
        return True, "已在运行"

    ok, err = container.prerequisites()
    if not ok:
        return False, err

    if not bridge.ready():
        return False, (
            "共享网络未启用：容器需要挂在共享网桥上。\n"
            "请到 设置 → 服务 → 虚拟机网络 先启用。"
        )

    # 旧版配置的 MAC 是占位符（lxc 随机生成），租约无法对应
    # 机器 —— 启动前统一迁移成固定 MAC（幂等）。
    if "xx:xx:xx" in container.conf_mac(vm.dir):
        try:
            container.write_conf(vm.dir, vm.spec)
        except RuntimeError:
            pass

    ok, err = container.start(machines_dir(), vm.spec.name)
    if not ok:
        return False, err

    # 首次启动确保 sshd 存在（rootfs 可能没预装 openssh）
    flag = vm.dir / ".sshd_ready"
    if not flag.exists():
        spec_img = images.get_spec(vm.spec.image_key)
        family = spec_img.family if spec_img else "debian"
        ok2, err2 = container.ensure_sshd(
            machines_dir(), vm.spec.name, vm.rootfs, family,
            username=vm.spec.username,
        )
        if ok2:
            flag.write_text("1", encoding="utf-8")
        else:
            # 机器已在跑，只是 sshd 没装上 —— 提示但不判失败
            return True, err2

    from core import state

    state.set_setting(f"vm.desired_running.{vm.name}", True)
    _sync_ssh_config()
    return True, ""


def stop_vm(vm: VM, timeout: int = 30) -> tuple[bool, str]:
    """停止机器（stop 即真休眠，状态保留在 rootfs）"""
    from core import state

    ok, err = container.stop(machines_dir(), vm.spec.name, timeout=timeout)
    if ok:
        state.set_setting(f"vm.desired_running.{vm.name}", False)
        _sync_ssh_config()
    return ok, err


def soft_stop(vm: VM, timeout: int = 30) -> tuple[bool, str]:
    """兼容入口：容器档里 stop 就是唯一形态（= 旧版的真休眠）"""
    return stop_vm(vm, timeout=timeout)


def restart_vm(vm: VM, timeout: int = 30) -> tuple[bool, str]:
    """重启机器：先停止再启动"""
    if not vm.running:
        return start_vm(vm)

    ok, err = stop_vm(vm, timeout=timeout)
    if not ok:
        return False, f"停止失败: {err}"

    ok, err = start_vm(vm)
    if not ok:
        return False, f"启动失败: {err}"

    return True, ""


def delete_vm(vm: VM, *, remove_disk: bool = True) -> tuple[bool, str]:
    """删除机器

    删除前必须确认容器真的停了：lxc 的运行时状态在 /run/lxc，
    只看目录会误判「没在跑」——目录删了进程还在，机器名仍被
    占用，再次创建会得到「已在运行」这种莫名其妙的错误。
    """
    ok, err = container.kill(machines_dir(), vm.spec.name)
    if not ok:
        return False, f"无法停止: {err}"

    # 等进程真正退出（lxc-stop -k 之后内核回收需要一点时间）
    for _ in range(10):
        if not container.is_running(machines_dir(), vm.spec.name):
            break
        time.sleep(0.3)

    if remove_disk:
        ok, err = vm.delete_files()
        if ok:
            _sync_ssh_config()
        return ok, err

    # 只删配置，保留 rootfs
    try:
        vm.config_file.unlink(missing_ok=True)
        vm.lxc_conf.unlink(missing_ok=True)
        _sync_ssh_config()
        return True, ""
    except OSError as e:
        return False, str(e)


# ─────────────────────────────────────────
# 密码
# ─────────────────────────────────────────
def get_password(name: str) -> str:
    """读取机器当前登录密码

    用户在详情页改过密码后，界面上所有展示密码的地方
    （悬停卡片、sudo 提权默认值）都必须跟着变，
    否则显示的还是默认密码，照着输必然失败。
    """
    from core import state

    try:
        val = state.get_setting(f"vm.password.{name}", "")
    except Exception:
        val = ""
    return val or DEFAULT_PASSWORD


def set_password(name: str, password: str) -> None:
    """记录机器当前登录密码（改密码成功后调用）"""
    from core import state

    try:
        state.set_setting(f"vm.password.{name}", password or "")
    except Exception:
        pass


def set_root_password(vm: VM, password: str) -> tuple[bool, str]:
    """改机器内用户密码：写 rootfs 的 shadow + 记录到 state"""
    if not password or len(password) < 4:
        return False, "密码太短（至少 4 位）"

    try:
        pwhash = _password_hash(password)
    except RuntimeError as e:
        return False, str(e)

    ok, err = container.set_root_password_hash(
        vm.rootfs, vm.spec.username, pwhash
    )
    if not ok:
        return False, err

    set_password(vm.spec.name, password)
    return True, ""


# ─────────────────────────────────────────
# 自启恢复
# ─────────────────────────────────────────
def desired_running_vms() -> list[VM]:
    """列出「用户希望其运行」的机器

    应用被强杀或系统重启后，恢复上次的运行状态。
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
    """应用启动时调用：把「希望运行但当前没跑」的机器拉起来"""
    results: list[tuple[str, bool, str]] = []

    for vm in desired_running_vms():
        if vm.running:
            continue
        ok, err = start_vm(vm)
        results.append((vm.name, ok, err))

    return results


# ─────────────────────────────────────────
# 端口记录 / 共享目录
# ─────────────────────────────────────────
def add_forward(vm: VM, host_port: int, guest_port: int,
                protocol: str = "tcp") -> tuple[bool, str]:
    """添加端口记录（容器端口直通，记录仅用于界面展示）"""
    if not port_available(host_port):
        return False, f"宿主机端口 {host_port} 已被占用"

    for f in vm.spec.forwards:
        if f.host_port == host_port:
            return False, f"端口 {host_port} 已配置"

    vm.spec.forwards.append(
        PortForward(host_port, guest_port, protocol)
    )
    vm.save()
    _sync_ssh_config()
    return True, ""


def remove_forward(vm: VM, host_port: int) -> tuple[bool, str]:
    """移除端口记录"""
    before = len(vm.spec.forwards)
    vm.spec.forwards = [
        f for f in vm.spec.forwards if f.host_port != host_port
    ]
    if len(vm.spec.forwards) == before:
        return False, "未找到该端口"
    vm.save()
    _sync_ssh_config()
    return True, ""


def add_share(vm: VM, host_path: str, tag: str) -> tuple[bool, str]:
    """添加共享目录（bind mount，重启机器生效）"""
    # 严格校验：必须是绝对路径、真实存在的目录。
    # 相对路径会被解析到进程 cwd，结果挂进去的是不存在的路径。
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
    # 配置变了，重新生成 lxc 配置
    try:
        container.write_conf(vm.dir, vm.spec)
    except RuntimeError as e:
        return False, str(e)
    return True, ""


def remove_share(vm: VM, tag: str) -> tuple[bool, str]:
    before = len(vm.spec.shares)
    vm.spec.shares = [s for s in vm.spec.shares if s[1] != tag]
    if len(vm.spec.shares) == before:
        return False, "未找到该共享"
    vm.save()
    try:
        container.write_conf(vm.dir, vm.spec)
    except RuntimeError as e:
        return False, str(e)
    return True, ""


# ─────────────────────────────────────────
# 重命名
# ─────────────────────────────────────────
def rename_vm(vm: VM, new_name: str) -> tuple[bool, str]:
    """重命名机器

    需要同步四处：机器目录名、VMSpec/vm.json、SQLite、~/.ssh/config
    """
    import re

    new_name = (new_name or "").strip()
    if not new_name:
        return False, "名称不能为空"

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", new_name):
        return False, "名称只能含字母、数字、点、下划线、短横线，且以字母或数字开头"

    if name_taken(new_name):
        return False, "名称「" + new_name + "」已被占用"

    if vm.running:
        return False, "请先停止机器"

    old_name = vm.spec.name
    old_dir = vm.dir
    new_dir = machines_dir() / new_name

    if new_dir.exists():
        return False, "目录已存在: " + str(new_dir)

    try:
        old_dir.rename(new_dir)
    except OSError as e:
        return False, f"目录改名失败: {e}"

    vm.spec.name = new_name
    vm.spec.hostname = new_name        # hostname 跟随名称
    new_vm = VM(spec=vm.spec, dir=new_dir)
    new_vm.save()
    try:
        container.write_conf(new_dir, vm.spec)
    except RuntimeError:
        pass

    try:
        from core import state

        state.delete_vm_row(old_name)
    except Exception as e:
        print(f"[vm] 清理旧 SQLite 记录失败: {e}")

    _sync_ssh_config()
    return True, ""


# ─────────────────────────────────────────
# 统计
# ─────────────────────────────────────────
def disk_usage(vm: VM) -> int:
    """机器目录实际占用（字节）"""
    total = 0
    for f in vm.dir.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def total_usage() -> int:
    """所有机器占用"""
    return sum(disk_usage(vm) for vm in load_all())


def purge_all(*, include_images: bool = True) -> tuple[int, int, str]:
    """清空所有机器及相关数据

    删除内容：所有机器（含 rootfs、配置）、可选的镜像缓存。
    机器会先被强制停止。
    """
    import shutil as _sh

    vms = load_all()
    freed = 0
    errors = []

    for vm in vms:
        if vm.running:
            container.kill(machines_dir(), vm.spec.name)
        try:
            freed += disk_usage(vm)
        except Exception:
            pass

        ok, err = delete_vm(vm, remove_disk=True)
        if not ok:
            _sh.rmtree(vm.dir, ignore_errors=True)
            if vm.dir.exists():
                errors.append(f"{vm.name}: {err}")

    # rootfs 缓存（下载的发行版 tarball，可重新下载）
    if include_images:
        try:
            for spec in images.list_images():
                p = images.rootfs_path(spec)
                if p.exists():
                    try:
                        freed += p.stat().st_size
                    except OSError:
                        pass
                    p.unlink(missing_ok=True)
        except Exception as e:
            errors.append(f"清理镜像缓存: {e}")

    _sync_ssh_config()

    return len(vms), freed, "；".join(errors)


def vm_data_size() -> tuple[int, int]:
    """机器相关数据占用

    返回 (机器总字节, 镜像缓存字节)
    """
    vm_bytes = total_usage()
    img_bytes = images.rootfs_cache_size()
    return vm_bytes, img_bytes
