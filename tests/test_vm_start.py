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
                """模拟一台「端口转发到本机」的 VM（SLIRP 模式）

                假 sshd 就起在 127.0.0.1:port，所以 ssh_host 是回环地址；
                网桥模式下这里会是 guest 自己的 IP。
                """
                running = False
                ssh_port = port
                ssh_host = "127.0.0.1"
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


class TestSnapshotResume:
    """快照恢复后必须发 cont

    踩过的致命 bug：-incoming 灌完迁移流后 QEMU 停在
    `paused (postmigrate)`，不发 cont 的话：
      · 进程在跑、端口在监听（TCP 能连上）
      · 但 guest 完全冻结，SSH 永远等不到 banner
    表现就是「虚拟机连不上」，软件内外都一样。
    """

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "core" / "vm" / "qemu.py").read_text(
            encoding="utf-8"
        )

    def test_has_resume_helper(self):
        src = self._src()
        assert "def resume_if_paused" in src
        assert "def vm_status" in src

    def test_restore_sends_cont(self):
        src = self._src()
        start = src.find("def snapshot_restore")
        end = src.find("def _cleanup_share_daemons", start)
        body = src[start:end]
        assert "resume_if_paused" in body, "恢复后必须 cont"
        assert body.find("resume_if_paused") < body.find("drop_snapshot")

    def test_resume_detects_postmigrate(self):
        src = self._src()
        start = src.find("def resume_if_paused")
        body = src[start:start + 1200]
        assert "paused" in body
        assert '"cont"' in body or "'cont'" in body

    def test_running_state_not_disturbed(self):
        """已经在 running 的不要重复发 cont"""
        src = self._src()
        start = src.find("def resume_if_paused")
        body = src[start:start + 1200]
        assert "running" in body


class TestPausedWatchdog:
    """周期性把 paused 的 VM 拉回运行

    快照恢复漏发 cont 时 guest 是冻结的：端口在监听、TCP 能连，
    但 SSH 永远等不到 banner。snapshot_restore 里已经发过 cont，
    这里再兜一层，覆盖旧二进制守护进程 / 手动 kill 等情况。
    """

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        root = Path(__file__).parent.parent
        return (root / "core" / "vm" / "manager.py").read_text(encoding="utf-8")

    def test_helper_exists(self):
        from core.vm import manager

        assert callable(manager.resume_paused_vms)

    def test_skips_running_vms(self, monkeypatch):
        """已经在跑的不能被碰"""
        from core.vm import manager, qemu

        called = []

        class FakeVM:
            spec = type("S", (), {"name": "x"})()
            running = True
            dir = manager.DEFAULT_VM_ROOT / "machines" / "x"

        monkeypatch.setattr(manager, "load_all", lambda: [FakeVM()])
        monkeypatch.setattr(qemu, "vm_status", lambda d: "running")
        monkeypatch.setattr(
            qemu, "resume_if_paused",
            lambda d, timeout=3.0: called.append(d) or True,
        )
        assert manager.resume_paused_vms() == []
        assert called == []

    def test_resumes_paused_vms(self, monkeypatch):
        from core.vm import manager, qemu

        class FakeVM:
            spec = type("S", (), {"name": "x"})()
            running = True
            dir = manager.DEFAULT_VM_ROOT / "machines" / "x"

        monkeypatch.setattr(manager, "load_all", lambda: [FakeVM()])
        monkeypatch.setattr(qemu, "vm_status", lambda d: "paused (postmigrate)")
        monkeypatch.setattr(
            qemu, "resume_if_paused", lambda d, timeout=3.0: True
        )
        assert manager.resume_paused_vms() == ["x"]

    def test_daemon_calls_watchdog(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "core" / "vm" / "daemon.py").read_text(
            encoding="utf-8"
        )
        start = src.find("def _tick")
        body = src[start:start + 800]
        assert "resume_paused_vms" in body


class TestExternalSshInfo:
    """账户页要给出第三方 SSH 客户端需要的全部字段"""

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        return (
            Path(__file__).parent.parent / "ui" / "vm_detail.py"
        ).read_text(encoding="utf-8")

    def test_shows_connection_fields(self):
        src = self._src()
        for field in ('"主机"', '"端口"', '"用户"', '"密码"'):
            assert field in src, f"缺少字段 {field}"

    def test_two_columns(self):
        """一行两列（grid），不再一列到底"""
        src = self._src()
        assert "QGridLayout()" in src
        import re

        # grid.addWidget(... , row, col) 可能被 black 折成多行，
        # 用正则匹配而不是找固定子串，避免格式化一变就误报
        m = re.search(
            r'cell_widget\("主机".*?\),\s*(\d+),\s*(\d+)\s*\)', src, re.S
        )
        assert m is not None, "找不到主机单元格"
        assert (m.group(1), m.group(2)) == ("0", "0"), "主机应在第 0 行第 0 列"

        m2 = re.search(
            r'cell_widget\("端口".*?\),\s*(\d+),\s*(\d+)\s*\)', src, re.S
        )
        assert m2 is not None, "找不到端口单元格"
        assert (m2.group(1), m2.group(2)) == ("0", "2"), \
            "端口应和主机同一行、第 2 列"

    def test_host_row_is_host_side(self):
        """第一行是宿主侧地址（每台 VM 唯一），不是 VM 内部地址"""
        src = self._src()
        start = src.find('cell_widget("主机"')
        assert '"127.0.0.1"' in src[start:start + 120]

    def test_no_duplicate_username(self):
        """用户名字段只出现一次，不再单独一行「用户名」"""
        src = self._src()
        assert src.count('cell_widget("用户"') == 1
        assert '"用户名"' not in src

    def test_no_hint_text(self):
        """提示文字已移除"""
        src = self._src()
        assert "第三方 SSH 客户端按上面这几项填写即可" not in src

    def test_no_private_key_row(self):
        """私钥路径太长且不用手填，已移除"""
        src = self._src()
        assert '"私钥"' not in src

    def test_no_duplicate_ssh_line(self):
        """ssh 命令已在窗口顶部，组内不再重复"""
        src = self._src()
        start = src.find("连接信息")
        body = src[start:start + 2000]
        assert "ssh {" not in body


