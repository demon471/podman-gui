"""VM 启动流程与状态一致性（ui/vm_page.py + core/vm/manager.py）

守住用户实际遇到的两个 bug：
  1. 通知说「已启动」但列表还显示「启动中…」—— 两边不一致
  2. 启动误报「180s 内未启动成功」，实际已经起来了

核心不变量：
  · 通知只在**真正就绪**时发一次
  · 列表状态与通知语义一致
  · 探测用 TCP 而不是完整 SSH（快 3 个数量级）
"""

from __future__ import annotations

import time

import pytest


# ─────────────────────────────────────────
# start_vm 的端口占用保护
# ─────────────────────────────────────────
class TestStartVmPortGuard:
    """端口已被占用时应判定为「已在运行」，不重复启动"""

    def test_returns_true_when_already_running(self, monkeypatch):
        from core.vm import manager

        class FakeVM:
            running = True
            spec = type("S", (), {"name": "x", "image_key": "alpine-3.22",
                                  "arch": "amd64", "memory_mb": 256})()
            dir = None

        ok, msg = manager.start_vm(FakeVM(), check_memory=False)
        assert ok is True
        assert "已在运行" in msg

    def test_port_occupied_with_ssh_banner_counts_as_running(
        self, monkeypatch, tmp_path
    ):
        """端口被占 + 能读到 SSH banner = 有残留 QEMU，视为已运行

        否则会报 "Could not set up host forwarding rule"，
        用户看到「启动失败」但机器其实在工作。

        用一个真实监听 + 真实 SSH banner 来验证。
        """
        import socket
        import threading

        from core.vm import manager

        # 起一个假 sshd：接受连接后发 SSH banner
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        stop = threading.Event()

        def serve():
            srv.settimeout(0.3)
            while not stop.is_set():
                try:
                    c, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    c.sendall(b"SSH-2.0-FakeSSHD\r\n")
                    c.close()
                except OSError:
                    pass

        th = threading.Thread(target=serve, daemon=True)
        th.start()

        try:
            class FakeVM:
                running = False
                ssh_port = port
                dir = tmp_path
                spec = type("S", (), {
                    "name": "x", "image_key": "alpine-3.22",
                    "arch": "amd64", "memory_mb": 256,
                })()

            ok, msg = manager.start_vm(FakeVM(), check_memory=False)
            assert ok is True
            assert "已在运行" in msg
        finally:
            stop.set()
            srv.close()
            th.join(timeout=2)


# ─────────────────────────────────────────
# 探测逻辑：用 TCP 而非 SSH
# ─────────────────────────────────────────
class TestProbeSpeed:
    """探测必须是毫秒级，不能是秒级

    历史问题：用 `ssh ... echo ok` 探测，VM 没就绪时每次等满
    8 秒超时，180 秒里只能试十几次 → 误报启动失败。
    """

    def test_tcp_probe_is_fast(self):
        """对一个不存在的端口探测，必须快速失败（毫秒级）"""
        import socket

        t0 = time.time()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", 59999))   # 几乎肯定没监听
        except OSError:
            pass
        finally:
            s.close()
        elapsed = time.time() - t0
        # 本地连接被拒是即时的，远小于 SSH 的 8 秒超时
        assert elapsed < 1.0, "探测太慢，会拖长启动判定"

    def test_vm_page_uses_tcp_not_ssh(self):
        """静态检查：探测实现里不应再调用 ssh.run_command"""
        import inspect

        from ui.vm_page import VMPage

        src = inspect.getsource(VMPage._probe_ssh_then_release)
        assert "socket" in src, "探测应使用 socket"
        assert "run_command" not in src, (
            "探测不应使用完整 SSH（8 秒超时会让启动误报失败）"
        )


# ─────────────────────────────────────────
# 通知时机
# ─────────────────────────────────────────
class TestNotificationTiming:
    """通知必须与列表状态一致"""

    def test_after_start_does_not_notify(self):
        """QEMU 进程起来时不能发「已启动」通知

        此时 guest 还在启动，列表显示「启动中…」。
        提前通知会让用户以为已经能用，但实际连不上。
        """
        import inspect

        from ui.vm_page import VMPage

        src = inspect.getsource(VMPage._after_start)
        # 成功路径里不应出现「已启动」通知
        assert "虚拟机已启动" not in src, (
            "_after_start 里不该发「已启动」通知 —— "
            "那只是 qemu 进程起来，guest 还没就绪"
        )

    def test_probe_done_notifies_ready(self):
        """就绪通知必须在探测成功的回调里发"""
        import inspect

        from ui.vm_page import VMPage

        src = inspect.getsource(VMPage._probe_ssh_then_release)
        assert "虚拟机已就绪" in src, (
            "就绪通知应由探测回调发出"
        )

    def test_refresh_before_notify(self):
        """必须先刷新列表再发通知，否则通知弹出时列表还是旧状态"""
        import inspect

        from ui.vm_page import VMPage

        src = inspect.getsource(VMPage._probe_ssh_then_release)
        refresh_pos = src.find("self.refresh_silent()")
        notify_pos = src.find("notifier().info")
        assert refresh_pos > 0 and notify_pos > 0
        assert refresh_pos < notify_pos, (
            "刷新列表必须在发通知之前 —— "
            "反过来的话通知弹出时列表还显示「启动中…」"
        )


# ─────────────────────────────────────────
# 启动走 resume_vm（自动选快照/冷启动）
# ─────────────────────────────────────────
class TestStartUsesResume:
    def test_start_selected_uses_resume_vm(self):
        """点「启动」应走 resume_vm —— 有快照时 1 秒恢复，
        而不是总是冷启动（十几秒）
        """
        import inspect

        from ui.vm_page import VMPage

        src = inspect.getsource(VMPage._start_selected)
        assert "resume_vm" in src, (
            "启动应调用 resume_vm（内部自动判断快照/冷启动）"
        )
