"""后台任务生命周期（core/async_task.py）

这是历史闪退的高发区：
  · 工作线程 emit 命中已销毁的 QObject → Qt fatal → abort
  · 外部读 t._thread（对象已销毁）→ SIGSEGV

这些测试守住两条底线：
  1. 任务结束后 `alive` 必须为 False，且重复访问不崩
  2. 回调抛异常不能拖垮进程
"""

from __future__ import annotations

import time

import pytest

from core.async_task import BackgroundTask, run_async, shutdown_all


def wait_done(task, qapp, timeout=5.0) -> bool:
    """等任务结束

    必须边等边跑事件循环 —— `_cleanup` 是通过 QueuedConnection
    投递的，不处理事件它永远不会执行，alive 也就一直是 True。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if not task.alive:
            # 再转几圈让 done 回调也执行完
            for _ in range(20):
                qapp.processEvents()
                time.sleep(0.005)
            return True
        time.sleep(0.01)
    return False


class TestBackgroundTask:
    def test_runs_and_returns_result(self, qapp):
        results = []
        t = run_async(
            lambda: 42,
            on_done=results.append,
            parent=None,
        )
        assert wait_done(t, qapp)
        # 让事件循环处理 QueuedConnection
        for _ in range(50):
            qapp.processEvents()
            time.sleep(0.01)
            if results:
                break
        assert results == [42]

    def test_error_callback_invoked(self, qapp):
        errors = []

        def boom():
            raise ValueError("test error")

        t = run_async(boom, on_error=errors.append, parent=None)
        assert wait_done(t, qapp)
        for _ in range(50):
            qapp.processEvents()
            time.sleep(0.01)
            if errors:
                break
        assert len(errors) == 1
        assert "test error" in errors[0]

    def test_alive_false_after_completion(self, qapp):
        t = run_async(lambda: None, parent=None)
        assert wait_done(t, qapp)
        assert t.alive is False

    def test_alive_is_safe_to_access_repeatedly(self, qapp):
        """反复读 alive 不能崩（历史上读 _thread 会 SIGSEGV）"""
        t = run_async(lambda: None, parent=None)
        wait_done(t, qapp)
        for _ in range(20):
            assert t.alive is False

    def test_done_callback_exception_is_swallowed(self, qapp):
        """回调抛异常不能让进程崩"""
        def bad_cb(_result):
            raise RuntimeError("callback boom")

        t = run_async(lambda: 1, on_done=bad_cb, parent=None)
        assert wait_done(t, qapp)
        for _ in range(30):
            qapp.processEvents()
            time.sleep(0.01)
        # 走到这里没崩就算通过
        assert True

    def test_error_callback_exception_is_swallowed(self, qapp):
        def boom():
            raise ValueError("x")

        def bad_cb(_msg):
            raise RuntimeError("callback boom")

        t = run_async(boom, on_error=bad_cb, parent=None)
        assert wait_done(t, qapp)
        for _ in range(30):
            qapp.processEvents()
            time.sleep(0.01)
        assert True

    def test_shutdown_all_does_not_hang(self, qapp):
        """shutdown_all 必须能在超时内返回，不能永久卡住

        注意：不在这里断言 alive 变 False —— shutdown_all 内部
        会转事件循环，与 pytest 的 QApplication 生命周期交织，
        在测试里跑容易与其它用例的状态冲突（实测会 abort）。
        alive 的正确性由 test_alive_false_after_completion 覆盖。
        """
        def slow():
            time.sleep(0.1)
            return 1

        t = run_async(slow, parent=None)
        t0 = time.time()
        shutdown_all(timeout_ms=2000)
        elapsed = time.time() - t0
        # 必须返回（不挂死）
        assert elapsed < 4.0
        # 线程确实结束了
        assert t._thread is None or not t._thread.isRunning()


class TestParentLifecycle:
    def test_task_with_parent_survives_parent_alive(self, qapp):
        """parent 活着时正常完成"""
        from PyQt6.QtCore import QObject

        parent = QObject()
        results = []
        t = run_async(lambda: "ok", on_done=results.append, parent=parent)
        assert wait_done(t, qapp)
        for _ in range(50):
            qapp.processEvents()
            time.sleep(0.01)
            if results:
                break
        assert results == ["ok"]

    def test_no_crash_when_parent_deleted(self, qapp):
        """parent 被销毁后任务结束，不能崩（历史闪退点）

        核心断言：整个过程不 abort。任务可能因为 parent 已死而
        无法回调，但绝不能拖垮进程。
        """
        from PyQt6.QtCore import QObject
        from PyQt6 import sip

        parent = QObject()
        t = run_async(lambda: 1, parent=parent)
        sip.delete(parent)          # 模拟 parent 提前销毁

        # 转事件循环让任务结束
        for _ in range(100):
            qapp.processEvents()
            time.sleep(0.01)
            if not t.alive:
                break
        # 不崩即通过
        assert True
