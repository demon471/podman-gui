"""设置页结构

用户要求：简单好用、符合习惯。
  · 服务默认用户级，未安装要能引导安装
  · 资源用「滑块 + 快捷档位」
  · 镜像加速改完即生效，不用点保存
  · 概览并入关于
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def page(qapp):
    from core.podman_client import PodmanClient
    from ui.settings_page import SettingsPage

    p = SettingsPage(PodmanClient())
    yield p

    # deleteLater() 只是排队，要等事件循环才真正销毁。
    # 不在这里驱动一次的话，几十个 SettingsPage 会一直活着，
    # 它们的延迟刷新定时器（80ms / 500ms）持续排队，代价最后
    # 算到下一个调用 processEvents 的用例头上 —— 表现为某个
    # 无引用例莫名其妙跑 50 秒。
    p.close()
    p.deleteLater()
    qapp.processEvents()


class TestTabLayout:
    def test_tab_order(self, page):
        """按「多久用一次」排：服务在最前，关于在最后"""
        names = [page.tabs.tabText(i) for i in range(page.tabs.count())]
        assert names == ["服务", "资源", "镜像", "存储", "关于"], names

    def test_overview_merged_into_about(self, page):
        """概览页已删除，数据进关于"""
        names = [page.tabs.tabText(i) for i in range(page.tabs.count())]
        assert "概览" not in names
        # 原概览的数据现在挂在关于页上
        for attr in ("_ov_version", "_ov_rootless", "_ov_kernel",
                     "_ov_driver", "_ov_socket", "_ov_images"):
            assert hasattr(page, attr), f"关于页缺少 {attr}"

    def test_all_tabs_render(self, page, qapp):
        for i in range(page.tabs.count()):
            page.tabs.setCurrentIndex(i)
            for _ in range(5):
                qapp.processEvents()


class TestServiceTab:
    def test_has_install_guidance(self, page):
        """未安装 podman 时要有引导安装入口"""
        assert hasattr(page, "box_not_installed")
        assert hasattr(page, "btn_install")
        from core import podman_config as cfg

        assert hasattr(cfg, "install_podman")
        assert hasattr(cfg, "podman_installed")

    def test_only_user_level_by_default(self, page):
        """主操作区只针对用户级服务，rootful 收进高级"""
        assert hasattr(page, "btn_svc_start")
        assert hasattr(page, "btn_svc_stop")
        assert hasattr(page, "btn_svc_restart")
        # 系统级默认隐藏（用 isHidden：页面未 show 时 isVisible 恒为 False）
        assert page.box_advanced.isHidden() is True

    def test_advanced_toggle(self, page, qapp):
        page.btn_advanced.setChecked(True)
        qapp.processEvents()
        assert page.box_advanced.isHidden() is False
        page.btn_advanced.setChecked(False)
        qapp.processEvents()
        assert page.box_advanced.isHidden() is True

    def test_buttons_reflect_state(self, page, qapp):
        """服务在跑时「启动」应禁用，避免点了没反应"""
        page._refresh_service()
        qapp.processEvents()
        running = page.btn_svc_stop.isEnabled()
        assert page.btn_svc_start.isEnabled() != running


class TestResourcesTab:
    @pytest.fixture(autouse=True)
    def _restore_limit(self):
        """测试会真的写配置，结束后必须还原用户原值"""
        from core import podman_config as cfg

        original = cfg.get_memory_limit_gb()
        yield
        cfg.set_memory_limit_gb(original)

    def test_has_slider_and_presets(self, page):
        assert hasattr(page, "sld_mem")
        assert hasattr(page, "_preset_buttons")
        assert not hasattr(page, "spn_mem"), "数值框已移除"

    def test_slider_range_is_host_memory(self, page, qapp):
        """滑块值直接就是 GB，0 = 不限制"""
        page._rebuild_presets(16.0)
        assert page.sld_mem.minimum() == 0
        assert page.sld_mem.maximum() == 16
        assert page.sld_mem.value() == 0

    def test_presets_include_unlimited(self, page):
        page._rebuild_presets(16.0)
        assert page._preset_values[0] == 0, "第一档必须是「不限制」"

    def test_presets_do_not_exceed_host(self, page):
        page._rebuild_presets(8.0)
        for gb in page._preset_values:
            assert gb <= 8, f"档位 {gb}G 超过物理内存"

    def test_presets_capped_at_six(self, page):
        page._rebuild_presets(256.0)
        assert len(page._preset_values) <= 7, "档位太多会撑爆一行"

    def test_slider_updates_display(self, page, qapp):
        page._rebuild_presets(16.0)
        page.sld_mem.setValue(4)
        assert page.lbl_mem_value.text() == "4 GB"

    def test_slider_zero_means_unlimited(self, page, qapp):
        page._rebuild_presets(16.0)
        page.sld_mem.setValue(0)
        assert page.sld_mem.value() == 0
        assert page.lbl_mem_value.text() == "不限制"

    def test_preset_click_syncs_slider(self, page, qapp):
        page._rebuild_presets(16.0)
        page._select_preset(4)
        assert page.sld_mem.value() == 4


class TestMirrorTab:
    def test_autosave_no_save_button(self, page):
        """改完即生效，界面上不该再有「保存」按钮"""
        src = (ROOT / "ui" / "settings_page.py").read_text(encoding="utf-8")
        assert "def _autosave_mirrors" in src
        assert "def _save_mirror_config" not in src, "旧的保存按钮逻辑应已删除"

    def test_proxy_collapsed_by_default(self, page):
        assert page.box_proxy.isHidden() is True

    def test_proxy_toggle_expands(self, page, qapp):
        page.btn_mirror_adv.setChecked(True)
        qapp.processEvents()
        assert page.box_proxy.isHidden() is False

    def test_status_line_exists(self, page):
        assert hasattr(page, "lbl_mirror_status")
        assert "加速" in page.lbl_mirror_status.text() or \
               "直连" in page.lbl_mirror_status.text()


class TestStartupAutoStart:
    """应用启动时自动确保用户级服务在跑"""

    def test_main_ensures_podman(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        assert "_ensure_podman_ready" in src
        assert "ensure_user_service" in src

    def test_core_helpers_exist(self):
        from core import podman_config as cfg

        assert callable(cfg.podman_installed)
        assert callable(cfg.podman_version)
        assert callable(cfg.install_podman)
        assert callable(cfg.ensure_user_service)

    def test_ensure_user_service_idempotent_safe(self):
        """服务已在跑时调用不应报错"""
        from core import podman_config as cfg

        if not cfg.podman_installed():
            pytest.skip("未安装 podman")
        ok, msg = cfg.ensure_user_service()
        assert ok, msg

    def test_install_rejects_unknown_distro(self, monkeypatch):
        from core import podman_config as cfg
        from core.vm import deps

        monkeypatch.setattr(deps, "detect_distro", lambda: "unknown")
        ok, msg = cfg.install_podman()
        assert ok is False
        assert "手动安装" in msg


class TestAboutIcon:
    def test_uses_colored_app_icon(self):
        """关于页头部用彩色应用图标，不是单色线条 seal"""
        src = (ROOT / "ui" / "settings_page.py").read_text(encoding="utf-8")
        start = src.find("def _build_about_tab")
        body = src[start:start + 900]
        assert "app_icon()" in body
        assert 'icon("seal"' not in body


class TestVmRootSetting:
    """虚拟机目录可配置（存储页）"""

    def test_core_helpers(self):
        from core.vm import manager

        assert callable(manager.vm_root)
        assert callable(manager.machines_dir)
        assert callable(manager.set_vm_root)

    def test_default_when_unset(self):
        from core import state
        from core.vm import manager

        state.set_setting(manager.KEY_VM_ROOT, "")
        assert manager.vm_root() == manager.DEFAULT_VM_ROOT
        assert manager.machines_dir() == manager.DEFAULT_VM_ROOT / "machines"

    def test_rejects_relative_path(self):
        from core.vm import manager

        ok, msg = manager.set_vm_root("relative/dir", move=False)
        assert ok is False
        assert "绝对路径" in msg

    def test_rejects_same_dir(self, tmp_path, monkeypatch):
        from core import state
        from core.vm import manager

        state.set_setting(manager.KEY_VM_ROOT, str(tmp_path))
        try:
            ok, msg = manager.set_vm_root(str(tmp_path), move=False)
            assert ok is False
            assert "相同" in msg
        finally:
            state.set_setting(manager.KEY_VM_ROOT, "")

    def test_sets_and_persists(self, tmp_path, monkeypatch):
        from core import state
        from core.vm import manager

        # 本机可能有 VM 在跑，这里只验证写入/读取逻辑
        monkeypatch.setattr(manager, "load_all", lambda: [])

        target = tmp_path / "vms"
        try:
            ok, msg = manager.set_vm_root(str(target), move=False)
            assert ok, msg
            assert manager.vm_root() == target
            assert manager.machines_dir() == target / "machines"
            assert target.is_dir()
        finally:
            state.set_setting(manager.KEY_VM_ROOT, "")

    def test_storage_tab_has_vm_section(self, page):
        assert hasattr(page, "st_vm_root")
        assert hasattr(page, "ed_vm_root")
        assert hasattr(page, "lbl_vm_used")

    def test_vm_section_ordered_before_reset(self):
        """危险操作放最后，符合设置页惯例"""
        src = (ROOT / "ui" / "settings_page.py").read_text(encoding="utf-8")
        start = src.find("def _build_storage_tab")
        body = src[start:start + 6000]
        assert body.find('_section("虚拟机目录")') < body.find('_section("重置")')

    def test_refresh_shows_path(self, page, qapp):
        from core.vm import manager

        page._refresh_vm_root_info()
        assert page.st_vm_root.text() == str(manager.vm_root())


class TestMemoryControl:
    """内存上限控件：滑块 + 快捷档位（不设数值框）"""

    def test_has_slider(self, page):
        assert hasattr(page, "sld_mem")
        assert not hasattr(page, "spn_mem"), "数值框已移除"

    def test_slider_updates_big_value(self, page, qapp):
        page._rebuild_presets(16.0)
        page.sld_mem.setValue(8)
        assert page.lbl_mem_value.text() == "8 GB"

    def test_zero_shows_unlimited(self, page, qapp):
        page._rebuild_presets(16.0)
        page.sld_mem.setValue(0)
        assert page.lbl_mem_value.text() == "不限制"

    def test_preset_syncs_slider(self, page, qapp):
        page._rebuild_presets(16.0)
        page._select_preset(4)
        assert page.sld_mem.value() == 4
        assert page.lbl_mem_value.text() == "4 GB"

    def test_debounced_apply(self, page):
        """连续拖动只写一次配置"""
        assert hasattr(page, "_schedule_mem_apply")
        src = (ROOT / "ui" / "settings_page.py").read_text(encoding="utf-8")
        assert "setSingleShot(True)" in src
        assert "_mem_timer" in src


class TestPageMargins:
    """设置页内容要贴近 tab 栏和窗口边缘"""

    def test_page_margin_small(self):
        from ui import settings_page as sp

        assert sp.PAGE_MARGIN <= 10, sp.PAGE_MARGIN

    def test_outer_layout_margin_small(self):
        src = (ROOT / "ui" / "settings_page.py").read_text(encoding="utf-8")
        assert "layout.setContentsMargins(4, 4, 4, 4)" in src


class TestUsageRows:
    """占用列表：VM 只显示当前，容器显示当前/上限并标 compose 项目"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "charts.py").read_text(encoding="utf-8")

    def test_vm_shows_only_actual(self):
        src = self._src()
        # 定位「数值（条形下方小字）」那段
        start = src.find("虚拟机只显示当前占用")
        assert start > 0, "找不到 VM 数值分支"
        lines = src[start:start + 400].split("\n")
        # 注释下一行就是 VM 的取值语句，必须只有当前值
        vm_line = next(x for x in lines if "val =" in x)
        assert 'f"{actual:.2f} GB"' in vm_line, vm_line
        assert "limit" not in vm_line, f"VM 不应出现上限：{vm_line}"

    def test_vm_has_no_percentage(self):
        src = self._src()
        assert 'if not it.get("is_vm"):' in src, "VM 不该显示百分比"

    def test_vm_bar_relative_to_host(self):
        src = self._src()
        assert "actual / self._host_gb" in src

    def test_compose_project_in_name(self):
        src = self._src()
        assert 'it.get("project")' in src
        assert 'f"{name} · {it[\'project\']}"' in src

    def test_compute_usage_reports_project(self):
        from core import podman_config as cfg

        src = (ROOT / "core" / "podman_config.py").read_text(encoding="utf-8")
        assert "com.docker.compose.project" in src
        assert '"project"' in src


