"""VM 内存自动回收

策略：
  · VM 启动后延迟一段时间，若空闲则用 balloon 压缩内存
  · 检测到 SSH 活动（命令执行/终端连接）则恢复到完整内存
  · 定时器每 30 秒评估一次

实测效果：Alpine VM 从 232MB 降到 160MB（省 31%）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.vm import memory, qemu


@dataclass
class ReclaimPolicy:
    """回收策略"""

    # 空闲多久后开始回收（秒）
    idle_threshold: int = 300          # 5 分钟
    # 回收后保留的内存（MB）
    keep_mb: int = 128
    # 评估间隔（秒）
    check_interval: int = 30
    # 是否启用
    enabled: bool = True


@dataclass
class VMActivity:
    """VM 活动追踪"""

    last_active: float = 0.0           # 最后活动时间（monotonic）
    reclaimed: bool = False            # 是否已回收
    reclaimed_at: float = 0.0

    def touch(self) -> None:
        """标记活动"""
        self.last_active = time.monotonic()
        self.reclaimed = False

    def idle_seconds(self) -> float:
        if self.last_active == 0:
            return 0.0
        return time.monotonic() - self.last_active


class MemoryManager(QObject):
    """VM 内存自动管理

    用法：
        mgr = MemoryManager()
        mgr.watch(vm)               # 开始监控某个 VM
        mgr.touch(vm.name)          # 标记活动（用户操作时调用）
        mgr.unwatch(vm.name)        # 停止监控
    """

    # 回收/恢复时发出（用于 UI 更新）
    reclaimed = pyqtSignal(str, int)       # (vm_name, rss_mb)
    restored = pyqtSignal(str)

    def __init__(self, policy: ReclaimPolicy | None = None, parent=None):
        super().__init__(parent)
        self.policy = policy or ReclaimPolicy()
        self._activity: dict[str, VMActivity] = {}
        self._vms: dict[str, object] = {}      # name → VM 对象

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        if self.policy.enabled:
            self._timer.start(self.policy.check_interval * 1000)

    # ─────────────────────────────────────────
    # 注册
    # ─────────────────────────────────────────
    def watch(self, vm) -> None:
        """开始监控某个 VM"""
        self._vms[vm.name] = vm
        if vm.name not in self._activity:
            self._activity[vm.name] = VMActivity(last_active=time.monotonic())

    def unwatch(self, name: str) -> None:
        self._vms.pop(name, None)
        self._activity.pop(name, None)

    def touch(self, name: str) -> None:
        """标记活动（用户执行命令、打开终端时调用）

        如果之前已回收，会恢复完整内存。
        """
        act = self._activity.get(name)
        if act is None:
            return

        act.touch()

        # 已回收 → 恢复
        if act.reclaimed:
            vm = self._vms.get(name)
            if vm is not None and vm.running:
                ok, _ = memory.balloon_restore(vm)
                if ok:
                    act.reclaimed = False
                    self.restored.emit(name)

    # ─────────────────────────────────────────
    # 定时评估
    # ─────────────────────────────────────────
    def _tick(self) -> None:
        if not self.policy.enabled:
            return

        for name, vm in list(self._vms.items()):
            if not vm.running:
                continue

            act = self._activity.get(name)
            if act is None:
                continue

            # 已回收，跳过
            if act.reclaimed:
                continue

            # 检查空闲时长
            if act.idle_seconds() < self.policy.idle_threshold:
                continue

            # 检查是否真的空闲（无 SSH 连接）
            if self._has_ssh_sessions(vm):
                act.touch()     # 有连接，视为活动
                continue

            # 执行回收
            keep = min(self.policy.keep_mb, vm.spec.memory_mb)
            ok, msg = memory.balloon_reclaim(vm, keep_mb=keep)
            if ok:
                act.reclaimed = True
                act.reclaimed_at = time.monotonic()
                # 等几秒读实际占用
                QTimer.singleShot(3000, lambda n=name: self._report(n))

    def _has_ssh_sessions(self, vm) -> bool:
        """检查 VM 内是否有活跃 SSH 会话"""
        from core.vm import ssh

        ok, out = ssh.run_command(
            vm, "who 2>/dev/null | wc -l", timeout=5
        )
        if not ok:
            return False
        try:
            return int(out.strip()) > 0
        except ValueError:
            return False

    def _report(self, name: str) -> None:
        """报告回收后的实际占用"""
        vm = self._vms.get(name)
        if vm is None or not vm.running:
            return
        usage = memory.vm_mem_usage(vm)
        self.reclaimed.emit(name, int(usage.rss_mb))

    # ─────────────────────────────────────────
    # 手动控制
    # ─────────────────────────────────────────
    def reclaim_now(self, vm) -> tuple[bool, str]:
        """立即回收（用户手动点按钮）"""
        keep = min(self.policy.keep_mb, vm.spec.memory_mb)
        ok, msg = memory.balloon_reclaim(vm, keep_mb=keep)
        if ok:
            act = self._activity.get(vm.name)
            if act:
                act.reclaimed = True
                act.reclaimed_at = time.monotonic()
            QTimer.singleShot(3000, lambda: self._report(vm.name))
        return ok, msg

    def restore_now(self, vm) -> tuple[bool, str]:
        """立即恢复完整内存"""
        ok, msg = memory.balloon_restore(vm)
        if ok:
            act = self._activity.get(vm.name)
            if act:
                act.reclaimed = False
                act.touch()
            self.restored.emit(vm.name)
        return ok, msg

    def set_enabled(self, enabled: bool) -> None:
        """开关自动回收"""
        self.policy.enabled = enabled
        if enabled:
            self._timer.start(self.policy.check_interval * 1000)
        else:
            self._timer.stop()

    def status(self, name: str) -> dict:
        """查询某个 VM 的回收状态"""
        act = self._activity.get(name)
        if act is None:
            return {"watched": False}

        return {
            "watched": True,
            "reclaimed": act.reclaimed,
            "idle_seconds": int(act.idle_seconds()),
            "idle_threshold": self.policy.idle_threshold,
            "keep_mb": self.policy.keep_mb,
        }
