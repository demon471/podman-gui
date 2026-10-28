"""VM 列表页交互（双击、悬停、详情入口）

用户报过的问题：
  · 未启动的虚拟机进不了详情页（双击后要等十几秒才有反应）
  · 悬停没有反应（旧版 show_for 有 NameError，一悬停就闪退）
  · 两个界面的 tab 观感不一致（内容区背景色不同）
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.needs_qt


@pytest.fixture
def vm_page(qapp):
    from ui.vm_page import VMPage

    page = VMPage()
    page.resize(700, 400)
    page.show()
    page.refresh_silent()
    page._rebuild()
    for _ in range(20):
        qapp.processEvents()
    yield page
    page.stop_timers()
    page.close()


class TestStoppedVMNoDetail:
    """未启动的虚拟机：不进详情、不显示卡片，但右键可用

    用户明确要求：
      · 未启动 → 悬停不显示信息卡片
      · 未启动 → 右键菜单里没有「详情…」
      · 未启动 → 双击不打开详情
      · 未启动 → 右键仍可用（启动 / 删除）

    原因：详情页里的文件、控制台、共享目录都依赖 guest 在运行，
    没启动时进去是空的；卡片里的 SSH 地址/密码也没意义。
    """

    @staticmethod
    def _select_vm(page, name):
        for r in range(page.model.rowCount()):
            if page.model.item(r, 0).text() == name:
                page.table.selectRow(r)
                return True
        return False

    @pytest.fixture
    def stopped_vm_page(self, vm_page, qapp, monkeypatch):
        """把 VM 置为「未启动」状态

        不真的去停 VM（守护进程会立刻恢复它，测试不稳定）。
        而是把 `_is_available` 固定为 False —— 这正是 UI 用来
        判断「能不能看详情 / 显示卡片」的唯一依据，
        所以 mock 它能精确覆盖这三条规则。
        """
        from core.vm import manager

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        name = vms[0].spec.name
        monkeypatch.setattr(vm_page, "_is_available", lambda vm: False)

        assert self._select_vm(vm_page, name)
        return vm_page, name

    def test_double_click_does_not_open_detail(self, stopped_vm_page, qapp):
        """双击未启动的 VM：不打开详情，只提示"""
        page, name = stopped_vm_page
        msgs = []
        page.status_message.connect(msgs.append)

        page._on_row_activated(page.table.currentIndex())
        for _ in range(10):
            qapp.processEvents()

        opened = any(
            w.__class__.__name__ == "VMDetailDialog" and w.isVisible()
            for w in qapp.topLevelWidgets()
        )
        assert not opened, "未启动时双击不该打开详情"
        assert any("未启动" in m for m in msgs), f"应提示未启动，实际 {msgs}"

    def test_hover_does_not_start_card_timer(self, stopped_vm_page, qapp):
        """悬停未启动的 VM：不启动卡片定时器"""
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        page, name = stopped_vm_page
        vp = page.table.viewport()
        rect = page.table.visualRect(page.model.index(0, 0))
        if rect.isEmpty():
            pytest.skip("没有可见行")

        page._card_timer.stop()
        ev = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(rect.center()),
            QPointF(vp.mapToGlobal(rect.center())),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        page.eventFilter(vp, ev)
        for _ in range(10):
            qapp.processEvents()

        assert not page._card_timer.isActive(), (
            "未启动的 VM 不该触发悬停卡片"
        )
        assert page._pending_row == -1

    def test_context_menu_has_no_detail(self, stopped_vm_page, qapp):
        """右键菜单：未启动时没有「详情…」，但有启动/删除"""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu

        page, name = stopped_vm_page
        captured = []
        orig = QMenu.exec

        def spy(self, *a, **k):
            captured.append([x.text() for x in self.actions() if x.text()])
            return None

        QMenu.exec = spy
        try:
            page._context_menu(QPoint(10, 10))
        finally:
            QMenu.exec = orig

        assert captured, "应弹出右键菜单"
        items = captured[0]
        assert not any("详情" in x for x in items), (
            f"未启动时不该有「详情…」，实际 {items}"
        )
        assert any("启动" in x for x in items), "应有「启动」"
        assert any("删除" in x for x in items), "应有「删除」"


class TestRunningVMHasDetail:
    """已启动的虚拟机：三项入口都要正常"""

    @pytest.fixture
    def running_vm_page(self, vm_page, qapp, monkeypatch):
        from core.vm import manager

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        name = vms[0].spec.name
        # 固定为「可用」，避免依赖守护进程的实时状态
        monkeypatch.setattr(vm_page, "_is_available", lambda vm: True)
        for r in range(vm_page.model.rowCount()):
            if vm_page.model.item(r, 0).text() == name:
                vm_page.table.selectRow(r)
                break
        return vm_page, name

    def test_hover_starts_card_timer(self, running_vm_page, qapp):
        """已启动：悬停要触发卡片"""
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        page, name = running_vm_page
        vp = page.table.viewport()
        rect = page.table.visualRect(page.model.index(0, 0))
        if rect.isEmpty():
            pytest.skip("没有可见行")

        page._card_timer.stop()
        ev = QMouseEvent(
            QMouseEvent.Type.MouseMove,
            QPointF(rect.center()),
            QPointF(vp.mapToGlobal(rect.center())),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        page.eventFilter(vp, ev)
        for _ in range(10):
            qapp.processEvents()
        assert page._card_timer.isActive(), "已启动的 VM 应触发悬停卡片"

    def test_context_menu_has_detail(self, running_vm_page, qapp):
        """已启动：右键菜单要有「详情…」"""
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu

        page, name = running_vm_page
        captured = []
        orig = QMenu.exec

        def spy(self, *a, **k):
            captured.append([x.text() for x in self.actions() if x.text()])
            return None

        QMenu.exec = spy
        try:
            page._context_menu(QPoint(10, 10))
        finally:
            QMenu.exec = orig

        items = captured[0] if captured else []
        assert any("详情" in x for x in items), (
            f"已启动时应有「详情…」，实际 {items}"
        )


class TestHoverCard:
    """悬停信息卡片"""

    def test_card_shows_for_stopped_vm(self, vm_page, qapp):
        """未运行状态也要能填内容（旧版这里 NameError → 闪退）"""
        from core.vm import manager

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        card = vm_page._info_card
        card.show_for(vms[0], False)
        for _ in range(10):
            qapp.processEvents()
        assert card.lbl_name.text() == vms[0].spec.name
        assert card.lbl_hint.text() != ""

    def test_hide_card_resets_pending_row(self, vm_page):
        """_hide_card 必须重置 _pending_row

        不重置的话，鼠标离开再回到同一行时 row 相同，
        定时器不会重启 → 卡片再也不显示（用户感觉「悬停没反应」）。
        """
        vm_page._pending_row = 0
        vm_page._card_row = 0
        vm_page._hide_card()
        assert vm_page._pending_row == -1
        assert vm_page._card_row == -1

    def test_card_timer_configured(self, vm_page):
        """卡片延时定时器配置正确（单次、有延时）"""
        assert vm_page._card_timer.isSingleShot()
        assert vm_page._card_timer.interval() > 0

    def test_show_card_now_needs_valid_row(self, vm_page, qapp):
        """行号无效时不显示卡片（不崩）"""
        vm_page._pending_row = -1
        vm_page._pending_pos = None
        vm_page._show_card_now()      # 不应抛异常
        assert not vm_page._info_card.isVisible()


class TestTabConsistency:
    """两个 tab 界面的视觉一致性"""

    def test_pane_uses_window_background(self):
        """QTabWidget::pane 必须用 window 色

        用 base（纯白）时，内部的白色 GroupBox 贴白底，
        边界看不出来，显得糊；设置页用的是 window 色（浅灰），
        两处不一致就会被用户看出来「样式不一样」。
        """
        import re
        from pathlib import Path

        style = (Path(__file__).parent.parent / "ui" / "style.py").read_text(
            encoding="utf-8"
        )
        code = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        m = re.search(r"QTabWidget::pane\s*\{{(.*?)\}}", code, re.S)
        assert m, "应有 QTabWidget::pane 规则"
        body = m.group(1)
        assert "{window}" in body, "pane 背景应用 window 色"
        assert "{base}" not in body, "pane 背景不要用 base（纯白）"

    def test_both_dialogs_share_background(self, qapp):
        """实测两个界面的内容区背景色一致"""
        from core.podman_client import PodmanClient
        from core.vm import manager
        from ui.settings_page import SettingsPage
        from ui.vm_detail import VMDetailDialog

        sp = SettingsPage(PodmanClient())
        sp.resize(700, 420)
        sp.show()

        vms = manager.load_all()
        if not vms:
            sp.close()
            pytest.skip("没有虚拟机")

        dlg = VMDetailDialog(vms[0])
        dlg.resize(700, 420)
        dlg.show()

        for _ in range(40):
            qapp.processEvents()
            time.sleep(0.01)

        # 采样内容区左侧背景
        sp_img = sp.grab().toImage()
        dlg_img = dlg.grab().toImage()

        # settings 的 tab 在顶部，内容从 y=50 开始
        sp_bg = sp_img.pixelColor(20, sp_img.height() - 30).name()
        # vm_detail 有头部，内容区更靠下
        dlg_bg = dlg_img.pixelColor(20, dlg_img.height() - 30).name()

        sp.close()
        dlg.close()

        assert sp_bg == dlg_bg, (
            f"两个界面内容区背景应一致：settings={sp_bg} vm={dlg_bg}"
        )


class TestDetailHeader:
    """详情页头部承载原「概览」的关键信息

    用户要求精简：概览页里操作系统、创建时间、运行时长、
    guest 系统、端口映射都属于冗余，只保留真正有用的
    「SSH 访问地址」，并合并到头部。
    """

    @pytest.fixture
    def detail(self, qapp):
        from core.vm import manager
        from ui.vm_detail import VMDetailDialog

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")

        dlg = VMDetailDialog(vms[0])
        dlg.show()
        for _ in range(30):
            qapp.processEvents()
            time.sleep(0.01)
        yield dlg, vms[0]
        dlg.close()

    def test_no_overview_tab(self, detail):
        """概览 tab 应已删除"""
        dlg, vm = detail
        names = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        assert "概览" not in names, f"不该再有概览页，实际 {names}"
        assert names == ["文件", "共享目录", "控制台"], names

    def test_header_shows_ssh_address(self, detail):
        """头部显示可复制的 SSH 地址"""
        dlg, vm = detail
        assert "ssh " in dlg.lbl_ssh.text()
        assert vm.spec.name in dlg.lbl_ssh.text()

    def test_header_shows_image_and_state(self, detail):
        """副标题显示「镜像 · 状态」"""
        dlg, vm = detail
        text = dlg.lbl_sub.text()
        assert vm.spec.image_key in text
        assert ("运行中" in text) or ("已停止" in text)

    def test_redundant_fields_removed(self, detail):
        """冗余字段的控件应已不存在"""
        dlg, vm = detail
        for attr in (
            "lbl_os", "lbl_created", "lbl_uptime",
            "lbl_host", "lbl_ports", "lbl_access",
            "btn_refresh_sys",
        ):
            assert not hasattr(dlg, attr), f"{attr} 应已删除"

    def test_copy_ssh_button_works(self, detail, qapp):
        """复制按钮把完整命令放进剪贴板"""
        from PyQt6.QtGui import QGuiApplication

        dlg, vm = detail
        dlg._copy_ssh()
        clip = QGuiApplication.clipboard().text()
        assert "ssh" in clip
        assert vm.spec.name in clip or "127.0.0.1" in clip
