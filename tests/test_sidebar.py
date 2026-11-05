"""左侧导航栏宽度

踩过的坑：
  · 写死宽度 → 换字体/改缩放就被截断（「Compose」变「Comp...」）
  · 只量文字宽度 → 漏掉 QSS 的 padding/margin，照样截断
  · QEvent.Type.ScreenChangeInternal 是 Qt 私有枚举，PyQt6 里不存在，
    写上去会 AttributeError → 异常抛进事件处理 → Qt fatal → 进程 abort
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sidebar(qapp):
    from ui.sidebar import Sidebar

    sb = Sidebar()
    sb.show()
    for _ in range(6):
        qapp.processEvents()
    yield sb
    sb.deleteLater()
    from PyQt6.QtCore import QEvent

    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class TestWidthFitsContent:
    def test_compose_not_truncated(self, sidebar):
        """最长的「Compose」必须完整显示"""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QFontMetrics

        fm = QFontMetrics(sidebar.list.font())
        need = fm.horizontalAdvance("Compose")
        # 宽度至少要能容纳 图标 + 文字 + QSS padding/margin
        assert sidebar.width() >= need + 40, (
            f"侧边栏 {sidebar.width()}px 放不下 Compose（文字 {need}px）"
        )

    def test_not_excessively_wide(self, sidebar):
        """也不能太宽，否则右边一大片空白"""
        assert sidebar.width() <= 220

    def test_width_reacts_to_font(self, qapp):
        """字体变大后宽度要跟着变（等价于缩放率改变）"""
        from PyQt6.QtGui import QFont

        from ui.sidebar import Sidebar

        sb = Sidebar()
        sb.show()
        for _ in range(6):
            qapp.processEvents()
        base = sb.width()

        f = QFont(sb.list.font())
        f.setPointSize(f.pointSize() + 5)
        sb.list.setFont(f)
        sb._fit_width()
        for _ in range(4):
            qapp.processEvents()

        assert sb.width() > base, "字体变大后侧边栏应更宽"
        sb.deleteLater()


class TestNoPrivateEnums:
    def test_no_screen_change_internal(self):
        """PyQt6 没有这个枚举，用了就会 abort（注释里说明不算）"""
        src = (ROOT / "ui" / "sidebar.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            code = line.split("#", 1)[0]
            assert "ScreenChangeInternal" not in code, line

    def test_change_event_guarded(self):
        """构造期与递归都要挡掉，否则改几何会无限触发"""
        src = (ROOT / "ui" / "sidebar.py").read_text(encoding="utf-8")
        start = src.find("def _fit_width")
        body = src[start:start + 900]
        assert 'hasattr(self, "list")' in body
        assert "_fitting" in body

    def test_no_set_fixed_width_hardcoded(self):
        src = (ROOT / "ui" / "sidebar.py").read_text(encoding="utf-8")
        assert "setFixedWidth(168)" not in src
        assert "setFixedWidth(184)" not in src


class TestNoCrashOnStyleChange:
    def test_restyle_does_not_crash(self, qapp):
        """重新应用样式表（等价于缩放变化）不能崩

        这个用例给 qapp 设了全局样式表 —— 必须还原，否则会污染
        后续用例：其它界面在「有/无全局样式表」两种状态下渲染结果
        不同，导致依赖背景色比较的用例（比如 test_vm_page_ux 里
        比对两个界面内容区背景）莫名其妙挂掉。
        """
        from ui.style import build_stylesheet
        from ui.sidebar import Sidebar

        saved = qapp.styleSheet()
        sb = Sidebar()
        sb.show()
        try:
            for _ in range(6):
                qapp.processEvents()
            for _ in range(3):
                qapp.setStyleSheet(build_stylesheet())
                for _ in range(4):
                    qapp.processEvents()
            assert sb.width() > 0
        finally:
            sb.hide()
            sb.deleteLater()
            qapp.setStyleSheet(saved)
            qapp.processEvents()
