"""虚拟机守护进程

独立于 GUI 的后台服务。存在的唯一理由：**GUI 会死，守护不能死。**

为什么必须独立
──────────────
用户可能以任何方式关闭 GUI：

    · 点窗口关闭按钮
    · 托盘「退出」
    · kill / kill -9
    · 桌面会话崩溃、注销、重启
    · 系统 OOM killer

如果「占住端口、按需唤醒、内存回收」这些逻辑跑在 GUI 进程里，
上面任何一种情况都会让所有虚拟机失去管理 —— 表现为
`ssh xxx.vm.local` 直接「连接被拒绝」，用户以为数据全丢了。

所以把这些职责放到一个由 systemd 托管的独立进程里：

    systemd --user
      └── podman-gui-vmd.service      ← 本模块
            ├── 唤醒代理（占住所有 VM 的转发端口）
            ├── 内存压力监控（紧张时休眠空闲 VM）
            └── 自愈（QEMU 意外退出后重建代理/恢复状态）

GUI 只负责展示和发指令（通过 state 数据库 + 控制 socket），
进程本身随时可以被杀掉，不影响虚拟机可用性。

运行方式
────────
    python -m core.vm.daemon            # 前台（调试）
    systemctl --user start podman-gui-vmd   # 由 systemd 托管

职责边界
────────
守护进程 **不** 启动用户没要求运行的 VM。
「哪些 VM 应该在运行」由 state 里的 vm.desired_running.<name> 决定，
这个标记只有用户点「启动」时才会置位。

自愈规则
────────
每轮巡检（默认 15 秒）：

  1. desired=True 且 QEMU 在跑   → 撤掉代理（端口归 QEMU）
  2. desired=True 且 QEMU 没跑   → 建代理（等外部连接唤醒）
  3. desired=False               → 撤掉代理，不碰 VM
  4. 端口被外来进程占用           → 记录并跳过，不强行抢
  5. 快照文件损坏                 → 丢弃，允许冷启动兜底
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

# 允许 `python -m core.vm.daemon` 与直接执行两种方式
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import state
from core.vm import activity as activity_mod
from core.vm import manager, qemu
from core.vm.activity import MemoryPressure
from core.vm.wakeproxy import ProxyPool

log = logging.getLogger("vmd")

# 巡检间隔（秒）。15 秒足够及时，且开销可忽略
TICK_INTERVAL = 15

# 连续观察到「完全空闲」多少轮才允许休眠。
# 2 轮 = 30 秒，避免探测抖动造成误判。
IDLE_STREAK_REQUIRED = 2

# 空闲多久（秒）才允许在内存紧张时休眠
IDLE_MINUTES = 10

# 快照保存/恢复的并发保护（同一时刻只处理一台，避免磁盘打满）
BUSY_FLAG = "vm.daemon.busy"


# ─────────────────────────────────────────
# 守护进程
# ─────────────────────────────────────────
class Daemon:
    def __init__(self) -> None:
        self._pool = ProxyPool(on_wake=self._wake_vm)
        self._stop = False
        # name -> 连续空闲轮数
        self._idle_streak: dict[str, int] = {}
        # name -> 空闲起点（monotonic）
        self._idle_since: dict[str, float] = {}
        # 正在恢复/休眠的 VM（避免重入）
        self._working: set[str] = set()

    # ─────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────
    def run(self) -> int:
        log.info("虚拟机守护进程启动 (pid=%d)", os.getpid())

        # systemd 停止时发 SIGTERM
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

        # 首次巡检立即执行（应用刚被杀，端口可能空着）
        self._tick()

        while not self._stop:
            # 用可中断的 sleep 代替 time.sleep，保证信号能及时响应
            for _ in range(TICK_INTERVAL * 10):
                if self._stop:
                    break
                time.sleep(0.1)
            if self._stop:
                break
            try:
                self._tick()
            except Exception as e:
                # 任何异常都不能让守护进程退出 —— 它是最后的保障
                log.exception("巡检异常: %s", e)

        self._shutdown()
        return 0

    def _on_signal(self, signum, frame) -> None:
        log.info("收到信号 %d，准备退出", signum)
        self._stop = True

    def _shutdown(self) -> None:
        """退出清理：释放端口（VM 本身保持运行）"""
        log.info("守护进程退出，释放代理端口")
        self._pool.stop_all()

    # ─────────────────────────────────────────
    # 巡检
    # ─────────────────────────────────────────
    def _tick(self) -> None:
        vms = manager.load_all()
        if not vms:
            self._pool.stop_all()
            self._idle_streak.clear()
            self._idle_since.clear()
            return

        desired: dict[str, object] = {}
        for vm in vms:
            name = vm.spec.name
            want = state.get_setting(f"vm.desired_running.{name}", False)
            if want:
                desired[name] = vm

        # 1) 清理已删除/已停止的 VM 的代理与计时
        for name in list(self._idle_streak):
            if name not in desired:
                self._idle_streak.pop(name, None)
                self._idle_since.pop(name, None)
                self._pool.drop(name)

        if not desired:
            self._pool.stop_all()
            return

        # 2) 按 VM 实际状态维护代理
        for name, vm in desired.items():
            if name in self._working:
                continue

            running = vm.running
            proxied = name in self._pool.active_names()

            if running and proxied:
                # QEMU 起来了但代理还占着端口（理论上 QEMU 抢不到端口，
                # 说明代理是这轮之前建的）→ 撤掉代理
                self._pool.drop(name)
            elif running and not proxied:
                # 正常运行，无需代理
                pass
            elif not running and not proxied:
                # 休眠 / 未运行 / QEMU 被外部杀掉
                #
                # 统一处理：建代理占住端口，等外部访问时再唤醒。
                # 这样即使 QEMU 被 kill -9，用户下次 ssh 依然能用 ——
                # 不需要守护进程主动去「复活」它（那会让用户
                # 主动 kill 掉虚拟机后又被莫名拉起）。
                ok, err = self._pool.ensure(vm, retries=1)
                if not ok:
                    log.debug("代理建立失败 %s: %s（下轮重试）", name, err)
            else:
                # 未运行但有代理 → 正常（等连接）
                pass

        # 3) 内存压力 + 空闲回收
        self._maybe_reclaim(desired)

    # ─────────────────────────────────────────
    # 内存回收
    # ─────────────────────────────────────────
    def _maybe_reclaim(self, desired: dict) -> None:
        pressure = activity_mod.memory_pressure()
        if not pressure.should_reclaim:
            # 内存充裕：清空空闲计时，不做任何回收
            self._idle_streak.clear()
            self._idle_since.clear()
            return

        candidates = [
            vm for name, vm in desired.items()
            if vm.running and name not in self._working
        ]
        if not candidates:
            return

        now = time.monotonic()
        to_sleep: list[tuple[float, str, object]] = []

        for vm in candidates:
            name = vm.spec.name
            try:
                act = activity_mod.probe(vm)
            except Exception as e:
                log.debug("探测失败 %s: %s", name, e)
                self._reset_idle(name)
                continue

            # 任何活动 → 重置
            if not act.reachable or act.busy:
                self._reset_idle(name)
                continue

            streak = self._idle_streak.get(name, 0) + 1
            self._idle_streak[name] = streak

            if streak < IDLE_STREAK_REQUIRED:
                self._idle_since.setdefault(name, now)
                continue

            since = self._idle_since.setdefault(name, now)
            idle_for = now - since

            threshold = 0 if pressure.level == "critical" else IDLE_MINUTES * 60
            if idle_for < threshold:
                continue

            to_sleep.append((idle_for, name, vm))

        if not to_sleep:
            return

        # 空闲最久的先回收
        to_sleep.sort(reverse=True, key=lambda x: x[0])

        log.info(
            "内存%s（可用 %dMB/%dMB, %.0f%%），回收 %d 台空闲虚拟机",
            "告急" if pressure.level == "critical" else "紧张",
            pressure.available_mb, pressure.total_mb, pressure.available_pct,
            len(to_sleep),
        )

        for idle_for, name, vm in to_sleep:
            self._sleep_vm(vm, idle_for)

    def _reset_idle(self, name: str) -> None:
        self._idle_streak.pop(name, None)
        self._idle_since.pop(name, None)

    def _sleep_vm(self, vm, idle_for: float) -> None:
        """把 VM 转成内存快照（同步执行，守护进程里可接受）"""
        name = vm.spec.name
        if name in self._working:
            return

        self._working.add(name)
        log.info("  休眠 %s（已空闲 %d 分钟）", name, int(idle_for // 60))
        try:
            ok, msg = manager.soft_stop(vm)
            if ok:
                log.info("  %s 已休眠", name)
            else:
                log.warning("  %s 休眠失败: %s", name, msg)
        except Exception as e:
            log.exception("  %s 休眠异常: %s", name, e)
        finally:
            self._working.discard(name)
            self._reset_idle(name)
            # 休眠后立刻建代理，保证外部 ssh 仍能唤醒
            try:
                self._pool.ensure(vm, retries=6)
            except Exception:
                pass

    # ─────────────────────────────────────────
    # 唤醒（代理回调，在工作线程里）
    # ─────────────────────────────────────────
    def _wake_vm(self, vm) -> bool:
        """外部连接进来 → 恢复 VM（优先内存快照，约 1–2 秒）"""
        name = vm.spec.name
        log.info("收到访问，恢复 %s", name)

        self._working.add(name)
        try:
            fresh = manager.load(name)
            if fresh is None:
                return False

            ok, err = manager.resume_vm(fresh)
            if not ok:
                log.warning("恢复 %s 失败: %s", name, err)
                return False

            # 等 SSH 真正可用
            from core.vm import ssh

            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    ok2, _ = ssh.test_connection(fresh, timeout=5)
                    if ok2:
                        log.info("%s 已恢复", name)
                        return True
                except Exception:
                    pass
                time.sleep(1)
            return False
        finally:
            self._working.discard(name)
            self._reset_idle(name)


# ─────────────────────────────────────────
# systemd 用户服务安装
# ─────────────────────────────────────────
UNIT_NAME = "podman-gui-vmd.service"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

_UNIT_TEMPLATE = """[Unit]
Description=Podman GUI 虚拟机守护进程
Documentation=https://github.com/demon471/podman-gui
# 桌面会话起来后才需要（VM 依赖用户级端口与 SSH 配置）
After=default.target