class TestGuestInternalInfo:
    """连接信息显示 guest 内部地址/端口，并标注「内部」"""

    @staticmethod
    def _detail() -> str:
        from pathlib import Path

        return (
            Path(__file__).parent.parent / "ui" / "vm_detail.py"
        ).read_text(encoding="utf-8")

    @staticmethod
    def _ssh_src() -> str:
        from pathlib import Path

        return (
            Path(__file__).parent.parent / "core" / "vm" / "ssh.py"
        ).read_text(encoding="utf-8")

    def test_ssh_helpers_exist(self):
        from core.vm import ssh

        assert callable(ssh.guest_ip)
        assert callable(ssh.guest_ssh_port)

    def test_guest_port_defaults_to_22(self):
        from core.vm import manager, ssh

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")
        assert ssh.guest_ssh_port(vms[0]) == 22

    def test_marks_as_internal(self):
        src = self._detail()
        assert '"内部"' in src or "'内部'" in src

    def test_loads_ip_async(self):
        """取 IP 要后台做，不能阻塞界面"""
        src = self._detail()
        assert "_load_guest_ip" in src
        assert "vm_ssh.guest_ip" in src

    def test_ip_query_excludes_loopback(self):
        src = self._ssh_src()
        start = src.find("def guest_ip")
        body = src[start:start + 900]
        assert "127." in body, "必须排除回环地址"


class TestDetailWidth:
    def test_narrower_than_main(self):
        """详情比主窗口（50%）更窄"""
        src = (     # noqa: E501
            __import__("pathlib").Path(__file__).parent.parent
            / "ui" / "vm_detail.py"
        ).read_text(encoding="utf-8")
        assert "width_ratio=0.38" in src


class TestInternalBadge:
    def test_badge_style_is_prominent(self):
        """宿主/VM 内都用徽标，不是灰色小字"""
        from pathlib import Path

        root = Path(__file__).parent.parent
        style = (root / "ui" / "style.py").read_text(encoding="utf-8")
        assert "QLabel#badgeInfo" in style
        detail = (root / "ui" / "vm_detail.py").read_text(encoding="utf-8")
        assert "badgeInfo" in detail
        assert '"宿主"' in detail, "宿主那组用醒目徽标"


class TestSlirpSubnet:
    """每台 VM 独立的用户网络网段

    QEMU 默认所有 VM 都用 10.0.2.0/24，内部 IP 全是 10.0.2.15。
    虽然各自隔离、互不冲突，但用户看到两台一样会困惑，
    也没法用内部 IP 区分虚拟机。
    """

    def test_subnets_differ_per_vm(self):
        from core.vm.qemu import slirp_subnet

        a = slirp_subnet("dev")
        b = slirp_subnet("rocky")
        assert a != b, "不同虚拟机应拿到不同网段"

    def test_subnet_stable(self):
        """同一台 VM 每次算出来必须一样（否则重启后 IP 就变）"""
        from core.vm.qemu import slirp_subnet

        assert slirp_subnet("dev") == slirp_subnet("dev")

    def test_subnet_format(self):
        from core.vm.qemu import slirp_subnet

        s = slirp_subnet("x")
        assert s.endswith(".0/24")
        parts = s.split(".")[0:3]
        assert parts[0] == "10" and parts[1] == "0"
        assert 100 <= int(parts[2]) <= 199, s

    def test_avoids_qemu_default_range(self):
        """不能落在 QEMU 默认的 10.0.2.0/24"""
        from core.vm.qemu import slirp_subnet

        for name in ("dev", "rocky", "ubuntu", "test", "a", "b"):
            assert not slirp_subnet(name).startswith("10.0.2."), name

    def test_build_command_includes_net(self):
        import inspect

        from core.vm import qemu

        src = inspect.getsource(qemu.build_command)
        assert "slirp_subnet(spec.name)" in src
        assert 'f"net=' in src


class TestHostVsGuestInfo:
    """界面要同时给出宿主侧和 VM 内两套地址"""

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        return (
            Path(__file__).parent.parent / "ui" / "vm_detail.py"
        ).read_text(encoding="utf-8")

    def test_shows_host_endpoint(self):
        src = self._src()
        assert '"127.0.0.1"' in src, "宿主侧地址"
        assert '"宿主"' in src

    def test_shows_guest_endpoint(self):
        src = self._src()
        assert '"VM 内"' in src

    def test_explains_same_ip_reason(self):
        """tooltip 要解释为什么网段是独立的"""
        src = self._src()
        assert "QEMU 用户网络" in src or "10.0.2.0/24" in src
