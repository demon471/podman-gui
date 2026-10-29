"""图标管理

跟随 KDE 主题：
- 优先使用 KDE 系统图标（breeze / breeze-dark 自动切换）
- 缺失时回退到内置 SVG，并按当前主题着色
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QPointF, Qt
from PyQt6.QtGui import QIcon, QIconEngine, QImage, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from core.kde_theme import is_dark_mode

# ─────────────────────────────────────────────
# 内置 SVG（currentColor 会被替换）
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# 图标设计规范（改动前务必先读）
#
# 一套图标要看起来协调，靠的是「同一套参数」，不是逐个画得像。
# 之前每个图标手写 stroke-width（1.3 / 1.6 / 1.9 / 2 / 2.4 混用），
# 还有几个是实心填充、其余是描边 —— 放在一起必然违和。
#
# 现在统一由下面的构造函数生成，规则只有一条：
#   · 24×24 网格，描边 1.75，圆头圆角，一律不填充
#   · 内容限制在 3–21 之间（留 3 单位呼吸空间）
#   · 全部描边，不出现任何实心块（实心夹在线条里很突兀）
#   · 例外只有极小的实心圆点（状态标记、pod 中心点）
#
# 新增图标请用 _svg/_p/_c/_r/_rect，不要手写 <path>，
# 否则线宽会再次漂移。
# ─────────────────────────────────────────────

_STROKE = 1.75
_ATTR = (
    'fill="none" stroke="currentColor" '
    f'stroke-width="{_STROKE}" '
    'stroke-linecap="round" stroke-linejoin="round"'
)
_SOLID_ATTR = (
    'fill="currentColor" stroke="currentColor" stroke-width="1.4" '
    'stroke-linecap="round" stroke-linejoin="round"'
)


def _svg(*parts: str) -> str:
    """包一层 <svg>（24×24 网格）"""
    return (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        + "".join(parts)
        + "</svg>"
    )


def _p(d: str) -> str:
    """描边路径"""
    return f'<path {_ATTR} d="{d}"/>'


def _pf(d: str) -> str:
    """实心路径（仅传送控制用）"""
    return f'<path {_SOLID_ATTR} d="{d}"/>'


def _c(cx: float, cy: float, r: float, solid: bool = False) -> str:
    """圆：solid=True 用实心（小圆点/状态标记）"""
    attr = 'fill="currentColor" stroke="none"' if solid else _ATTR
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" {attr}/>'


def _rect(x: float, y: float, w: float, h: float, rx: float = 2.0,
          solid: bool = False) -> str:
    attr = 'fill="currentColor" stroke="none"' if solid else _ATTR
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
        f'rx="{rx}" {attr}/>'
    )


def _e(cx: float, cy: float, rx: float, ry: float) -> str:
    """椭圆（圆柱顶面）"""
    return f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" {_ATTR}/>'


SVG = {
    # ══════════ 侧边导航 ══════════
    # 立方体：容器
    "container": _svg(
        _p("M12 3.4 20.3 7.9 12 12.4 3.7 7.9Z"),
        _p("M3.7 7.9v8.4L12 20.7v-8.3"),
        _p("M20.3 7.9v8.4L12 20.7"),
    ),
    # 三层叠片：镜像
    "image": _svg(
        _p("M12 3 20 7 12 11 4 7Z"),
        _p("M4 11.4 12 15.4 20 11.4"),
        _p("M4 15.8 12 19.8 20 15.8"),
    ),
    # 圆柱：卷
    "volume": _svg(
        _e(12, 6.4, 7.4, 2.8),
        _p("M4.6 6.4v11.4c0 1.55 3.31 2.8 7.4 2.8s7.4-1.25 7.4-2.8V6.4"),
        _p("M4.6 12.1c0 1.55 3.31 2.8 7.4 2.8s7.4-1.25 7.4-2.8"),
    ),
    # 三节点连线：网络
    "network": _svg(
        _c(12, 5.4, 2.5),
        _c(5.2, 18.2, 2.5),
        _c(18.8, 18.2, 2.5),
        _p("M10.7 7.6 6.5 15.9"),
        _p("M13.3 7.6 17.5 15.9"),
        _p("M7.7 18.2h8.6"),
    ),
    # 六边形：Pod（k8s 语义）
    "pod": _svg(
        _p("M12 2.9 19.9 7.45 19.9 16.55 12 21.1 4.1 16.55 4.1 7.45Z"),
        _c(12, 12, 1.6),
    ),
    # 四宫格 + 对勾：Compose
    "compose": _svg(
        _rect(3.2, 3.2, 7.4, 7.4, 1.6),
        _rect(13.4, 3.2, 7.4, 7.4, 1.6),
        _rect(3.2, 13.4, 7.4, 7.4, 1.6),
        _p("M13.4 17.6 16 20.2 20.8 15.4"),
    ),
    # 显示器：虚拟机
    "vm": _svg(
        _rect(2.8, 4.2, 18.4, 12.6, 2.2),
        _p("M12 16.8v3.8"),
        _p("M8.8 20.6h6.4"),
    ),
    # 双滑块：设置
    "settings": _svg(
        _p("M3.6 7.2h4.9"),
        _p("M13.5 7.2h6.9"),
        _c(11, 7.2, 2.3),
        _p("M3.6 16.8h7.1"),
        _p("M15.7 16.8h4.7"),
        _c(13.3, 16.8, 2.3),
    ),

    # ══════════ 应用/托盘标识 ══════════
    # 两层集装箱堆叠（托盘与菜单里用；主窗口用彩色 appicon）
    # 托盘/标识：等轴测箱体 + 托盘底（呼应应用图标，但只有
    # 4 条线 —— 小尺寸下线条一多就糊成一团）
    "seal": _svg(
        _p("M12 3.8 19.2 7.8 12 11.8 4.8 7.8Z"),
        _p("M4.8 7.8v6.6L12 18.4v-6.6"),
        _p("M19.2 7.8v6.6L12 18.4"),
        _p("M4.4 20.8h15.2"),
    ),
    "podman": _svg(
        _p("M12 3.8 19.2 7.8 12 11.8 4.8 7.8Z"),
        _p("M4.8 7.8v6.6L12 18.4v-6.6"),
        _p("M19.2 7.8v6.6L12 18.4"),
        _p("M4.4 20.8h15.2"),
    ),

    # ══════════ 传送控制 ══════════
    # 与整套图标一样用描边：实心块夹在一堆线条里很突兀。
    # 三角形用圆角描边保证小尺寸也不糊。
    "play": _svg(_p("M8.8 5.4 18.2 12 8.8 18.6Z")),
    "pause": _svg(
        _p("M9.4 5.6v12.8"),
        _p("M14.6 5.6v12.8"),
    ),
    "stop": _svg(_rect(6.8, 6.8, 10.4, 10.4, 2.2)),

    # ══════════ 动作 ══════════
    "terminal": _svg(
        _rect(2.8, 4.2, 18.4, 15.6, 2.2),
        _p("M7 9.6 10.2 12.4 7 15.2"),
        _p("M12.8 15.2h4.4"),
    ),
    "log": _svg(
        _p("M13.6 3.2H6.4a2 2 0 0 0-2 2v13.6a2 2 0 0 0 2 2h11.2"
           "a2 2 0 0 0 2-2V9.2Z"),
        _p("M13.6 3.2v6h6"),
        _p("M7.8 13h8.4"),
        _p("M7.8 16.6h5.6"),
    ),
    # 循环箭头 + 中心点：重启
    "restart": _svg(
        _p("M20.2 12a8.2 8.2 0 1 1-2.6-6"),
        _p("M20.8 3.8v5.4h-5.4"),
        _c(12, 12, 1.6, solid=True),
    ),
    # 循环箭头：刷新
    "refresh": _svg(
        _p("M20.2 12a8.2 8.2 0 1 1-2.6-6"),
        _p("M20.8 3.8v5.4h-5.4"),
    ),
    "delete": _svg(
        _p("M4.6 6.8h14.8"),
        _p("M9.4 6.8V4.6a1.4 1.4 0 0 1 1.4-1.4h2.4a1.4 1.4 0 0 1 "
           "1.4 1.4v2.2"),
        _p("M6.6 6.8l.9 12.2a1.8 1.8 0 0 0 1.8 1.7h5.4a1.8 1.8 0 0 0 "
           "1.8-1.7l.9-12.2"),
        _p("M10.4 10.8v6"),
        _p("M13.6 10.8v6"),
    ),
    "search": _svg(
        _c(10.8, 10.8, 6.4),
        _p("M15.6 15.6 20.6 20.6"),
    ),
    "add": _svg(
        _p("M12 5.2v13.6"),
        _p("M5.2 12h13.6"),
    ),
    # 清理：星星（区别于删除用的垃圾桶）
    "clean": _svg(
        _p("M11 3.4l2 4.9 4.9 2-4.9 2-2 4.9-2-4.9-4.9-2 4.9-2Z"),
        _p("M17.6 14.2l1 2.4 2.4 1-2.4 1-1 2.4-1-2.4-2.4-1 2.4-1Z"),
    ),
    "info": _svg(
        _c(12, 12, 8.6),
        _p("M12 11.2v5.4"),
        _c(12, 7.9, 1.05, solid=True),
    ),
    "edit": _svg(
        _p("M16.4 3.6a2.3 2.3 0 0 1 3.2 3.2L8.2 18.2l-4.6 1.4 1.4-4.6Z"),
        _p("M14.6 5.4 18 8.8"),
    ),
    "save": _svg(
        _p("M5 3.8h10.6l4.6 4.6v11.8a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4.8"
           "a1 1 0 0 1 1-1Z"),
        _p("M8 3.8v5.4h6.6V3.8"),
        _p("M7.4 20.2v-6.4h9.2v6.4"),
    ),
    "folder": _svg(
        _p("M3.6 7.4a1.8 1.8 0 0 1 1.8-1.8h3.6l2.2 2.6h7.4a1.8 1.8 0 0 1 "
           "1.8 1.8v8.6a1.8 1.8 0 0 1-1.8 1.8H5.4a1.8 1.8 0 0 1-1.8-1.8Z"),
    ),
    "file": _svg(
        _p("M13.4 3.4H7a1.8 1.8 0 0 0-1.8 1.8v13.6A1.8 1.8 0 0 0 7 20.6"
           "h10a1.8 1.8 0 0 0 1.8-1.8V9Z"),
        _p("M13.4 3.4V9h5.4"),
    ),
    "up": _svg(_p("M6.4 14.4 12 8.8l5.6 5.6")),
    "down": _svg(_p("M6.4 9.6 12 15.2l5.6-5.6")),
    "upload": _svg(
        _p("M12 15.6V4.4"),
        _p("M7.8 8.6 12 4.4l4.2 4.2"),
        _p("M4.6 15.4v3.2a1.8 1.8 0 0 0 1.8 1.8h11.2a1.8 1.8 0 0 0 "
           "1.8-1.8v-3.2"),
    ),
    "download": _svg(
        _p("M12 4.4v11.2"),
        _p("M7.8 11.4 12 15.6l4.2-4.2"),
        _p("M4.6 15.4v3.2a1.8 1.8 0 0 0 1.8 1.8h11.2a1.8 1.8 0 0 0 "
           "1.8-1.8v-3.2"),
    ),
    # 扳手：构建
    "build": _svg(
        _p("M4.6 19.4 12.4 11.6"),
        _p("M10.6 6.2l7.2 7.2 3.4-3.4-7.2-7.2Z"),
    ),
    "home": _svg(
        _p("M3.8 10.4 12 3.6l8.2 6.8"),
        _p("M5.8 9v10a1 1 0 0 0 1 1h10.4a1 1 0 0 0 1-1V9"),
        _p("M9.8 20v-5.4h4.4V20"),
    ),
    "more": _svg(
        _c(5.4, 12, 1.5, solid=True),
        _c(12, 12, 1.5, solid=True),
        _c(18.6, 12, 1.5, solid=True),
    ),
    "check": _svg(_p("M4.8 12.6 9.6 17.4 19.2 6.6")),
    "copy": _svg(
        _rect(8.6, 8.6, 11.8, 11.8, 2.0),
        _p("M15.4 5.6V4.8a1.8 1.8 0 0 0-1.8-1.8H4.8a1.8 1.8 0 0 0-1.8 "
           "1.8v8.8a1.8 1.8 0 0 0 1.8 1.8h.8"),
    ),
    "eye": _svg(
        _p("M2.6 12S6.1 5.6 12 5.6 21.4 12 21.4 12 17.9 18.4 12 18.4 "
           "2.6 12 2.6 12Z"),
        _c(12, 12, 3.2),
    ),
    "eye_off": _svg(
        _p("M2.6 12S6.1 5.6 12 5.6c1.6 0 3 .5 4.2 1.2"),
        _p("M21.4 12S17.9 18.4 12 18.4c-1.6 0-3-.5-4.2-1.2"),
        _p("M4.2 19.8 19.8 4.2"),
    ),
}

# KDE 系统图标映射（优先使用）
KDE_ICON_MAP = {
    "podman": "utilities-terminal",
    "seal": "applications-system",
    "container": "application-x-executable",
    "vm": "computer",
    "image": "application-x-executable",
    "volume": "drive-harddisk",
    "network": "network-workgroup",
    "pod": "folder",
    "terminal": "utilities-terminal",
    "log": "text-x-generic",
    "play": "media-playback-start",
    "stop": "media-playback-stop",
    "pause": "media-playback-pause",
    "restart": "view-refresh",
    "delete": "edit-delete",
    "refresh": "view-refresh",
    "search": "edit-find",
    "add": "list-add",
    "clean": "edit-clear",
    "settings": "settings-configure",
    "info": "dialog-information",
    "build": "applications-development",
    "compose": "document-multiple",
    "edit": "document-edit",
    "save": "document-save",
    "folder": "folder",
    "up": "go-up",
    "down": "go-down",
    "restart": "view-refresh",
    # 文件浏览
    "file": "text-x-generic",
    "upload": "go-up",
    "download": "go-down",
    "home": "go-home",
    "copy": "edit-copy",
    "check": "dialog-ok-apply",
    "more": "overflow-menu",
}

# 主题色
# ─────────────────────────────────────────
# 图标尺寸规范（全应用统一，勿随意偏离）
# ─────────────────────────────────────────
from PyQt6.QtCore import QSize

ICON_SIZE_SIDEBAR = QSize(20, 20)    # 侧边导航
ICON_SIZE_TOOLBAR = QSize(16, 16)    # 工具栏按钮
ICON_SIZE_BUTTON = QSize(16, 16)     # 普通按钮
ICON_SIZE_MENU = QSize(16, 16)       # 菜单项
ICON_SIZE_TAB = QSize(16, 16)        # 标签页
ICON_SIZE_DIALOG = QSize(48, 48)     # 对话框标题

# 内部渲染尺寸（比显示尺寸略大，保证高 DPI 清晰）
_RENDER_SCALE = 2

DANGER_FG = "#e74c3c"  # 危险操作（删除）固定红色
DARK_FG = "#232629"    # 亮色主题下的深色前景
LIGHT_FG = "#eff0f1"   # 暗色主题下的浅色前景


def _fg_color() -> str:
    """当前主题下的图标前景色"""
    return LIGHT_FG if is_dark_mode() else DARK_FG


# ─────────────────────────────────────────
# 光学尺寸统一
#
# 问题：各 SVG 的内容在 24x24 viewBox 里占比不同（stop 44%，network 94%），
# 直接渲染会导致视觉大小相差一倍多。
# 方案：测量内容边界，统一缩放到画布的固定占比，并居中。
# ─────────────────────────────────────────

# 内容占画布的目标比例（视觉一致性基准）
OPTICAL_FILL = 0.78
# 过度放大保护（避免极小图标被拉爆）
OPTICAL_MAX_SCALE = 1.75
# 测量用的探测尺寸
_PROBE = 64

# 缓存：svg 内容 hash → (x, y, w, h) 或 None
_BBOX_CACHE: dict[int, tuple[int, int, int, int] | None] = {}


def _alpha_bbox(img: QImage) -> tuple[int, int, int, int] | None:
    """测量 alpha 通道的内容边界，返回 (x, y, w, h)"""
    w, h = img.width(), img.height()
    if img.format() != QImage.Format.Format_ARGB32:
        img = img.convertToFormat(QImage.Format.Format_ARGB32)

    # 一次性取出整块像素数据（ARGB32，内存序 BGRA）
    ptr = img.bits()
    ptr.setsize(img.sizeInBytes())
    raw = bytes(ptr)
    stride = img.bytesPerLine()

    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        base = y * stride
        for x in range(w):
            # alpha 在每像素第 4 字节
            if raw[base + x * 4 + 3] > 10:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y

    if maxx < 0:
        return None
    return minx, miny, maxx - minx + 1, maxy - miny + 1


def _measure(svg_content: str, renderer: QSvgRenderer) -> tuple[int, int, int, int] | None:
    """测量 SVG 内容边界（按内容缓存，与颜色/尺寸无关）"""
    key = hash(svg_content)
    if key in _BBOX_CACHE:
        return _BBOX_CACHE[key]

    probe = QPixmap(_PROBE, _PROBE)
    probe.fill(Qt.GlobalColor.transparent)
    painter = QPainter(probe)
    renderer.render(painter)
    painter.end()

    bbox = _alpha_bbox(probe.toImage())
    _BBOX_CACHE[key] = bbox
    return bbox


def _render_svg(svg_content: str, color: str, size: int, optical: bool = True) -> QPixmap:
    """把 SVG 渲染成指定颜色和尺寸的 QPixmap

    optical=True 时统一内容视觉占比（推荐），
    optical=False 时按原始 viewBox 直接缩放。
    """
    colored = svg_content.replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(colored.encode()))

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    if optical:
        bbox = _measure(svg_content, renderer)
        if bbox:
            bx, by, bw, bh = bbox
            ratio = max(bw, bh) / _PROBE
            if ratio > 0.01:
                scale = min(OPTICAL_FILL / ratio, OPTICAL_MAX_SCALE)

                # 内容中心相对探测画布中心的偏移，换算到目标尺寸
                k = size / _PROBE
                off_x = (bx + bw / 2 - _PROBE / 2) * k * scale
                off_y = (by + bh / 2 - _PROBE / 2) * k * scale

                painter.translate(size / 2 - off_x, size / 2 - off_y)
                painter.scale(scale, scale)
                painter.translate(-size / 2, -size / 2)

    renderer.render(painter)
    painter.end()
    return pix


def app_icon() -> QIcon:
    """应用图标（彩色，多尺寸）

    托盘用它而不是内置单色 seal：托盘是 16–24px 的小尺寸，
    彩色实心图标在任务栏里辨识度远高于描边线条。
    """
    from pathlib import Path

    from PyQt6.QtCore import QSize

    ic = QIcon()
    base = Path(__file__).resolve().parent / "appicon"
    for s in (16, 22, 24, 32, 36, 42, 48, 64, 128, 256):
        f = base / f"podman-gui-{s}.png"
        if f.exists():
            ic.addFile(str(f), QSize(s, s))
    return ic


def icon(name: str, size: int = 16, danger: bool = False,
         color: str | None = None) -> QIcon:
    """获取图标（颜色随主题动态变化）

    策略：
    1. 内置 SVG → 用 QIconEngine 动态渲染，主题切换时自动变色
    2. 若 KDE 有对应语义图标且内置 SVG 缺失，用 KDE 图标

    参数：
      · danger=True  固定红色（危险操作）
      · color="#..." 固定指定颜色（如成功态的绿色对勾），
        此时不再跟随主题

    注意：size 是逻辑尺寸，实际按 2x 渲染保证高 DPI 清晰。
    请优先使用 ICON_SIZE_* 常量，勿硬编码数字。
    """
    svg_content = SVG.get(name)
    if svg_content:
        return QIcon(_ThemedIconEngine(svg_content, size, danger, color))

    kde_name = KDE_ICON_MAP.get(name, name)
    return QIcon.fromTheme(kde_name)


class _ThemedIconEngine(QIconEngine):
    """动态图标引擎

    绘制时检测前景色是否变化，变化则重新渲染，
    因此主题切换后 Qt 重绘时自动使用新颜色。
    """

    def __init__(self, svg_content: str, size: int, danger: bool = False,
                 color: str | None = None):
        super().__init__()
        self._svg = svg_content
        self._size = size
        self._danger = danger
        self._fixed_color = color
        self._cached: QPixmap | None = None
        self._cached_color: str = ""
        self._cached_size: int = 0

    def _pixmap(self, size: int) -> QPixmap:
        # color 优先（固定色），其次 danger（固定红），最后跟随主题
        if self._fixed_color:
            color = self._fixed_color
        elif self._danger:
            color = DANGER_FG
        else:
            color = _fg_color()
        if (
            self._cached is not None
            and self._cached_color == color
            and self._cached_size == size
        ):
            return self._cached

        pix = _render_svg(self._svg, color, size)
        self._cached = pix
        self._cached_color = color
        self._cached_size = size
        return pix

    def paint(self, painter, rect, mode, state) -> None:
        # 按实际请求尺寸渲染（高 DPI 下 rect 会更大）
        target = max(1, int(min(rect.width(), rect.height())))
        pix = self._pixmap(target)
        painter.drawPixmap(rect, pix, pix.rect())

    def pixmap(self, size, mode, state) -> QPixmap:
        return self._pixmap(max(1, int(size.width())))

    def clone(self):
        return _ThemedIconEngine(self._svg, self._size, self._danger)


def icon_pair(name: str, size: int = 16) -> tuple[QIcon, QIcon]:
    """返回 (亮色主题图标, 暗色主题图标)"""
    svg_content = SVG.get(name)
    if not svg_content:
        return QIcon.fromTheme(name), QIcon.fromTheme(name)
    return (
        QIcon(_render_svg(svg_content, DARK_FG, size)),
        QIcon(_render_svg(svg_content, LIGHT_FG, size)),
    )


# ─────────────────────────────────────────
# 容器状态图标（带颜色语义）
# ─────────────────────────────────────────
STATE_COLORS = {
    "running": "#27ae60",     # 绿：运行中
    "paused": "#f39c12",      # 橙：已暂停
    "restarting": "#3498db",  # 蓝：重启中
    "created": "#3498db",     # 蓝：已创建
    "exited": "#95a5a6",      # 灰：已停止
    "stopped": "#95a5a6",
    "dead": "#e74c3c",        # 红：失效
    "configured": "#95a5a6",
}

# 状态 → 图标名
STATE_ICONS = {
    "running": "state_running",
    "paused": "state_paused",
    "restarting": "state_restarting",
    "created": "state_created",
    "exited": "state_stopped",
    "stopped": "state_stopped",
    "dead": "state_error",
    "configured": "state_stopped",
}


def state_icon(state: str, size: int = 16, on_selected: bool = False) -> QIcon:
    """生成带语义色的状态图标

    形状区分状态（不只靠颜色，色盲友好）：
      running    → 实心圆 + 光环
      paused     → 双竖条
      restarting → 循环箭头
      created    → 圆环
      exited     → 实心方块
      dead       → 叉号

    on_selected=True 时改用高对比配色（用于选中行）。
    """
    from PyQt6.QtCore import QRectF
    from PyQt6.QtGui import QColor, QPainter, QPen

    key = state.lower()

    if on_selected:
        # 选中行：统一用高亮前景色，靠形状区分
        base = QColor(_selected_fg())
        fill = base
        # 已停止用半透明，其他用实心
        if key in ("exited", "stopped", "configured", "created"):
            fill = QColor(base)
            fill.setAlpha(150)
        outline = base
    else:
        base = QColor(STATE_COLORS.get(key, "#95a5a6"))
        fill = base
        outline = base

    px = size * _RENDER_SCALE
    pix = QPixmap(px, px)
    pix.fill(Qt.GlobalColor.transparent)

    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    d = float(px)
    cx = cy = d / 2
    stroke = max(2.2, d * 0.10)

    # 加一圈半透明对比描边：深色/浅色背景上都可见
    if not on_selected:
        contrast = QColor("#ffffff")
        contrast.setAlpha(170)
        halo_pen = QPen(contrast)
        halo_pen.setWidthF(stroke * 1.8)
        halo_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        halo_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

    pen = QPen(outline)
    pen.setWidthF(stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

    # ── 运行中：实心圆 + 光环 ──
    if key == "running":
        # 白色底衬（提升任意背景下的对比度）
        if not on_selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 200))
            p.drawEllipse(QRectF(d * 0.18, d * 0.18, d * 0.64, d * 0.64))

        halo = QColor(fill)
        halo.setAlpha(70 if not on_selected else 100)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(QRectF(d * 0.02, d * 0.02, d * 0.96, d * 0.96))

        p.setBrush(fill)
        p.drawEllipse(QRectF(d * 0.22, d * 0.22, d * 0.56, d * 0.56))

    # ── 已暂停：双竖条（实心）──
    elif key == "paused":
        w = d * 0.20
        h = d * 0.56
        y = cy - h / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(cx - w * 1.6, y, w, h), w * 0.35, w * 0.35)
        p.drawRoundedRect(QRectF(cx + w * 0.6, y, w, h), w * 0.35, w * 0.35)

    # ── 重启中：循环箭头 ──
    elif key == "restarting":
        r = d * 0.31
        rect = QRectF(cx - r, cy - r, r * 2, r * 2)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(rect, 50 * 16, 270 * 16)

        # 箭头三角
        ax, ay = cx + r * 0.62, cy - r * 0.78
        p.setBrush(fill)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(
            QPointF(ax - d * 0.03, ay - d * 0.15),
            QPointF(ax + d * 0.14, ay + d * 0.02),
            QPointF(ax - d * 0.07, ay + d * 0.10),
        )

    # ── 已创建：圆环（粗描边）──
    elif key == "created":
        if not on_selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 210))
            p.drawEllipse(QRectF(d * 0.20, d * 0.20, d * 0.60, d * 0.60))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QRectF(d * 0.24, d * 0.24, d * 0.52, d * 0.52))

    # ── 已停止：实心圆角方块 ──
    elif key in ("exited", "stopped", "configured"):
        if not on_selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 210))
            p.drawRoundedRect(
                QRectF(d * 0.21, d * 0.21, d * 0.58, d * 0.58),
                d * 0.13, d * 0.13,
            )
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawRoundedRect(
            QRectF(d * 0.25, d * 0.25, d * 0.50, d * 0.50),
            d * 0.11, d * 0.11,
        )

    # ── 失效：叉号 ──
    elif key == "dead":
        m = d * 0.27
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(m, m), QPointF(d - m, d - m))
        p.drawLine(QPointF(d - m, m), QPointF(m, d - m))

    # ── 其他：小实心圆 ──
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawEllipse(QRectF(d * 0.28, d * 0.28, d * 0.44, d * 0.44))

    p.end()
    return QIcon(pix)


def _selected_fg() -> str:
    """选中行前景色（与 HighlightedText 一致）"""
    from PyQt6.QtGui import QPalette
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return "#ffffff"
    return app.palette().color(
        QPalette.ColorRole.HighlightedText
    ).name()


