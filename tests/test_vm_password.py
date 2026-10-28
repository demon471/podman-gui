"""VM 登录密码的持久化与展示

用户改过密码后，界面上所有显示密码的地方都必须跟着变，
否则显示的还是镜像默认值，照着输必然失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestPasswordStorage:
    """core/vm/manager.py 的 get_password / set_password"""

    def test_default_when_unset(self):
        from core.vm import manager
        from core.vm.cloudinit import DEFAULT_PASSWORD

        assert manager.get_password("__nonexistent_vm__") == DEFAULT_PASSWORD

    def test_roundtrip(self):
        from core.vm import manager

        name = "__pwd_test__"
        try:
            manager.set_password(name, "newpass123")
            assert manager.get_password(name) == "newpass123"
        finally:
            manager.set_password(name, "")

    def test_empty_falls_back_to_default(self):
        from core.vm import manager
        from core.vm.cloudinit import DEFAULT_PASSWORD

        name = "__pwd_test__"
        manager.set_password(name, "")
        assert manager.get_password(name) == DEFAULT_PASSWORD

    def test_two_vms_are_independent(self):
        from core.vm import manager

        try:
            manager.set_password("__pwd_a__", "aaa")
            manager.set_password("__pwd_b__", "bbb")
            assert manager.get_password("__pwd_a__") == "aaa"
            assert manager.get_password("__pwd_b__") == "bbb"
        finally:
            manager.set_password("__pwd_a__", "")
            manager.set_password("__pwd_b__", "")


class TestInfocardShowsPassword:
    """悬停卡片展示实际密码"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "vm_infocard.py").read_text(encoding="utf-8")

    def test_label_renamed_to_password(self):
        """「初始密码」→「密码」：改过之后就不再是初始值了"""
        src = self._src()
        assert '"初始密码"' not in src
        assert '"密码"' in src

    def test_uses_actual_password(self):
        src = self._src()
        assert "get_password" in src, "必须读实际密码，不能用常量"
        assert "DEFAULT_PASSWORD" not in src, "不能再直接引用默认密码"

    def test_card_shows_new_password(self, qapp):
        from core.vm import manager
        from core.vm.cloudinit import DEFAULT_PASSWORD
        from ui.vm_infocard import VMInfoCard

        vms = manager.load_all()
        if not vms:
            pytest.skip("没有虚拟机")
        vm = vms[0]
        name = vm.spec.name

        card = VMInfoCard()
        try:
            card.show_for(vm, True)
            assert card._rows[2][1].text() == manager.get_password(name)

            manager.set_password(name, "changed999")
            card.show_for(vm, True)
            assert card._rows[2][1].text() == "changed999"
        finally:
            manager.set_password(name, DEFAULT_PASSWORD)
            card.deleteLater()


class TestChangePasswordSaves:
    """改密码成功后要落库"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "vm_detail.py").read_text(encoding="utf-8")

    def test_success_path_persists(self):
        src = self._src()
        start = src.find("def _change_password")
        end = src.find("def _build_shares", start)
        body = src[start:end] if end > start else src[start:]
        assert "set_password" in body, "成功后必须记录新密码"

    def test_files_sudo_default_uses_actual(self):
        src = (ROOT / "ui" / "vm_files.py").read_text(encoding="utf-8")
        assert "get_password" in src

    def test_sudo_candidates_include_actual(self):
        src = (ROOT / "core" / "vm" / "sudo.py").read_text(encoding="utf-8")
        assert "get_password" in src