class TestComposeMemoryColumn:
    """Compose 服务表要看当前内存和配置上限"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "compose_page.py").read_text(encoding="utf-8")

    def test_table_has_memory_column(self):
        src = self._src()
        assert 'QTableWidget(0, 5)' in src
        assert '"内存"' in src

    def test_fills_current_and_limit(self):
        src = self._src()
        assert "compute_usage" in src
        assert "actual:.2f} / {limit:.2f} GB" in src


class TestUnifiedListCards:
    """所有右侧列表区统一成圆角白底卡片"""

    @staticmethod
    def _css() -> str:
        from ui.style import build_stylesheet

        return build_stylesheet()

    def test_list_card_has_radius(self, qapp):
        css = self._css()
        start = css.find("QFrame#listCard {")
        assert start > 0
        assert "border-radius: 8px" in css[start:start + 160]

    def test_empty_state_transparent_inside_card(self, qapp):
        """空状态在卡片内部，自己不该再画一层边框"""
        css = self._css()
        start = css.find("QWidget#emptyState {")
        block = css[start:start + 160]
        assert "background-color: transparent" in block
        assert "border-radius" not in block

    def test_header_has_column_dividers(self, qapp):
        css = self._css()
        start = css.find("QHeaderView::section {")
        assert "border-right: 1px solid" in css[start:start + 300]

    def test_header_corners_rounded(self, qapp):
        """表头首尾自己带圆角，否则会盖掉卡片的圆角"""
        css = self._css()
        assert "QFrame#listCard QHeaderView::section:first" in css
        assert "border-top-left-radius: 8px" in css

    def test_toolbar_has_no_divider(self, qapp):
        css = self._css()
        start = css.find("QToolBar {")
        assert "border-bottom" not in css[start:start + 200]


class TestListCards:
    """所有右侧列表区包在圆角白底卡片里

    不能只给 QTableView 加 border-radius：viewport 和表头都是方形
    子控件，会把圆角盖掉（表现为「边角缺一块、线没连上」）。
    """

    @staticmethod
    def _src(name: str) -> str:
        return (ROOT / "ui" / name).read_text(encoding="utf-8")

    def test_pages_use_list_card(self):
        expected = {
            "container_page.py": "self._table_stack",
            "resource_page.py": "self._table_stack",
            "vm_page.py": "self._table_stack",
            "vm_files.py": "self.table",
            "vm_detail.py": "self.tbl_shares",
        }
        for name, var in expected.items():
            src = self._src(name)
            assert f"make_list_card({var}" in src, f"{name} 的列表没包卡片"

        # Compose 的服务表外面是一张 setObjectName("listCard") 的卡片
        # （标题在卡片外，表格贴边），不是 make_list_card 包的
        compose = self._src("compose_page.py")
        assert 'grp2.setObjectName("listCard")' in compose
        assert "g2.addWidget(self.tbl_svc, 1)" in compose

    def test_card_style_exists(self, qapp):
        from ui.style import build_stylesheet

        css = build_stylesheet()
        assert "QFrame#listCard" in css
        start = css.find("QFrame#listCard {")
        assert "border-radius: 8px" in css[start:start + 160]

    def test_inner_views_transparent(self, qapp):
        """卡片内的表格/表头必须透明，否则会盖住圆角"""
        from ui.style import build_stylesheet

        css = build_stylesheet()
        start = css.find("QFrame#listCard QTableView")
        assert start > 0
        assert "background-color: transparent" in css[start:start + 300]

    def test_header_keeps_column_dividers(self, qapp):
        """列之间的竖分隔线要保留，否则一排表头糊成一片"""
        from ui.style import build_stylesheet

        css = build_stylesheet()
        start = css.find("QHeaderView::section {")
        assert "border-right: 1px solid" in css[start:start + 300]

    def test_list_card_wraps_content(self, qapp):
        from PyQt6.QtWidgets import QLabel

        from ui.widgets import ListCard, make_list_card

        lbl = QLabel("x")
        card = make_list_card(lbl)
        assert isinstance(card, ListCard)
        assert card.content is lbl


class TestUsageRowsHeight:
    """占用列表要按行数撑开，不能被裁"""

    def test_height_accounts_for_gap(self, qapp):
        from ui.charts import CARD_GAP, CARD_H, ContainerRows

        w = ContainerRows()
        for n in (1, 2, 3, 4, 5):
            items = [
                {"name": f"c{i}", "state": "running",
                 "limit_gb": 1, "actual_gb": 0.1}
                for i in range(n)
            ]
            w.set_items(items, 1, 1)
            rows = (n + 1) // 2
            assert w.minimumHeight() == rows * (CARD_H + CARD_GAP), (
                f"{n} 项时应为 {rows} 行的高度"
            )
        w.deleteLater()


class TestWindowGeometry:
    """默认窗口尺寸按屏幕算，并居中"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")

    def test_uses_available_geometry(self):
        src = self._src()
        assert "_apply_default_geometry" in src
        assert "availableGeometry" in src

    def test_clamps_to_screen(self):
        """小屏上不能超出可用区域"""
        src = self._src()
        start = src.find("def _apply_default_geometry")
        body = src[start:start + 1200]
        assert "avail.width() - 40" in body
        assert "avail.height() - 60" in body

    def test_uses_percentage_not_fixed(self):
        """窗口尺寸用屏幕百分比，且比例不超过 60%"""
        src = self._src()
        start = src.find("def _apply_default_geometry")
        body = src[start:start + 1200]
        assert "avail.width() * 0.50" in body
        assert "avail.height() * 0.56" in body
        # 不能有固定上限（有上限大屏上就变回固定尺寸）
        assert "min(w, 1400)" not in body

    def test_no_hardcoded_resize_in_init(self):
        """构造函数里不再写死 1200×760"""
        src = self._src()
        start = src.find("def __init__")
        body = src[start:start + 500]
        assert "self.resize(1200, 760)" not in body


