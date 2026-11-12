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

    import time as _t

    # 这里的职责只有一件事：**确保线程都结束了**，
    # 避免解释器关闭时强杀 QThread 导致 abort。
    #
    # 不在里面转事件循环：
    # 这个函数是在 app.exec() 返回之后调用的，此时没有活跃的
    # 事件循环；手动 processEvents() 会重入 Qt 内部状态，
    # 实测直接 abort。
    #
    # _cleanup 里只做 deleteLater（不需要立即执行），
    # 对象回收交给 Qt 在退出时处理，不影响进程安全退出。
    deadline = _t.monotonic() + timeout_ms / 1000
    for task in list(tasks):
        remaining = max(0, int((deadline - _t.monotonic()) * 1000))
        if not task.wait(remaining):
            # 超时未完：对象由 _ACTIVE 继续持有防 GC
            try:
                task.setParent(None)
            except RuntimeError:
                pass


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
        self._finished = False

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

        # 清理不依赖 Qt 信号 —— parent 被销毁后 self 已死，
        # 任何投递到 self 的连接都会失效，_cleanup 永远不执行。
        # 改成让 worker 在退出前直接调用（在工作线程里，纯 Python
        # 操作，不碰 Qt 对象，因此安全）。
        self._worker.finished.connect(self._mark_finished)
        self._worker.failed.connect(self._mark_finished)

        _ACTIVE.add(self)
        self._thread.start()

    def _mark_finished(self, *_args) -> None:
        """工作线程结束时调用（在工作线程里）

        只做纯 Python 的记账：把 _thread/_worker 摘掉、
        从 _ACTIVE 移除。不碰任何 Qt 对象，因此即使 self 的
        C++ 部分已被销毁也不会崩。
        """
        _ACTIVE.discard(self)
        self._finished = True

    def _alive(self) -> bool:
        """对象是否仍然有效

        parent 被销毁后，Qt 会 deleteLater 掉子对象，此时
        Python 包装层可能还活着但底层 C++ 对象已死 ——
        继续调 setToolTip / emit 等会触发 Qt fatal（abort）。
        用 sip 检查底层对象状态。
        """
        try:
            import sip

            return not sip.isdeleted(self)
        except Exception:
            return True

    def _handle_done(self, result: Any) -> None:
        if not self._alive():
            return
        if self._on_done is not None:
            try:
                self._on_done(result)
            except Exception:
                pass

    def _handle_error(self, msg: str) -> None:
        if not self._alive():
            return
        if self._on_error is not None:
            try:
                self._on_error(msg)
            except Exception:
                pass

    def _cleanup(self) -> None:
        """UI 线程侧的清理（deleteLater 内部对象）"""
        _ACTIVE.discard(self)
        self._finished = True

        worker, thread = self._worker, self._thread
        self._worker = None
        self._thread = None

        if not self._alive():
            return

        if worker is not None:
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        if thread is not None:
            try:
                thread.deleteLater()
            except RuntimeError:
                pass

    @property
    def alive(self) -> bool:
        if getattr(self, "_finished", False):
            return False
        """任务是否还在跑（对已销毁对象也安全）

        为什么需要它：
        任务结束后 `_cleanup` 会把 `_thread` 置 None 并 deleteLater
        掉内部对象。如果外部还持有 BackgroundTask 引用并去读
        `t._thread`，而该 QObject 已被 Qt 销毁，就是访问悬空指针
        → SIGSEGV（进程直接消失，没有 Python traceback）。

        用这个属性代替直接读 `_thread`，会先检查底层对象是否还在。
        """
        if not self._alive():
            return False
        return self._thread is not None

    def wait(self, ms: int = 5000) -> bool:
        """同步等待线程结束

        先 requestInterruption 再 wait。
        只 wait 不 quit 的话，工作线程 emit finished 后
        QThread 的 quit() 需要事件循环才生效 —— 而这里正卡在
        wait 里，事件循环不动，线程永远不退出（实测 3 秒超时）。
        主动 quit 让线程循环收到退出请求。
        """
        t = self._thread
        if t is None:
            return True
        try:
            if t.isRunning():
                t.quit()
        except RuntimeError:
            return True
        try:
            return t.wait(ms)
        except RuntimeError:
            return True


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