[Service]
Type=simple
{work_dir}ExecStart={exec_start}
Restart=always
RestartSec=3
# 关键：只杀守护进程本身，绝不碰 QEMU 子进程。
# KillMode=mixed 会在服务停止/重启时把整个 cgroup 里的进程
# （包括 QEMU）一起 SIGKILL —— 那会让正在运行的虚拟机被
# 无辜杀死。QEMU 用 -daemonize 独立运行，本来就该脱离服务
# 生命周期，由守护进程按 desired_running 管理。
KillMode=process
TimeoutStopSec=10
# 允许守护进程 fork 出的 QEMU 存活
SendSIGKILL=no

[Install]
WantedBy=default.target
"""


def _daemon_exec_start() -> tuple[str, str]:
    """生成 (ExecStart, WorkingDirectory)

    打包成单文件时，用 GUI 二进制加 --daemon 参数（复用同一份代码）；
    源码运行时用 python -m core.vm.daemon，并设置工作目录 ——
    否则 systemd 从 HOME 启动时找不到 core 包。
    """
    exe = Path(sys.executable).resolve()

    # PyInstaller 单文件：sys.frozen 为真，sys.executable 就是那个二进制
    if getattr(sys, "frozen", False):
        return f"{exe} --daemon", ""

    # 源码运行：必须指定工作目录，让 -m 能找到 core 包
    root = Path(__file__).resolve().parents[2]
    return f"{exe} -m core.vm.daemon", f"WorkingDirectory={root}\n"


def install_service() -> tuple[bool, str]:
    """安装并启用守护服务（用户级，不需要 sudo）"""
    import subprocess

    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    unit_path = UNIT_DIR / UNIT_NAME

    try:
        exec_start, work_dir = _daemon_exec_start()
        unit_path.write_text(
            _UNIT_TEMPLATE.format(exec_start=exec_start, work_dir=work_dir),
            encoding="utf-8",
        )
    except OSError as e:
        return False, f"写入 unit 失败: {e}"

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=15,
        )
        r = subprocess.run(
            ["systemctl", "--user", "enable", "--now", UNIT_NAME],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            return True, "守护服务已安装并启动"
        return False, (r.stderr or r.stdout).strip()[:300]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def uninstall_service() -> tuple[bool, str]:
    """停用并移除守护服务"""
    import subprocess

    try:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", UNIT_NAME],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        (UNIT_DIR / UNIT_NAME).unlink(missing_ok=True)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, text=True, timeout=15,
        )
        return True, "守护服务已移除"
    except OSError as e:
        return False, str(e)


def service_status() -> dict:
    """查询守护服务状态"""
    import subprocess

    info = {
        "installed": (UNIT_DIR / UNIT_NAME).exists(),
        "active": False,
        "pid": 0,
    }
    if not info["installed"]:
        return info

    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", UNIT_NAME],
            capture_output=True, text=True, timeout=10,
        )
        info["active"] = (r.stdout or "").strip() == "active"

        r = subprocess.run(
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT_NAME],
            capture_output=True, text=True, timeout=10,
        )
        try:
            info["pid"] = int((r.stdout or "0").strip())
        except ValueError:
            info["pid"] = 0
    except (subprocess.TimeoutExpired, OSError):
        pass

    return info


def notify_gui() -> None:
    """通知 GUI 状态已变化（守护进程重启后让界面刷新代理显示）"""
    # 目前 GUI 靠轮询，这里留作扩展点
    return


# ─────────────────────────────────────────
# 入口
# ─────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Podman GUI 虚拟机守护进程")
    parser.add_argument(
        "--root", default=None,
        help="项目根目录（源码运行时需要）",
    )
    parser.add_argument(
        "--install", action="store_true", help="安装 systemd 用户服务",
    )
    parser.add_argument(
        "--uninstall", action="store_true", help="卸载 systemd 用户服务",
    )
    parser.add_argument(
        "--status", action="store_true", help="查看服务状态",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="详细日志",
    )
    args = parser.parse_args(argv)

    if args.install:
        ok, msg = install_service()
        print(msg)
        return 0 if ok else 1

    if args.uninstall:
        ok, msg = uninstall_service()
        print(msg)
        return 0 if ok else 1

    if args.status:
        import json

        print(json.dumps(service_status(), ensure_ascii=False, indent=2))
        return 0

    if args.root:
        sys.path.insert(0, args.root)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    return Daemon().run()


if __name__ == "__main__":
    sys.exit(main())
