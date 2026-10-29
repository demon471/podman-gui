"""UI 冒烟测试：把每个页面真实创建并渲染一遍

为什么需要它
────────────
静态检查（pyflakes）只能发现「名字根本没定义」；
而 `running` → `available` 这类**参数改名后漏改引用**，
pyflakes 是查不出来的（它是函数内的自由变量引用）。

这类错误只在**真正执行到那一行**时才炸，而 Qt 事件循环里
抛异常会直接 abort 整个进程（用户看到的就是闪退）。

所以：把每个页面都创建出来、调用 refresh/rebuild、
触发展示逻辑，让这些代码路径都跑一遍。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.needs_qt


@pytest.fixture(scope="module")
def main_window(qapp):
    """真实 MainWindow（带 Podman 客户端）"""
    from core.podman_client import PodmanClient
    from ui.main_window import MainWindow

    win = MainWindow(PodmanClient())
    win.show()
    yield win
    try:
        win.page_vms.stop_timers()
    except Exception:
        pass
    win.close()


class TestAllPagesRender:
    """逐个切页，确保没有运行时异常"""

    KEYS = [
        "containers", "images", "volumes", "networks",
        "pods", "compose", "vms", "system", "settings",
    ]

    @pytest.mark.parametrize("key", KEYS)
    def test_page_switches_without_error(self, main_window, qapp, key):
        """切到每个页面并刷新 —— 覆盖 fetch_data / row_from_item"""
        win = main_window
        win._on_nav(key)
        for _ in range(20):
            qapp.processEvents()
        page = win.stack.currentWidget()
        assert page is not None

    def test_all_pages_in_sequence(self, main_window, qapp):
        """连续切一遍所有页面"""
        win = main_window
        for key in self.KEYS:
            win._on_nav(key)
            for _ in range(10):
                qapp.processEvents()


class TestVMInfoCard:
    """悬停信息卡片 —— 历史闪退点（参数改名漏改）"""

    def test_show_for_running_vm(self, main_window, qapp):
        """运行中的 VM：卡片能正常填充"""
        from core.vm import manager

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        card = main_window.page_vms._info_card
        # available=True 分支
        card.show_for(vms[0], True)
        for _ in range(10):
            qapp.processEvents()
        assert card.lbl_name.text() == vms[0].spec.name

    def test_show_for_stopped_vm(self, main_window, qapp):
        """未运行的 VM：走「未运行提示」分支

        这一分支历史上因为参数从 running 改名成 available
        而漏改，导致 NameError → 闪退。
        """
        from core.vm import manager

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        card = main_window.page_vms._info_card
        card.show_for(vms[0], False)     # 关键：available=False
        for _ in range(10):
            qapp.processEvents()
        # 走到这里没异常就说明该分支安全
        assert card.lbl_hint.text() != ""

    def test_show_for_both_states(self, main_window, qapp):
        """两种状态连续切换，不能残留错误状态"""
        from core.vm import manager

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        card = main_window.page_vms._info_card
        for available in (True, False, True, False):
            card.show_for(vms[0], available)
            for _ in range(5):
                qapp.processEvents()


class TestVMPageInteractions:
    """VM 页面各种交互路径"""

    def test_rebuild_with_selection(self, main_window, qapp):
        win = main_window
        win._on_nav("vms")
        page = win.page_vms
        page.refresh_silent()
        if page.model.rowCount() > 0:
            page.table.selectRow(0)
            page._update_actions()
        for _ in range(10):
            qapp.processEvents()

    def test_pending_state_rendering(self, main_window, qapp):
        """loading 态渲染（灰字 + 转圈）"""
        win = main_window
        page = win.page_vms
        page.refresh_silent()
        if page.model.rowCount() == 0:
            pytest.skip("没有虚拟机")

        name = page.model.item(0, 0).text()
        page._pending_vms.add(name)
        page._starting.add(name)
        try:
            page._rebuild()
            for _ in range(10):
                qapp.processEvents()
            # 状态列应显示「启动中…」
            assert page.model.item(0, 1).text() == "启动中…"
        finally:
            page._pending_vms.discard(name)
            page._starting.discard(name)
            page._rebuild()

    def test_keepalive_status_queries(self, main_window, qapp):
        """保活状态查询不应抛异常"""
        win = main_window
        page = win.page_vms
        page.refresh_silent()
        for vm in page._vms:
            name = vm.spec.name
            page.keepalive.wants_running(name)
            page.keepalive.idle_left(name)
            page.keepalive.needs_wake(name)
            page.keepalive.status(name)
            page._is_available(vm)


class TestVMDetailDialog:
    """详情对话框（含文件面板）"""

    def test_opens_and_renders(self, main_window, qapp):
        from core.vm import manager
        from ui.vm_detail import VMDetailDialog

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        dlg = VMDetailDialog(vms[0])
        dlg.show()
        for _ in range(30):
            qapp.processEvents()

        # 逐个 tab 切换
        for i in range(dlg.tabs.count()):
            dlg.tabs.setCurrentIndex(i)
            for _ in range(10):
                qapp.processEvents()

        dlg.close()


class TestDetailDialogCleanup:
    """详情页精简：去掉无用的串口输出区与「N 项」计数

    这两块占了界面空间却没提供信息：
      · 串口输出几乎总是空的，排查启动失败直接看 console.log
      · 文件列表的「N 项」肉眼可见，纯噪音
    """

    @staticmethod
    def _detail_src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "ui" / "vm_detail.py").read_text(
            encoding="utf-8"
        )

    def test_no_console_output_group(self):
        src = self._detail_src()
        assert "控制台输出" not in src
        assert "QEMU 串口输出" not in src

    def test_no_console_widget(self):
        src = self._detail_src()
        assert "self.console" not in src
        assert "_load_console" not in src

    def test_no_textedit_import(self):
        """QTextEdit 只在串口输出区用过，删了就该一起清掉"""
        src = self._detail_src()
        assert "QTextEdit" not in src

    def test_tab_renamed_to_account(self):
        """tab 只剩账户 + 密码，名字不该再叫「控制台」"""
        src = self._detail_src()
        assert '"账户")' in src
        assert '"控制台")' not in src

    def test_shares_tab_index_unchanged(self):
        """删掉内容不能改变 tab 顺序（重启后要定位共享目录）"""
        src = self._detail_src()
        # 文件=0, 共享目录=1, 账户=2
        files_pos = src.find('"文件")')
        shares_pos = src.find('"共享目录")')
        account_pos = src.find('"账户")')
        assert 0 < files_pos < shares_pos < account_pos

    def test_no_item_count_status(self):
        """文件列表不再显示「N 项」"""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "ui" / "vm_files.py").read_text(
            encoding="utf-8"
        )
        # 列表刷新后不再报告条目数
        assert 'len(entries)} 项' not in src
        # 但删除提示这类有用信息要保留
        assert "已删除" in src


class TestPasswordPanel:
    """账户页「修改密码」面板

    覆盖用户实际会踩的点：看不见密码、出错不知道哪栏错、
    连点两次提交、成功/失败没有区分。
    """

    @pytest.fixture
    def dlg(self, qapp):
        from core.vm import manager
        from ui.vm_detail import VMDetailDialog

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")
        d = VMDetailDialog(vms[0])
        d.tabs.setCurrentIndex(2)          # 账户
        d.show()
        for _ in range(10):
            qapp.processEvents()
        yield d
        d.close()

    # ── 成功态 ──
    def test_success_flashes_on_button(self, dlg, qapp):
        """成功反馈落在按钮上：对勾 + 「保存成功」，且文字走禁用灰"""
        dlg._flash_pwd_success()
        assert dlg.btn_chpwd.text() == "保存成功"
        assert not dlg.btn_chpwd.isEnabled(), "按钮应禁用（灰字）"

    def test_success_locks_all_three_buttons(self, dlg, qapp):
        """保存成功期间三个按钮都点不动 —— 天然防抖"""
        dlg._flash_pwd_success()
        assert not dlg.btn_chpwd.isEnabled()
        assert not dlg.btn_pwd_clear.isEnabled()
        assert not dlg.btn_close.isEnabled()
        assert dlg._pwd_busy is True

    def test_success_restores_after_delay(self, dlg, qapp):
        """1.5s 后全部复原"""
        dlg._flash_pwd_success()
        dlg._restore_pwd_button()
        assert dlg.btn_chpwd.text() == "修改密码"
        assert dlg.btn_chpwd.isEnabled()
        assert dlg.btn_pwd_clear.isEnabled()
        assert dlg.btn_close.isEnabled()
        assert dlg._pwd_busy is False

    def test_debounce_blocks_repeat_submit(self, dlg, qapp):
        """成功提示期间再点不会重复提交"""
        dlg._flash_pwd_success()
        before = dlg.lbl_chpwd_status.text()
        dlg._change_password()
        assert dlg.lbl_chpwd_status.text() == before
        dlg._restore_pwd_button()

    # ── 进行中 ──
    def test_busy_disables_inputs_and_buttons(self, dlg, qapp):
        dlg._set_pwd_busy(True)
        assert dlg.btn_chpwd.text() == "修改中…"
        for w in (dlg.btn_chpwd, dlg.btn_pwd_clear,
                  dlg.ed_old_pwd, dlg.ed_new_pwd, dlg.ed_new_pwd2):
            assert not w.isEnabled()
        dlg._set_pwd_busy(False)
        assert dlg.btn_chpwd.text() == "修改密码"
        assert dlg.ed_old_pwd.isEnabled()

    # ── 密码可见性 ──
    def test_eye_toggles_echo_mode(self, dlg, qapp):
        """眼睛按钮：密码明文 ↔ 圆点"""
        from PyQt6.QtWidgets import QLineEdit

        ed = dlg.ed_new_pwd
        assert ed.echoMode() == QLineEdit.EchoMode.Password
        act = dlg._eye_actions[1]
        act.trigger()
        assert ed.echoMode() == QLineEdit.EchoMode.Normal
        act.trigger()
        assert ed.echoMode() == QLineEdit.EchoMode.Password

    def test_every_field_has_eye_action(self, dlg):
        """三个输入框都要能切换可见性"""
        assert len(dlg._eye_actions) == 3

    # ── 实时校验 ──
    def test_live_validation_mismatch(self, dlg, qapp):
        dlg.ed_new_pwd.setText("abcdefgh")
        dlg.ed_new_pwd2.setText("abcdxxxx")
        assert "不一致" in dlg.lbl_pwd_match.text()
        assert dlg.lbl_pwd_match.objectName() == "errLabel"

    def test_live_validation_match(self, dlg, qapp):
        dlg.ed_new_pwd.setText("abcdefgh")
        dlg.ed_new_pwd2.setText("abcdefgh")
        assert "一致" in dlg.lbl_pwd_match.text()
        assert dlg.lbl_pwd_match.objectName() == "okLabel"

    def test_live_validation_length(self, dlg, qapp):
        dlg.ed_new_pwd.setText("ab")
        assert "还差 2 位" in dlg.lbl_pwd_rule.text()
        assert dlg.lbl_pwd_rule.objectName() == "errLabel"
        dlg.ed_new_pwd.setText("abcd")
        assert dlg.lbl_pwd_rule.objectName() == "okLabel"

    # ── 错误反馈 ──
    def test_empty_old_password_error(self, dlg, qapp):
        """缺当前密码：红字提示 + 焦点回到该栏"""
        dlg.ed_old_pwd.clear()
        dlg.ed_new_pwd.setText("abcdefgh")
        dlg.ed_new_pwd2.setText("abcdefgh")
        dlg._change_password()
        assert "当前密码" in dlg.lbl_chpwd_status.text()
        assert dlg.lbl_chpwd_status.objectName() == "errLabel"

    def test_mismatch_error_points_to_confirm_field(self, dlg, qapp):
        dlg.ed_old_pwd.setText("12345")
        dlg.ed_new_pwd.setText("abcdefgh")
        dlg.ed_new_pwd2.setText("different")
        dlg._change_password()
        assert "不一致" in dlg.lbl_chpwd_status.text()
        assert dlg.lbl_chpwd_status.objectName() == "errLabel"

    def test_same_password_rejected(self, dlg, qapp):
        dlg.ed_old_pwd.setText("abcdefgh")
        dlg.ed_new_pwd.setText("abcdefgh")
        dlg.ed_new_pwd2.setText("abcdefgh")
        dlg._change_password()
        assert "不能与当前密码相同" in dlg.lbl_chpwd_status.text()

    # ── 提示位置 ──
    def test_warning_comes_first(self):
        """改密码后果的提醒必须在最前面（红字）"""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "ui" / "vm_detail.py").read_text(
            encoding="utf-8"
        )
        start = src.find("def _build_account")
        end = src.find("def _add_pwd_field", start)
        body = src[start:end]
        warn_pos = body.find('"warnLabel"')
        old_field_pos = body.find('"当前密码"')
        assert warn_pos > 0 and old_field_pos > 0
        assert warn_pos < old_field_pos, "警示应在当前密码输入框之前"

    def test_warning_style_is_red(self, qapp):
        from ui.style import build_stylesheet

        css = build_stylesheet()
        assert "QLabel#warnLabel" in css

    def test_no_noise_status_text(self):
        """底部不再有「操作进行中，请稍候…」这类噪音"""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "ui" / "vm_detail.py").read_text(
            encoding="utf-8"
        )
        # 只允许出现在注释里
        for line in src.splitlines():
            if "操作进行中" in line:
                assert line.strip().startswith("#") or "不再" in line, line


class TestEmptyStateSimplified:
    """空状态只留图标 + 标题

    说明文字是在教用户用界面（按钮就在工具栏上），
    操作按钮又和工具栏重复，两者都去掉。
    """

    def test_no_action_button(self, qapp):
        from ui.widgets import EmptyState

        es = EmptyState(icon_name="volume", title="还没有卷")
        buttons = es.findChildren(type(es).__mro__[0])  # 占位，真正检查在下面
        from PyQt6.QtWidgets import QPushButton

        assert es.findChildren(QPushButton) == [], "空状态不应再有按钮"
        es.deleteLater()

    def test_subtitle_hidden_by_default(self, qapp):
        from ui.widgets import EmptyState

        es = EmptyState(icon_name="volume", title="还没有卷")
        assert es._subtitle.isVisible() is False
        es.deleteLater()

    def test_subtitle_usable_for_errors(self, qapp):
        """连接断开这类「不是空而是出错」的场景仍需要说明"""
        from ui.widgets import EmptyState

        es = EmptyState(icon_name="volume", title="Podman 服务已断开")
        es.set_text("Podman 服务已断开", "无法连接到 Podman，正在等待重连…")
        assert es._subtitle.text().startswith("无法连接")
        es.deleteLater()

    def test_pages_do_not_pass_action(self):
        from pathlib import Path

        root = Path(__file__).parent.parent / "ui"
        for name in ("container_page.py", "resource_page.py", "vm_page.py"):
            src = (root / name).read_text(encoding="utf-8")
            start = src.find("EmptyState(")
            assert start > 0, name
            block = src[start:start + 400]
            assert "action_text" not in block, f"{name} 仍在传 action_text"
            assert "on_action" not in block, f"{name} 仍在传 on_action"


class TestTrayMenuStability:
    """托盘菜单不能在弹出过程中被重建

    aboutToShow 里 menu.clear() 会销毁正在展示的 QAction，
    KDE/Wayland 下点击落空 —— 用户看到「点退出没反应」。
    """

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "ui" / "tray.py").read_text(
            encoding="utf-8"
        )

    def test_not_rebuilt_on_about_to_show(self):
        src = self._src()
        assert "aboutToShow.connect(self._rebuild_menu)" not in src, (
            "不能在 aboutToShow 里重建菜单"
        )

    def test_guarded_while_visible(self):
        src = self._src()
        start = src.find("def _rebuild_menu")
        body = src[start:start + 700]
        assert "isVisible()" in body, "菜单可见时必须跳过重建"

    def test_rebuilds_after_data_refresh(self):
        src = self._src()
        start = src.find("def refresh")
        end = src.find("def _menu_signature", start)
        body = src[start:end]
        assert "_rebuild_menu()" in body, "数据刷新后应重建菜单"

    def test_quit_action_uses_explicit_lambda(self):
        src = self._src()
        assert "lambda *_: self.quit_app.emit()" in src

    def test_menu_built_when_no_containers(self, qapp):
        """回归：空容器列表时菜单也必须建出来

        曾经用 () 当 _menu_sig 初始值，而空列表的指纹正好也是 ()，
        被判成「内容没变」直接跳过 —— 菜单一个项都没有，
        右键看起来就是完全失效。
        """
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QMenu

        from ui.tray import TrayIcon

        class Stub(QObject):
            show_window = pyqtSignal()
            quit_app = pyqtSignal()
            _menu_signature = TrayIcon._menu_signature
            _rebuild_menu = TrayIcon._rebuild_menu

        st = Stub()
        st.menu = QMenu()
        st._containers = []
        st._menu_sig = None          # 哨兵：还没构建过
        st._menu_dirty = False

        st._rebuild_menu()

        texts = [a.text() for a in st.menu.actions()]
        assert any("退出" in t for t in texts), (
            f"空容器时菜单缺「退出」，实际 {texts}"
        )
        assert any("显示主窗口" in t for t in texts), texts

    def test_signature_sentinel_is_none(self):
        """初始指纹必须是 None，不能是空元组"""
        src = self._src()
        assert "_menu_sig: tuple | None = None" in src

    def test_no_rebuild_when_signature_unchanged(self, qapp):
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QMenu

        from ui.tray import TrayIcon

        class Stub(QObject):
            show_window = pyqtSignal()
            quit_app = pyqtSignal()
            _menu_signature = TrayIcon._menu_signature
            _rebuild_menu = TrayIcon._rebuild_menu

        st = Stub()
        st.menu = QMenu()
        st._containers = []
        st._menu_sig = None
        st._menu_dirty = False
        st._rebuild_menu()

        before = [a.text() for a in st.menu.actions()]
        st._rebuild_menu()
        assert [a.text() for a in st.menu.actions()] == before

    def test_skip_rebuild_while_visible(self, qapp):
        """菜单开着时改动数据：不能重建，只标脏"""
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QMenu

        from ui.tray import TrayIcon

        class Stub(QObject):
            show_window = pyqtSignal()
            quit_app = pyqtSignal()
            _menu_signature = TrayIcon._menu_signature
            _rebuild_menu = TrayIcon._rebuild_menu

        st = Stub()
        st.menu = QMenu()
        st._containers = []
        st._menu_sig = None
        st._menu_dirty = False
        st._rebuild_menu()

        before = [a.text() for a in st.menu.actions()]
        st.menu.show()
        st._menu_sig = ("changed",)      # 伪造数据变化
        st._rebuild_menu()

        assert st._menu_dirty is True
        assert [a.text() for a in st.menu.actions()] == before, (
            "菜单可见时不应被清空重建"
        )
        st.menu.hide()


class TestToolbarDangerColor:
    """工具栏删除按钮不再用红色

    整排按钮只有它一个红块，显得突兀；
    危险性由二次确认对话框承担。
    """

    def test_no_danger_icon_in_toolbars(self):
        from pathlib import Path

        root = Path(__file__).parent.parent / "ui"
        for name in ("container_page.py", "resource_page.py",
                     "compose_page.py"):
            src = (root / name).read_text(encoding="utf-8")
            assert 'icon("delete", danger=True)' not in src, (
                f"{name} 的删除按钮仍是红色"
            )


class TestListIconAlignment:
    """列表里图标和文字必须视觉居中对齐

    Qt 把图标按单元格矩形居中、文字按字体基线居中，
    字体下伸部把文字视觉中心拉低，图标总是高 2px。
    """

    def _table(self, qapp, use_delegate=True):
        from PyQt6.QtCore import QSize
        from PyQt6.QtGui import QStandardItem, QStandardItemModel
        from PyQt6.QtWidgets import QTableView

        from resources.icons import icon
        from ui.widgets import apply_aligned_delegate

        t = QTableView()
        t.horizontalHeader().setVisible(False)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(26)
        t.setIconSize(QSize(16, 16))
        if use_delegate:
            apply_aligned_delegate(t)
        m = QStandardItemModel(1, 1)
        m.setItem(0, 0, QStandardItem(icon("pod", 16), "pod_compose"))
        t.setModel(m)
        t.resize(200, 40)
        t.show()
        for _ in range(6):
            qapp.processEvents()
        return t

    @staticmethod
    def _ink_center_y(view, qapp, x0, x1):
        img = view.grab().toImage()
        ys = []
        for y in range(img.height()):
            for x in range(x0, x1):
                c = img.pixelColor(x, y)
                if c.alpha() > 60 and c.lightness() < 140:
                    ys.append(y)
        return (min(ys) + max(ys)) / 2.0 if ys else None

    def test_icon_centered_with_text(self, qapp):
        """图标墨迹中心与文字墨迹中心重合（容差 1px）"""
        t = self._table(qapp)
        try:
            icon_c = self._ink_center_y(t, qapp, 2, 32)
            text_c = self._ink_center_y(t, qapp, 34, 200)
            assert icon_c is not None and text_c is not None
            assert abs(icon_c - text_c) <= 1.0, (
                f"图标中心 {icon_c} 与文字中心 {text_c} 偏差过大"
            )
        finally:
            t.close()

    def test_delegate_shifts_icon_by_font_metrics(self, qapp):
        """委托算出的 y 必须基于字体度量，而不是写死的偏移

        换字体后偏差值会变（测试环境字体恰好偏差为 0），
        所以这里验证的是「计算方式」而不是某个具体像素。
        """
        from pathlib import Path

        src = (Path(__file__).parent.parent / "ui" / "widgets.py").read_text(
            encoding="utf-8"
        )
        start = src.find("class AlignedItemDelegate")
        body = src[start:src.find("def _tint_icon", start)]
        assert "fm.ascent()" in body, "必须用字体 ascent 计算基线"
        assert "fm.xHeight()" in body, "必须用 x-height 计算视觉中心"
        assert "SE_ItemViewItemDecoration" in body, "横向位置要沿用样式计算结果"

    def test_delegate_installed_and_idempotent(self, qapp):
        from ui.widgets import AlignedItemDelegate, apply_aligned_delegate

        t = self._table(qapp, use_delegate=False)
        try:
            apply_aligned_delegate(t)
            first = t.itemDelegate()
            assert isinstance(first, AlignedItemDelegate)
            apply_aligned_delegate(t)
            assert t.itemDelegate() is first, "重复调用不应换新委托"
        finally:
            t.close()

    def test_selected_row_icon_tinted(self, qapp):
        """选中行图标要跟文字一起变高亮色"""
        t = self._table(qapp)
        try:
            t.selectRow(0)
            for _ in range(6):
                qapp.processEvents()
            img = t.grab().toImage()
            # 选中行图标区域应有浅色像素（高亮前景）
            light = 0
            for y in range(img.height()):
                for x in range(2, 32):
                    c = img.pixelColor(x, y)
                    if c.alpha() > 60 and c.lightness() > 200:
                        light += 1
            assert light > 10, "选中态图标没有变亮，压在蓝底上看不清"
        finally:
            t.close()

    def test_all_main_lists_use_delegate(self):
        """所有主列表都要装上这个委托"""
        from pathlib import Path

        root = Path(__file__).parent.parent / "ui"
        expected = {
            "container_page.py": "self.table",
            "resource_page.py": "self.table",
            "vm_page.py": "self.table",
            "vm_files.py": "self.table",
            "vm_detail.py": "self.tbl_shares",
            "compose_page.py": "self.tbl_svc",
            "sidebar.py": "self.list",
        }
        for name, attr in expected.items():
            src = (root / name).read_text(encoding="utf-8")
            assert f"apply_aligned_delegate({attr})" in src, (
                f"{name} 的列表没有接入对齐委托"
            )
