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
    p.deleteLater()


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
        assert hasattr(page, "lbl_mem_value")
        assert hasattr(page, "_preset_buttons")

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
        assert page.lbl_mem_value.text() == "不限制"

    def test_preset_click_syncs_slider(self, page, qapp):
        page._rebuild_presets(16.0)
        page._select_preset(4)
        assert page.sld_mem.value() == 4
        assert page.lbl_mem_value.text() == "4 GB"


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