class TestSidebarWidth:
    def test_width_fits_content(self):
        """宽度按最长标签算，不写死"""
        src = (ROOT / "ui" / "sidebar.py").read_text(encoding="utf-8")
        assert "_fit_width" in src
        assert "setFixedWidth(168)" not in src


class TestQuitFlow:
    """托盘退出必须真的能退出

    踩过的坑：快照失败后退化成 ACPI 关机，qemu 迟迟不退，
    app.quit() 永远等不到 → 用户看到「点退出没反应」。
    """

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "main_window.py").read_text(encoding="utf-8")

    def test_has_force_quit_timeout(self):
        src = self._src()
        assert "_force_quit" in src
        assert "EXIT_TIMEOUT_MS" in src

    def test_timeout_uses_self(self):
        """类属性必须带 self.，否则 NameError 会抛进事件循环导致 abort"""
        src = self._src()
        assert "QTimer.singleShot(self.EXIT_TIMEOUT_MS" in src
        assert "QTimer.singleShot(EXIT_TIMEOUT_MS" not in src

    def test_force_quit_ends_process(self):
        src = self._src()
        start = src.find("def _force_quit")
        body = src[start:start + 700]
        assert "os._exit" in body, "必须硬退出，否则线程卡住时退不掉"

    def test_hides_window_immediately(self):
        """点退出后窗口要立刻消失，不能让用户干等"""
        src = self._src()
        start = src.find("def _quit_from_tray")
        body = src[start:start + 900]
        assert "self.hide()" in body

    def test_soft_stop_uses_short_timeout(self):
        """退出场景不该等满 30 秒"""
        src = self._src()
        assert "soft_stop(vm, timeout=" in src


class TestComposeToolbar:
    """Compose 工具栏只留高频操作

    移除「保存 / 刷新 / 根目录 / 浏览」：
      · 保存 → YAML 改为自动保存
      · 刷新 → 列表本来就定时刷
      · 根目录 → 几乎不改，且可以在设置里改
    """

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "compose_page.py").read_text(encoding="utf-8")

    def test_removed_actions(self):
        src = self._src()
        for gone in ('"根目录:"', "act_browse = QAction", "act_save = QAction"):
            assert gone not in src, f"工具栏不该再有 {gone}"

    def test_keeps_core_actions(self):
        src = self._src()
        for keep in ("act_new", "act_up", "act_down",
                     "act_restart", "act_pull", "act_logs", "act_delete"):
            assert keep in src, f"工具栏丢了 {keep}"

    def test_yaml_autosaves(self):
        """没有保存按钮，必须有自动保存"""
        src = self._src()
        assert "_autosave_yaml" in src
        assert "focusOutEvent" in src or "_yaml_focus_out" in src

    def test_autosave_on_project_switch(self):
        src = self._src()
        start = src.find("def _on_tree_select")
        body = src[start:start + 300]
        assert "_autosave_yaml()" in body, "切换项目前必须保存"
