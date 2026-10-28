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
