"""图标体系一致性

一套图标看起来协调，靠的是「同一套参数」，不是逐个画得像。
之前每个图标手写 stroke-width（1.3/1.6/1.9/2/2.4 混用），
还混着实心填充，放在一起必然违和。这些测试把规则钉死，
以后改图标不会悄悄跑偏。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 允许的线宽：1.75 是标准描边，1.4 只用于实心图形的圆角描边
ALLOWED_STROKE = {"1.75", "1.4"}

# 整套图标一律描边，不允许出现实心块
SOLID_ALLOWED: set[str] = set()


@pytest.fixture(scope="module")
def svg():
    from resources.icons import SVG

    return SVG


class TestStrokeConsistency:
    def test_only_standard_widths(self, svg):
        widths = set()
        for body in svg.values():
            widths |= set(re.findall(r'stroke-width="([\d.]+)"', body))
        assert widths <= ALLOWED_STROKE, f"出现了非标准线宽: {widths}"

    def test_every_icon_has_viewbox_24(self, svg):
        for name, body in svg.items():
            assert 'viewBox="0 0 24 24"' in body, f"{name} 网格不是 24×24"

    def test_round_caps_and_joins(self, svg):
        """圆头圆角是这套图标的手感来源"""
        for name, body in svg.items():
            if 'stroke-width' not in body:
                continue
            assert 'stroke-linecap="round"' in body, f"{name} 缺 linecap"
            assert 'stroke-linejoin="round"' in body, f"{name} 缺 linejoin"

    def test_fills_only_for_transport(self, svg):
        """除了播放/暂停/停止，其余一律描边不填充"""
        for name, body in svg.items():
            if name in SOLID_ALLOWED:
                continue
            # 允许极小的实心点（状态标记 / 图标内圆点）
            solid = re.findall(
                r'<(?:path|rect|circle|ellipse)[^>]*fill="currentColor"', body
            )
            for tag in solid:
                assert 'r="1' in tag or 'r="1.' in tag or 'circle' in tag, (
                    f"{name} 用了实心填充但不是小圆点: {tag[:60]}"
                )

    def test_no_handwritten_paths(self):
        """新增图标必须用 _svg/_p/_c/_rect 构造，不能手写 <path>"""
        src = (ROOT / "resources" / "icons.py").read_text(encoding="utf-8")
        start = src.find("SVG = {")
        end = src.find("\n}\n", start)
        body = src[start:end]
        # 数据块里不应该直接出现带 stroke-width 的原始 path
        assert 'stroke-width="' not in body.replace("_ATTR", ""), (
            "图标数据块里不应手写线宽，请用构造函数"
        )


class TestCoverage:
    def test_all_referenced_icons_exist(self, svg):
        """代码里 icon("x") 用到的名字必须能解析出来"""
        from resources.icons import KDE_ICON_MAP

        used = set()
        for f in ROOT.rglob("*.py"):
            s = str(f)
            if ".build-venv" in s or "/tests/" in s:
                continue
            txt = f.read_text(encoding="utf-8")
            used |= set(
                re.findall(r'(?<![a-z_])icon\(\s*"([a-z0-9_]+)"', txt)
            )
        missing = sorted(n for n in used if n not in svg and n not in KDE_ICON_MAP)
        assert not missing, f"这些图标没有定义: {missing}"

    def test_sidebar_icons_present(self, svg):
        for name in ("container", "image", "volume", "network",
                     "pod", "compose", "vm", "settings"):
            assert name in svg, f"侧边栏图标 {name} 缺失"

    def test_sidebar_icons_are_outline(self, svg):
        """侧边栏全部描边 —— 混进实心块就会显得不协调"""
        for name in ("container", "image", "volume", "network",
                     "pod", "compose", "vm", "settings"):
            body = svg[name]
            # 允许极小的实心圆点（如 pod 中心点），其余不能填充
            stripped = re.sub(
                r'<circle[^>]*fill="currentColor"[^>]*/>', "", body
            )
            assert 'fill="currentColor"' not in stripped, (
                f"{name} 含实心填充，应与其余图标统一为描边"
            )


class TestRendering:
    def test_all_icons_render_non_empty(self, qapp):
        """每个图标都要真的画出东西（空白说明 path 写错了）"""
        from resources.icons import SVG, icon

        for name in SVG:
            pm = icon(name, 20).pixmap(20, 20)
            assert not pm.isNull(), f"{name} 渲染失败"
            img = pm.toImage()
            ink = sum(
                1
                for y in range(img.height())
                for x in range(img.width())
                if img.pixelColor(x, y).alpha() > 10
            )
            assert ink > 8, f"{name} 几乎是空白（ink={ink}）"

    def test_icon_size_respected(self, qapp):
        from resources.icons import icon

        for size in (16, 20, 24, 48):
            pm = icon("container", size).pixmap(size, size)
            assert pm.width() == size and pm.height() == size


class TestTrayIcon:
    def test_app_icon_has_multiple_sizes(self, qapp):
        """托盘要用多尺寸彩色图标，Qt 才会挑对分辨率"""
        from resources.icons import app_icon

        ic = app_icon()
        assert not ic.isNull()
        sizes = {s.width() for s in ic.availableSizes()}
        assert len(sizes) >= 5, f"尺寸太少: {sizes}"
        assert 22 in sizes or 24 in sizes, "缺少托盘常用尺寸"

    def test_tray_uses_line_art_icon(self):
        """托盘用线条风图标，且必须是简化过的（小尺寸不糊）"""
        src = (ROOT / "ui" / "tray.py").read_text(encoding="utf-8")
        assert 'icon("seal"' in src, "托盘应使用线条风 seal 图标"

    def test_seal_is_simplified(self, svg):
        """托盘图标线条数必须少 —— 多了在 16px 下糊成一团"""
        body = svg["seal"]
        strokes = body.count("<path") + body.count("<rect") + body.count(
            "<circle"
        )
        assert strokes <= 5, f"seal 有 {strokes} 条线，小尺寸会糊"

    def test_seal_renders_clearly_at_16px(self, qapp):
        """16px 下必须画得出足够墨量（不能细到看不见）"""
        from resources.icons import icon

        pm = icon("seal", 16).pixmap(16, 16)
        img = pm.toImage()
        ink = sum(
            1
            for y in range(16)
            for x in range(16)
            if img.pixelColor(x, y).alpha() > 40
        )
        assert ink > 30, f"16px 下墨量太少（ink={ink}），会糊或看不见"
