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

SVG = {
    "seal": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 644 644" width="100%" height="100%" fill="none"><circle cx="322" cy="322" r="315" stroke="currentColor" stroke-width="48" /><path d="M 122 418 C 145 365, 148 310, 154 255 C 165 165, 232 109, 322 109 C 412 109, 479 165, 490 255 C 496 310, 499 365, 525 422" stroke="currentColor" stroke-width="54" stroke-linecap="round" stroke-linejoin="round" /><circle cx="241.6" cy="252" r="34" fill="currentColor" /><circle cx="401.4" cy="252" r="34" fill="currentColor" /><path d="M 322 277 C 302 277, 293 283, 293 292 C 293 302, 310 314, 322 314 C 334 314, 351 302, 351 292 C 351 283, 342 277, 322 277 Z" fill="currentColor" /><path d="M 249 321 C 251 344, 268 351, 285 351 C 304 351, 319 337, 322 313 C 325 337, 340 351, 359 351 C 376 351, 393 344, 395 321" stroke="currentColor" stroke-width="50" stroke-linecap="round" stroke-linejoin="round" /><path d="M 85 430 C 138 405, 195 401, 250 413 C 295 424, 338 446, 385 459 C 435 471, 492 470, 540 459" stroke="currentColor" stroke-width="54" stroke-linecap="round" /><path d="M 390 424 C 445 438, 508 434, 564 413" stroke="currentColor" stroke-width="54" stroke-linecap="round" /><path d="M 113 475 C 162 456, 212 454, 262 470 C 308 485, 350 504, 398 512 C 432 516, 466 515, 497 509" stroke="currentColor" stroke-width="54" stroke-linecap="round" /><path d="M 155 518 C 192 509, 228 510, 264 522 C 300 535, 334 550, 370 556 C 390 559, 410 559, 429 557" stroke="currentColor" stroke-width="54" stroke-linecap="round" /></svg>""",
    "podman": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3 12.5h18v4a5 5 0 0 1-5 5H8a5 5 0 0 1-5-5v-4z"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M6.5 9.5V6M10.5 9.5V5M14.5 9.5V6M18 9.5V7.5"/>
<circle cx="6.5" cy="12.5" r="1" fill="currentColor"/>
<circle cx="17.5" cy="12.5" r="1" fill="currentColor"/>
</svg>""",

    "container": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="currentColor" d="M3 7l9-4 9 4v10l-9 4-9-4V7zm9 2.2L5.2 6 3.6 6.7l7.2 3.2 7.2-3.2-1.6-.7L12 9.2zM4.5 8.4v8l6.7 3v-8l-6.7-3zm15 0l-6.7 3v8l6.7-3v-8z"/>
</svg>""",

    "image": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 2.5l9.5 5-9.5 5-9.5-5 9.5-5z"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M2.5 12l9.5 5 9.5-5"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M2.5 16.5l9.5 5 9.5-5"/>
</svg>""",

    "volume": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<ellipse cx="12" cy="6" rx="8" ry="3" fill="none" stroke="currentColor" stroke-width="2"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M4 6v12c0 1.66 3.58 3 8 3s8-1.34 8-3V6"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M4 12c0 1.66 3.58 3 8 3s8-1.34 8-3"/>
</svg>""",

    "network": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="currentColor" d="M9 2h6a1 1 0 011 1v4a1 1 0 01-1 1h-2v2h5a2 2 0 012 2v3h2a1 1 0 011 1v4a1 1 0 01-1 1h-6a1 1 0 01-1-1v-4a1 1 0 011-1h2v-3H6v3h2a1 1 0 011 1v4a1 1 0 01-1 1H2a1 1 0 01-1-1v-4a1 1 0 011-1h2v-3a2 2 0 012-2h5V8H9a1 1 0 01-1-1V3a1 1 0 011-1zM4 16v3h3v-3H4zm13 0v3h3v-3h-3zM10 4v3h4V4h-4z"/>
</svg>""",

    "pod": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="currentColor" d="M12 2l8 4.5v9L12 20l-8-4.5v-9L12 2zm0 2.3L6.4 7.5 12 10.7l5.6-3.2L12 4.3zM5.6 9v5.6l5.6 3.2v-5.6L5.6 9zm12.8 0l-5.6 3.2v5.6l5.6-3.2V9z"/>
</svg>""",

    "terminal": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="2.5" y="4" width="19" height="16" rx="2" fill="none" stroke="currentColor" stroke-width="2"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M7 9.5l3 2.5-3 2.5M13 15h4"/>
</svg>""",

    "log": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6z"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M14 3v6h6M8 13h8M8 17h5"/>
</svg>""",

    "play": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" d="M7.5 4.5l12 7.5-12 7.5z"/>
</svg>""",

    "stop": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="5" y="5" width="14" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="2"/>
</svg>""",

    "restart": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3.5 12.5a8.5 8.5 0 1 0 2.6-6.1"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3.5 5.5v6h6"/>
</svg>""",

    "delete": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3.5 6.5h17M8.5 6.5V4a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v2.5"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M5.5 6.5l1 13a1.5 1.5 0 0 0 1.5 1.4h8a1.5 1.5 0 0 0 1.5-1.4l1-13"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M10 11v6M14 11v6"/>
</svg>""",

    "refresh": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20.5 11.5a8.5 8.5 0 1 1-2.6-6.1"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20.5 4.5v6h-6"/>
</svg>""",

    "search": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="currentColor" stroke-width="2"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M15.5 15.5L20 20"/>
</svg>""",

    "add": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M12 5v14M5 12h14"/>
</svg>""",

    "clean": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M4 20l6-6M9 9l6 6M13.5 3.5l7 7-9 9-7-7z"/>
</svg>""",

    "settings": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M4 6.5h9M17 6.5h3M4 12h3M11 12h9M4 17.5h11M19 17.5h1"/>
<circle cx="15" cy="6.5" r="2.2" fill="none" stroke="currentColor" stroke-width="2"/>
<circle cx="9" cy="12" r="2.2" fill="none" stroke="currentColor" stroke-width="2"/>
<circle cx="17" cy="17.5" r="2.2" fill="none" stroke="currentColor" stroke-width="2"/>
</svg>""",

    "info": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M12 11v5.5"/>
<circle cx="12" cy="7.8" r="1.2" fill="currentColor"/>
</svg>""",

    "compose": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="3" y="3.5" width="7.5" height="7.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="2"/>
<rect x="13.5" y="3.5" width="7.5" height="7.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="2"/>
<rect x="3" y="14" width="7.5" height="7.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="2"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M14 18.5l2.5 2.5 5-5"/>
</svg>""",
    "edit": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M16.5 3.5l4 4L8 20H4v-4L16.5 3.5z"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M14 6l4 4"/>
</svg>""",
    "save": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M17 21v-8H7v8M7 3v5h8"/>
</svg>""",
    "folder": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M3 7.5a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-10.5z"/>
</svg>""",
    "up": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 19.5V5M5.5 11.5L12 5l6.5 6.5"/>
</svg>""",
    "down": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M12 4.5V19M5.5 12.5L12 19l6.5-6.5"/>
</svg>""",
    "build": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M14.5 6.5a3.5 3.5 0 1 0 4.9-3.2l-2.4 2.4-2.5-.6-.6-2.5 2.4-2.4a3.5 3.5 0 0 0-3.2 4.9l-8.3 8.3a2 2 0 1 0 2.8 2.8l8.3-8.3"/>
</svg>""",

    "pause": """<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
<rect x="6" y="4.5" width="4" height="15" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>
<rect x="14" y="4.5" width="4" height="15" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>
</svg>""",
}

# KDE 系统图标映射（优先使用）
KDE_ICON_MAP = {
    "podman": "utilities-terminal",
    "seal": "applications-system",
    "container": "application-x-executable",
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


def icon(name: str, size: int = 16, danger: bool = False) -> QIcon:
    """获取图标（颜色随主题动态变化）

    策略：
    1. 内置 SVG → 用 QIconEngine 动态渲染，主题切换时自动变色
    2. 若 KDE 有对应语义图标且内置 SVG 缺失，用 KDE 图标

    注意：size 是逻辑尺寸，实际按 2x 渲染保证高 DPI 清晰。
    请优先使用 ICON_SIZE_* 常量，勿硬编码数字。
    """
    svg_content = SVG.get(name)
    if svg_content:
        return QIcon(_ThemedIconEngine(svg_content, size, danger))

    kde_name = KDE_ICON_MAP.get(name, name)
    return QIcon.fromTheme(kde_name)


class _ThemedIconEngine(QIconEngine):
    """动态图标引擎

    绘制时检测前景色是否变化，变化则重新渲染，
    因此主题切换后 Qt 重绘时自动使用新颜色。
    """

    def __init__(self, svg_content: str, size: int, danger: bool = False):
        super().__init__()
        self._svg = svg_content
        self._size = size
        self._danger = danger
        self._cached: QPixmap | None = None
        self._cached_color: str = ""
        self._cached_size: int = 0

    def _pixmap(self, size: int) -> QPixmap:
        # danger=True 时固定红色（不随主题变化）
        color = DANGER_FG if self._danger else _fg_color()
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


