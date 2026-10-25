"""通用后台任务

把耗时的 Podman API 调用（如 version() 900ms、system_info() 900ms）
放到后台线程执行，避免阻塞 UI 线程。

用法：
    task = BackgroundTask(fn, on_done=callback, on_error=err_callback, parent=self)
    task.start()

回调在 UI 线程执行（通过 QThread 信号机制保证）。
"""

from __future__ import annotations

from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal


class _Worker(QObject):
    """在工作线程中执行函数"""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:
            self.failed.emit(str(e))
        else:
            self.finished.emit(result)


# 全局活跃任务表（用于退出时等待，避免 QThread 被强制终止导致 abort）
_ACTIVE: set["BackgroundTask"] = set()


def shutdown_all(timeout_ms: int = 3000) -> None:
    """等待所有后台任务结束（程序退出前调用）

    Qt 在解释器关闭时若仍有 QThread 在运行，会强制终止并 abort，
    因此退出前必须等它们跑完。
    """
    shutdown_tasks(list(_ACTIVE), timeout_ms=timeout_ms)


def shutdown_tasks(tasks, timeout_ms: int = 3000) -> None:
    """等待指定的一批任务结束

    窗口/对话框关闭时调用：parent 即将销毁，
    必须保证工作线程先停，否则后续 emit 命中已销毁对象。
    """
    import time as _t

    deadline = _t.monotonic() + timeout_ms / 1000
    for task in list(tasks):
        remaining = max(0, int((deadline - _t.monotonic()) * 1000))
        if not task.wait(remaining):
            # 超时未完：让线程自然跑完（detach），
            # 但对象由 _ACTIVE 继续持有防 GC，
            # 回调 parent 已死则信号自动丢弃（Queued 安全）
            task.setParent(None)


class BackgroundTask(QObject):
    """后台任务封装

    生命周期自我管理：任务完成后自动清理线程，调用方可丢弃引用。
    """

    def __init__(
        self,
        fn: Callable[[], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._fn = fn
        self._on_done = on_done
        self._on_error = on_error
        self._thread: QThread | None = None
        self._worker: _Worker | None = None

    def start(self) -> None:
        self._thread = QThread()
        self._worker = _Worker(self._fn)
        self._worker.moveToThread(self._thread)

        # 显式 QueuedConnection：worker 在工作线程 emit，
        # 接收者（self）在创建线程。Auto 在接收者线程相同时
        # 会选 Direct，一旦 parent 已销毁就变野指针调用
        # → Qt fatal → abort（断点续传闪退的根因）。
        # Queued 保证 emit 只投递事件，接收者销毁则事件丢弃，安全。
        from PyQt6.QtCore import Qt as _Qt

        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(
            self._handle_done, _Qt.ConnectionType.QueuedConnection
        )
        self._worker.failed.connect(
            self._handle_error, _Qt.ConnectionType.QueuedConnection
        )
        # 无论成功失败都退出线程
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup)

        _ACTIVE.add(self)
        self._thread.start()

    def _handle_done(self, result: Any) -> None:
        if self._on_done is not None:
            try:
                self._on_done(result)
            except Exception:
                pass

    def _handle_error(self, msg: str) -> None:
        if self._on_error is not None:
            try:
                self._on_error(msg)
            except Exception:
                pass

    def _cleanup(self) -> None:
        _ACTIVE.discard(self)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        if self._thread is not None:
            self._thread.deleteLater()
            self._thread = None

    def wait(self, ms: int = 5000) -> bool:
        """同步等待（仅测试用）"""
        if self._thread is None:
            return True
        return self._thread.wait(ms)


def run_async(
    fn: Callable[[], Any],
    *,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    parent: QObject | None = None,
) -> BackgroundTask:
    """便捷函数：启动后台任务并返回句柄（保持引用防止 GC）"""
    task = BackgroundTask(fn, on_done=on_done, on_error=on_error, parent=parent)
    task.start()
    return task
