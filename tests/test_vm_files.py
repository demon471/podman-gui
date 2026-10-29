"""VM 文件面板（ui/vm_files.py）

用户实际报过的问题：
  1. 上传对话框选不中文件（KDE Wayland + PyInstaller 下原生 portal 失效）
  2. 下载提示「请选择要下载的文件」，但用户明明选了（选了目录）
  3. 没有权限时既不提示也无法提权
  4. tab 文字下方多一条横线（QSS 重复边框）

这些测试守住修复。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.needs_qt


@pytest.fixture
def file_panel(qapp):
    """真实 FilePanel（连 dev 或 rocky）"""
    from core.vm import manager
    from ui.vm_files import FilePanel

    vm = manager.load("dev") or manager.load("rocky")
    if vm is None:
        pytest.skip("没有可用虚拟机")

    panel = FilePanel(vm)
    panel.show()
    # 等初始加载
    import time

    for _ in range(100):
        qapp.processEvents()
        time.sleep(0.05)
        if panel.table.rowCount() > 0:
            break
    yield panel
    panel.close_session()


class TestFileDialog:
    """文件对话框必须用 Qt 内置（非原生）"""

    def test_dialog_uses_non_native(self, file_panel):
        """KDE Wayland + PyInstaller 下原生 portal 会静默失败

        表现为「对话框能打开但选不中文件、返回空」。
        必须显式要求非原生对话框。
        """
        from PyQt6.QtWidgets import QFileDialog

        kwargs = file_panel._file_dialog_kwargs()
        assert "options" in kwargs
        assert kwargs["options"] & QFileDialog.Option.DontUseNativeDialog

    def test_upload_accepts_returned_paths(self, file_panel, monkeypatch):
        """上传拿到路径后应真的走 SFTP put"""
        from PyQt6.QtWidgets import QFileDialog

        calls = []
        monkeypatch.setattr(
            QFileDialog, "getOpenFileNames",
            staticmethod(lambda *a, **k: (["/tmp/a.txt"], "")),
        )

        class FakeSession:
            def put(self, local, remote):
                calls.append((local, remote))

        file_panel._session = FakeSession()
        file_panel._upload()
        # 等后台任务
        import time
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        for _ in range(100):
            app.processEvents()
            time.sleep(0.02)
            if calls:
                break
        assert calls, "上传应调用 session.put"


class TestDownloadSelection:
    """下载的选中判定"""

    def test_directory_is_downloadable(self, file_panel, monkeypatch):
        """选中目录也应能下载（递归），不能提示「请选择文件」"""
        from PyQt6.QtWidgets import QFileDialog

        # 选第一个目录
        dirs = [
            e for e in file_panel._entries if e.is_dir
        ] if hasattr(file_panel, "_entries") else []
        if not dirs:
            # 手动构造：直接看 _download 是否过滤掉目录
            pass

        # 关键：下载逻辑要把目录也纳入（递归下载），
        # 而不是过滤掉后误报「请选择文件」
        import inspect

        src = inspect.getsource(file_panel._download)
        assert "is_dir" in src, "下载应区分文件/目录并都处理"
        assert "_download_dir" in src, "目录应递归下载"

    def test_empty_selection_gives_clear_hint(self, file_panel, monkeypatch):
        """没选中任何东西时提示要说清楚"""
        file_panel.table.clearSelection()
        msgs = []
        file_panel.status.connect(msgs.append)
        file_panel._download()
        assert any("选择" in m for m in msgs), f"应提示选择，实际 {msgs}"


class TestPermissionEscalation:
    """权限失败时的提权入口"""

    def test_permission_error_detected(self, file_panel):
        """各类权限错误文案都要能识别"""
        for msg in (
            "Permission denied",
            "[Errno 13] Permission denied",
            "EACCES: permission denied",
            "Operation not permitted",
            "权限不足",
        ):
            assert file_panel._is_permission_error(msg), msg

    def test_normal_error_not_misdetected(self, file_panel):
        """普通错误不能被当成权限问题"""
        for msg in (
            "No such file",
            "Connection refused",
            "timed out",
        ):
            assert not file_panel._is_permission_error(msg), msg

    def test_permission_error_offers_sudo(self, file_panel, monkeypatch):
        """权限失败应弹出提权对话框"""
        from PyQt6.QtWidgets import QMessageBox

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: (
                asked.append(a[2] if len(a) > 2 else ""),
                QMessageBox.StandardButton.No,
            )[1]),
        )
        file_panel._op_error("[Errno 13] Permission denied")
        assert asked, "权限错误应弹提权对话框"
        assert "root" in asked[0].lower() or "权限" in asked[0]


class TestTabStyle:
    """tab 样式必须交给 Breeze 原生渲染

    历史教训：
      · 曾经加 border-top → tab 下出现双线
      · 曾经自定义 tab 背景/边框 → Qt 放弃 Breeze 绘制，
        丢失顶部高亮条，灰色块显得突兀，和系统应用完全不像
    """

    @staticmethod
    def _style_src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "ui" / "style.py").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _strip_comments(s: str) -> str:
        import re

        return re.sub(r"/\*.*?\*/", "", s, flags=re.S)

    def test_no_qtabbar_override(self):
        """不能有 QTabBar::tab 规则

        一旦覆写，Qt 就不再用 Breeze 的绘制器 ——
        顶部高亮条会消失，选中态变成一块灰色背景。
        """
        code = self._strip_comments(self._style_src())
        assert "QTabBar::tab" not in code, (
            "不要覆写 QTabBar::tab —— 会让 Breeze 的原生 tab 绘制失效"
        )

    def test_pane_has_no_border_top(self):
        """内容区不能有上边框（会和 tab 的下边框叠成双线）"""
        import re

        code = self._strip_comments(self._style_src())
        m = re.search(r"QTabWidget::pane\s*\{{(.*?)\}}", code, re.S)
        if not m:
            return          # 没写 pane 规则也行（Breeze 默认就够）
        assert "border-top" not in m.group(1)

    def test_uses_breeze_style(self, qapp):
        """运行环境必须真的在用 Breeze 风格

        如果 QStyle 不是 Breeze，tab 的「原生绘制」就无从谈起。
        """
        from PyQt6.QtWidgets import QStyleFactory

        assert "Breeze" in QStyleFactory.keys(), "系统未安装 Breeze 风格"

    def test_tabwidget_exists_and_renders(self, qapp):
        """两个 tab 界面都能创建并渲染（防止样式改动引发异常）"""
        from PyQt6.QtWidgets import QTabWidget

        from core.podman_client import PodmanClient
        from ui.settings_page import SettingsPage

        sp = SettingsPage(PodmanClient())
        sp.show()
        for _ in range(20):
            qapp.processEvents()
        tabs = sp.findChildren(QTabWidget)
        assert tabs, "设置页应有 QTabWidget"
        assert tabs[0].count() >= 5, "设置页应有 5 个 tab"
        sp.close()


class TestBusyStateRecovery:
    """忙碌态必须能恢复 —— 用户报过「下载后所有按钮变灰」"""

    def test_set_busy_false_reenables_all(self, file_panel):
        file_panel._set_busy(True)
        assert not file_panel.btn_upload.isEnabled()
        assert not file_panel.btn_download.isEnabled()
        file_panel._set_busy(False)
        for b in (file_panel.btn_upload, file_panel.btn_download,
                  file_panel.btn_mkdir, file_panel.btn_delete):
            assert b.isEnabled(), f"{b.text()} 应恢复可用"

    def test_download_done_resets_busy(self, file_panel, monkeypatch):
        """下载完成后必须解除 busy

        历史 bug：_download 的 done 回调只 emit 状态，
        没调 _set_busy(False) → 所有按钮永久变灰，
        用户只能关窗口重开。
        """
        from PyQt6.QtWidgets import QFileDialog

        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: "/tmp"),
        )

        # 选中一个文件
        file_panel.table.selectRow(0)
        sel = file_panel._selected()
        if not sel:
            pytest.skip("没有可选条目")

        # 替换 session，避免真下载
        class FakeSession:
            def get(self, *a, **k):
                pass

            def listdir(self, *a, **k):
                return []

        file_panel._session = FakeSession()
        file_panel._download()

        # 等任务完成
        import time
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        for _ in range(150):
            app.processEvents()
            time.sleep(0.02)
            if not file_panel._busy:
                break

        assert not file_panel._busy, "下载完成后 busy 应解除"
        assert file_panel.btn_download.isEnabled(), "下载按钮应恢复"

    def test_busy_signal_emitted(self, file_panel):
        """忙碌状态变化要通过信号通知外层（禁用关闭按钮）"""
        got = []
        file_panel.busy_changed.connect(got.append)
        file_panel._set_busy(True)
        file_panel._set_busy(False)
        assert got == [True, False]


class TestSudoFallback:
    """系统目录写操作要有 sudo 兜底"""

    def test_shq_escapes_quotes(self):
        from core.vm.files import _shq

        assert _shq("a b") == "'a b'"
        assert _shq("it's") == "'it'\\''s'"

    def test_mkdir_falls_back_to_sudo(self, monkeypatch):
        """SFTP mkdir 被拒时应改用 sudo

        测的是 SftpSession.mkdir —— 权限兜底在那一层。
        """
        from core.vm import files as vf

        class FakeVM:
            spec = type("S", (), {"name": "x", "username": "u"})()
            ssh_port = 1
            ssh_key = None
            dir = None

        s = vf.SftpSession(FakeVM())

        class DeniedSftp:
            def mkdir(self, path):
                raise PermissionError("[Errno 13] Permission denied")

        s._sftp = DeniedSftp()
        s.connect = lambda: None          # 跳过真实连接

        calls = []
        monkeypatch.setattr(s, "_sudo_run", lambda cmd: calls.append(cmd))
        s.mkdir("/opt/x")
        assert calls == ["mkdir -p '/opt/x'"]

    def test_permission_denied_raises_when_no_sudo(self, monkeypatch):
        """没有 sudo 权限时要抛出明确错误，让 UI 能提示"""
        from core.vm import files as vf

        class FakeVM:
            spec = type("S", (), {"name": "x", "username": "u"})()
            ssh_port = 1
            ssh_key = None
            dir = None

        s = vf.SftpSession(FakeVM())
        # 不连 SSH，直接测 _sudo_run 的失败路径
        monkeypatch.setattr(
            "core.vm.sudo.run",
            lambda *a, **k: (False, "permission denied"),
        )
        with pytest.raises(PermissionError):
            s._sudo_run("mkdir /opt/x")


class TestConnectionRetry:
    """首次打开文件页时 VM 可能还在开机

    SSH 没就绪就必然失败，必须自动重连，而不是把
    「读取失败」丢给用户让他自己点刷新。
    """

    def test_retry_constants(self):
        from ui import vm_files as vf

        assert vf.RETRY_ATTEMPTS >= 20, "重试次数太少，覆盖不了冷启动"
        assert vf.RETRY_DELAY_MS >= 1000

    def test_conn_error_detection(self):
        from ui.vm_files import _is_conn_error

        for msg in (
            "Error reading SSH protocol banner",
            "Connection refused",
            "timed out",
            "Socket is closed",
            "Unable to connect to port 2222",
        ):
            assert _is_conn_error(msg), msg

    def test_non_conn_errors_not_retried(self):
        from ui.vm_files import _is_conn_error

        for msg in (
            "Permission denied",
            "No such file or directory",
            "读取失败: 权限不足",
        ):
            assert not _is_conn_error(msg), msg

    def test_navigate_retries_on_conn_error(self):
        from pathlib import Path

        src = (Path(__file__).parent.parent / "ui" / "vm_files.py").read_text(
            encoding="utf-8"
        )
        start = src.find("def error(msg)")
        body = src[start:start + 900]
        assert "_is_conn_error" in body
        assert "_retry_left" in body
        assert "_retry_navigate" in body

    def test_retry_does_not_reset_budget(self):
        """自动重试不能把额度重置，否则会无限重连"""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "ui" / "vm_files.py").read_text(
            encoding="utf-8"
        )
        assert "_retrying" in src
