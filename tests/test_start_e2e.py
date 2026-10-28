"""端到端：启动流程的通知/列表一致性（需要真实 VM）

用 @pytest.mark.needs_vm 隔离，默认不跑。
执行：./run_tests.sh --all  或  pytest -m needs_vm
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.needs_vm


class TestStartEndToEnd:
    def test_notification_matches_list_state(self, qapp):
        """冷启动一台 VM：通知只发一次，且列表同步变为「运行中」

        这是用户报的 bug：通知说已启动，列表还显示启动中。
        """
        from core.vm import manager, qemu

        vm = manager.load("rocky")
        if vm is None:
            pytest.skip("rocky 不存在")

        if vm.running:
            manager.stop_vm(vm, timeout=30)
            time.sleep(2)
        qemu.drop_snapshot(vm.dir)      # 强制冷启动

        from ui.vm_page import VMPage

        page = VMPage()
        page.refresh_silent()
        page._rebuild()

        # 拦截通知
        events = []
        import core.notify as N

        orig = N.notifier().info
        N.notifier().info = lambda s, b="": (
            events.append(s), orig(s, b)
        )[1]

        try:
            for r in range(page.model.rowCount()):
                if page.model.item(r, 0).text() == "rocky":
                    page.table.selectRow(r)
                    break
            page._start_selected()

            deadline = time.time() + 120
            while time.time() < deadline:
                qapp.processEvents()
                time.sleep(0.1)
                if "rocky" not in page._pending_vms:
                    break

            # 断言
            assert len(events) == 1, f"应只通知一次，实际 {events}"
            assert "就绪" in events[0]

            text = ""
            for r in range(page.model.rowCount()):
                if page.model.item(r, 0).text() == "rocky":
                    text = page.model.item(r, 1).text()
                    break
            assert text == "运行中", f"列表应显示运行中，实际 {text!r}"
        finally:
            N.notifier().info = orig
            page.stop_timers()
