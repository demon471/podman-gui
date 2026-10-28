"""虚拟机常驻保活

目标：让 `ssh <名字>.vm.local` 绝大多数时候是「瞬时」的。

背景
────
云镜像 VM 的冷启动成本约 12–17 秒（UEFI 固件 + GRUB + dracut +
systemd）。这段耗时里没有「浪费」，是云镜像的正常成本，压不下去
（实测：换磁盘缓存策略、加 vCPU、精简 initramfs、改内核参数、
屏蔽 cloud-init —— 全部无影响）。

真正的解法不是「启动更快」，而是「不需要启动」：
让 VM 常驻运行，只有长时间没人用时才真正关机。

    启动 → 常驻 → 空闲 T 分钟 → 真正关机
                     ↑
              任何活动（SSH 会话、guest CPU 忙）都重置计时

于是第二次之后的 `ssh` 是秒连的，冷启动的十几秒只在很久没碰之后
才付一次。

三层保护（防止误杀）
──────────────────
1. **运行意图**：用户点「启动」才置位，点「停止」才清除。
   保活只在意图为「运行」时生效 —— 用户主动停的绝不会被自动拉起。

2. **SSH 会话检测**：guest 内有 `who` 登录会话 → 视为在用，不计空闲。
   读 `last` 的登录/登出时间，取「最后一个会话的最近活动」，
   因此长时间不敲键盘的会话也不会被判为空闲。

3. **guest CPU 检测**：读 `/proc/stat` 算 CPU 使用率，
   超过阈值说明在跑活（编译、服务），不计空闲。
   连不上 SSH（网络异常等）也按「有活动」处理 —— 宁可不关，不可误关。

内存
────
保活期间 VM 内存靠 virtio-balloon 自动回收（见 autoreclaim.py），
空闲后会压到很小，不需要为此关机。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.vm import activity as activity_mod
from core.vm.activity import Activity, MemoryPressure
from core.vm.wakeproxy import ProxyPool

log = logging.getLogger(__name__)

# 设置键（存 SQLite settings 表）
KEY_ENABLED = "vm.keepalive.enabled"
KEY_IDLE_MIN = "vm.keepalive.idle_minutes"

DEFAULT_ENABLED = True
DEFAULT_IDLE_MINUTES = 10

# 可选的空闲时长（分钟）
IDLE_CHOICES = [5, 10, 15, 30, 60, 120, 240]

# 评估间隔（秒）。60 秒足够准，且避免频繁 SSH 打扰 guest。
CHECK_INTERVAL = 20

# guest CPU 使用率超过这个百分比视为「在干活」
BUSY_CPU_PERCENT = 15.0

# 一次活动检测的 SSH 超时（秒）。探测很轻（一次往返），
# 但 guest 负载高时可能慢，给足余量避免误判成「连不上」。
PROBE_TIMEOUT = 10


# ─────────────────────────────────────────
# 配置
# ─────────────────────────────────────────
def load_config() -> tuple[bool, int]:
    """保活配置

    对用户完全透明：始终开启，空闲阈值固定。
    没有开关、没有设置项 —— 用户不需要为后台的资源调度做决定。
    """
    return DEFAULT_ENABLED, DEFAULT_IDLE_MINUTES


def save_config(enabled: bool, idle_minutes: int) -> None:
    """保留接口（旧版本调用点），当前无实际作用"""
    from core import state

    state.set_setting(KEY_ENABLED, bool(enabled))
    state.set_setting(KEY_IDLE_MIN, max(1, int(idle_minutes)))


# ─────────────────────────────────────────
# 内存压力
# ─────────────────────────────────────────
# ─────────────────────────────────────────
# 保活调度
# ─────────────────────────────────────────
@dataclass
class _Record:
    """单台 VM 的保活记录"""

    # 运行意图：用户点过「启动」且没点「停止」
    desired: bool = False
    # 本进程观察到「确实在运行」的连续空闲起点（monotonic）
    idle_since: float = 0.0
    # 最近一次评估时间
    last_check: float = 0.0
    # 最近一次探测结果（供 UI 显示）
    last_activity: Activity = field(default_factory=Activity)
    # 正在休眠处理中（避免重复触发）
    sleeping: bool = False
    # 连续观察到「完全空闲」的次数。要求连续多次才允许回收，
    # 避免探测瞬间的 TIME_WAIT / 短连接抖动造成误判。
    idle_streak: int = 0


class KeepAliveManager(QObject):
    """VM 常驻保活

    用法：
        mgr = KeepAliveManager()
        mgr.note_started(vm)        # 用户启动后调用
        mgr.note_stopped(vm)        # 用户停止后调用
        mgr.idle_left(vm.name)      # UI 查询剩余时间
    """

    # 因空闲而自动关机（UI 更新列表）
    auto_slept = pyqtSignal(str)
    # 自动关机失败
    auto_sleep_failed = pyqtSignal(str, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)

        self._recs: dict[str, _Record] = {}
        self._vms: dict[str, object] = {}
        self._task = None            # 当前后台探测任务（单飞）

        # 唤醒代理：VM 休眠期间占住 SSH 端口，
        # 外部 `ssh xxx.vm.local` 连上来时自动唤醒 VM。
        # 这是「外部终端直接 ssh」也能秒连的关键 —— GUI 拦不住
        # 用户在别的终端敲命令，只能让端口本身保持可用。
        self._pool = ProxyPool(on_wake=self._wake_for_proxy)
        # 守护进程状态缓存（避免每轮都调 systemctl）
        self._daemon_cache: bool | None = None
        self._daemon_checked: float = 0.0

        self.enabled, self.idle_minutes = load_config()

        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL * 1000)
        self._timer.timeout.connect(self._tick)
        if self.enabled:
            self._timer.start()

    # ─────────────────────────────────────────
    # 配置
    # ─────────────────────────────────────────
    def apply_config(self, enabled: bool, idle_minutes: int) -> None:
        """设置页改完调用"""
        self.enabled = bool(enabled)
        self.idle_minutes = max(1, int(idle_minutes))

        if self.enabled:
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            # 关闭保活时清空空闲起点，避免下次开启立刻超时
            for rec in self._recs.values():
                rec.idle_since = 0.0

        self._sync_proxies()

    def set_enabled(self, enabled: bool) -> None:
        self.apply_config(enabled, self.idle_minutes)

    # ─────────────────────────────────────────
    # 意图登记
    # ─────────────────────────────────────────
    def note_started(self, vm) -> None:
        """用户启动了一台 VM"""
        name = vm.spec.name
        self._vms[name] = vm
        rec = self._recs.setdefault(name, _Record())
        rec.desired = True
        rec.sleeping = False
        # 空闲计时从「真正就绪」开始，由调用方在 SSH 通后再次 note_ready
        rec.idle_since = time.monotonic()

        try:
            from core import state

            state.set_setting(f"vm.desired_running.{name}", True)
        except Exception:
            pass

        self._sync_proxies()

    def note_ready(self, vm) -> None:
        """SSH 已就绪 —— 空闲计时从现在开始"""
        name = vm.spec.name
        rec = self._recs.get(name)
        if rec is not None:
            rec.idle_since = time.monotonic()
            rec.sleeping = False

    def note_stopped(self, vm) -> None:
        """用户主动停止了 VM（清除运行意图，不再自动拉起）"""
        name = vm.spec.name
        rec = self._recs.get(name)
        if rec is not None:
            rec.desired = False
            rec.idle_since = 0.0
            rec.sleeping = False

        try:
            from core import state

            state.set_setting(f"vm.desired_running.{name}", False)
        except Exception:
            pass

        self._sync_proxies()

    def note_activity(self, name: str) -> None:
        """有用户活动（打开终端、执行命令）→ 重置空闲计时"""
        rec = self._recs.get(name)
        if rec is not None:
            rec.idle_since = time.monotonic()

    def forget(self, name: str) -> None:
        """VM 被删除"""
        self._recs.pop(name, None)
        self._vms.pop(name, None)
        self._pool.drop(name)

    def desired_running(self, name: str) -> bool:
        rec = self._recs.get(name)
        return bool(rec and rec.desired)

    # ─────────────────────────────────────────
    # 唤醒代理
    # ─────────────────────────────────────────
    def _wake_for_proxy(self, vm) -> bool:
        """唤醒代理回调：把 VM 拉起来（阻塞直到 SSH 就绪）

        在代理的工作线程里调用，不能碰 Qt 对象。
        """
        from core.vm import manager, ssh

        name = vm.spec.name
        log.info("唤醒代理请求启动 %s", name)

        # 优先从内存快照恢复（约 1s），没有快照才冷启动（约 19s）
        ok, err = manager.resume_vm(vm)
        if not ok:
            log.warning("唤醒代理启动 %s 失败: %s", name, err)
            return False

        # 等 SSH 真的能连上（QEMU 进程起来 ≠ 能登录）
        fresh = manager.load(name)
        if fresh is None:
            return False

        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                ok2, _ = ssh.test_connection(fresh, timeout=5)
                if ok2:
                    return True
            except Exception:
                pass
            time.sleep(2)
        return False

    def _daemon_running(self) -> bool:
        """守护进程是否在运行

        守护进程（systemd 用户服务）负责长期占住端口。
        它在跑时 GUI 不该再抢同一批端口 —— 会造成 bind 失败
        并让界面显示异常的代理状态。
        """
        if self._daemon_cache is not None:
            if time.monotonic() - self._daemon_checked < 30:
                return self._daemon_cache

        active = False
        try:
            from core.vm import daemon as daemon_mod

            active = bool(daemon_mod.service_status().get("active"))
        except Exception:
            active = False

        self._daemon_cache = active
        self._daemon_checked = time.monotonic()
        return active

    def _sync_proxies(self) -> None:
        """按 VM 当前状态增删唤醒代理

        · 意图运行 + 进程没跑 → 起代理（占端口，等外部连接）
        · 进程在跑            → 停代理（端口归 QEMU）
        · 意图不要运行        → 停代理

        守护进程在跑时完全不碰代理 —— 端口归它管。
        """
        if not self.enabled or self._daemon_running():
            self._pool.stop_all()
            return

        for name, rec in list(self._recs.items()):
            vm = self._vms.get(name)
            if vm is None:
                self._pool.drop(name)
                continue

            should_proxy = rec.desired and not vm.running
            if should_proxy:
                # 只尝试一次，避免在 UI 线程里 sleep 重试。
                # 失败（多半是 QEMU 刚退出、端口还在 TIME_WAIT）
                # 交给 _schedule_proxy_retry 延迟补上。
                ok, err = self._pool.ensure(vm, retries=1)
                if not ok:
                    log.debug("代理启动失败 %s: %s，稍后重试", name, err)
                    self._schedule_proxy_retry(name)
            else:
                self._pool.drop(name)

    def _schedule_proxy_retry(self, name: str, attempt: int = 1) -> None:
        """延迟重试建立唤醒代理（端口可能还在 TIME_WAIT）"""
        if attempt > 20:
            log.warning("唤醒代理 %s 多次重试仍失败，放弃", name)
            return

        def retry():
            rec = self._recs.get(name)
            vm = self._vms.get(name)
            if rec is None or vm is None:
                return
            if not (self.enabled and rec.desired and not vm.running):
                return
            if self._pool.ensure(vm, retries=1)[0]:
                log.debug("唤醒代理 %s 已补建（第 %d 次重试）", name, attempt)
                return
            self._schedule_proxy_retry(name, attempt + 1)

        QTimer.singleShot(1000, retry)

    def proxied_vms(self) -> list[str]:
        """当前由代理占着端口的 VM（休眠中，可被自动唤醒）"""
        return self._pool.active_names()

    # ─────────────────────────────────────────
    # 按需唤醒
    # ─────────────────────────────────────────
    def wants_running(self, name: str) -> bool:
        """这台 VM 是否处于「应当运行」状态（含休眠中）"""
        rec = self._recs.get(name)
        return bool(rec and rec.desired)

    def needs_wake(self, name: str) -> bool:
        """休眠中需要唤醒（意图运行 + 进程没跑）"""
        if not self.wants_running(name):
            return False
        vm = self._vms.get(name)
        return vm is not None and not vm.running

    # ─────────────────────────────────────────
    # 状态查询（UI 用）
    # ─────────────────────────────────────────
    def idle_left(self, name: str) -> int | None:
        """距自动关机还剩多少秒（None = 不适用/未运行）"""
        if not self.enabled:
            return None
        rec = self._recs.get(name)
        if rec is None or not rec.desired or rec.sleeping:
            return None

        vm = self._vms.get(name)
        if vm is None or not vm.running:
            return None

        if rec.idle_since <= 0:
            return None

        elapsed = time.monotonic() - rec.idle_since
        left = self.idle_minutes * 60 - elapsed
        return max(0, int(left))

    def is_sleeping(self, name: str) -> bool:
        rec = self._recs.get(name)
        return bool(rec and rec.sleeping)

    def status(self, name: str) -> dict:
        """完整状态（调试/详情页用）"""
        rec = self._recs.get(name)
        if rec is None:
            return {"tracked": False}

        return {
            "tracked": True,
            "desired": rec.desired,
            "sleeping": rec.sleeping,
            "idle_left": self.idle_left(name),
            "sessions": rec.last_activity.sessions,
            "cpu_percent": round(rec.last_activity.cpu_percent, 1),
            "reachable": rec.last_activity.reachable,
        }

    # ─────────────────────────────────────────
    # 定时评估
    # ─────────────────────────────────────────
    def _tick(self) -> None:
        """周期性评估：是否需要为缓解内存压力而回收空闲 VM

        触发条件（全部满足才动手）：
          1. 宿主机内存确实紧张
          2. 该 VM 登记了「要运行」（用户启动过且没停止）
          3. 该 VM 进程确实在跑
          4. 该 VM 完全没有任何访问活动

        内存不紧张时什么都不做 —— VM 保持全速运行，
        用户不会因为后台策略而感到「卡」。
        """
        if not self.enabled:
            return

        # 后台探测任务还在跑（单飞）
        if self._task is not None:
            return

        # ── 内存压力评估 ──
        pressure = self._memory_pressure()
        if not pressure.should_reclaim:
            return

        # ── 挑候选 ──
        candidates = []
        for name, rec in list(self._recs.items()):
            if not rec.desired or rec.sleeping:
                continue

            vm = self._vms.get(name)
            if vm is None or not vm.running:
                rec.idle_since = 0.0
                continue

            candidates.append((name, vm))

        if not candidates:
            return

        # 探测放后台（SSH + monitor 查询会阻塞）
        from core.async_task import run_async

        def work():
            out = {}
            for name, vm in candidates:
                try:
                    out[name] = activity_mod.probe(vm)
                except Exception as e:
                    log.debug("活动探测失败 %s: %s", name, e)
                    # 探测失败 → 保守视为有活动
                    act = Activity(reachable=False)
                    act.host_conns = 1
                    out[name] = act
            return out

        def done(results):
            self._task = None
            self._apply(results, pressure)

        def error(msg):
            self._task = None
            log.debug("保活探测任务失败: %s", msg)

        self._task = run_async(work, on_done=done, on_error=error, parent=self)

    def _memory_pressure(self) -> MemoryPressure:
        """评估宿主机内存压力（委托给纯 Python 实现）"""
        return activity_mod.memory_pressure()

    def _apply(self, results: dict, pressure: "MemoryPressure") -> None:
        """根据活动探测结果决定回收哪些 VM"""
        now = time.monotonic()

        # critical：回收所有完全空闲的（不看空闲时长）
        # tight：只回收已经空闲超过阈值的
        idle_threshold = 0 if pressure.level == "critical" else self.idle_minutes * 60

        # 按「空闲最久」排序，内存紧张时优先回收最没用的
        idle_list: list[tuple[float, str, Activity]] = []

        for name, act in results.items():
            rec = self._recs.get(name)
            if rec is None:
                continue

            rec.last_check = now
            rec.last_activity = act

            # 有任何活动 → 重置计时，绝不回收
            if not act.reachable or act.busy:
                rec.idle_since = now
                rec.idle_streak = 0
                continue

            # 完全空闲 → 累积连续次数
            rec.idle_streak += 1

            # 要求连续 2 次探测都空闲才认为「真的没人用」。
            # 单次空闲可能是探测间隙，连续两次才可靠。
            if rec.idle_streak < 2:
                if rec.idle_since <= 0:
                    rec.idle_since = now
                continue

            # 记录空闲起点
            if rec.idle_since <= 0:
                rec.idle_since = now
                continue

            idle_for = now - rec.idle_since
            if idle_for < idle_threshold:
                continue

            idle_list.append((idle_for, name, act))

        if not idle_list:
            return

        # 空闲越久越先回收
        idle_list.sort(reverse=True)

        log.info(
            "内存%s（可用 %dMB / %dMB, %.0f%%），回收 %d 台空闲虚拟机",
            "紧张" if pressure.level == "tight" else "告急",
            pressure.available_mb, pressure.total_mb,
            pressure.available_pct, len(idle_list),
        )

        for idle_for, name, act in idle_list:
            log.info("  回收 %s（已空闲 %d 分钟，%s）",
                     name, int(idle_for // 60), act.describe())
            self._auto_sleep(name)

    def _auto_sleep(self, name: str) -> None:
        """空闲超时：真正停止 VM（运行意图保留，下次自动恢复）"""
        rec = self._recs.get(name)
        vm = self._vms.get(name)
        if rec is None or vm is None or rec.sleeping:
            return

        rec.sleeping = True
        log.info("VM %s 空闲超时，自动休眠", name)

        from core.async_task import run_async

        def work():
            from core.vm import manager

            return manager.soft_stop(vm)

        def done(result):
            ok = bool(result and result[0])
            if ok:
                # 运行意图保留 —— 用户下次访问时自动拉起
                self.auto_slept.emit(name)
            else:
                err = (result[1] if result else "") or "未知错误"
                log.warning("VM %s 自动休眠失败: %s", name, err)
                self.auto_sleep_failed.emit(name, err)

            r = self._recs.get(name)
            if r is not None:
                r.sleeping = False
                r.idle_since = 0.0

            # VM 已休眠 → 起代理占住端口，外部 ssh 才能连上并触发唤醒
            if ok:
                self._sync_proxies()

        def error(msg):
            log.warning("VM %s 自动休眠异常: %s", name, msg)
            self.auto_sleep_failed.emit(name, msg)
            r = self._recs.get(name)
            if r is not None:
                r.sleeping = False

        run_async(work, on_done=done, on_error=error, parent=self)

    # ─────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────
    def stop_timer(self) -> None:
        self._timer.stop()

    def shutdown(self) -> None:
        """应用退出：停掉定时器和唤醒代理（VM 本身保持运行）

        代理随进程退出而释放端口。下次应用启动时，_sync_proxies
        会为仍在休眠的 VM 重新建立代理。
        """
        self.stop_timer()
        self._pool.stop_all()
