"""侧边栏导航

参照 Dolphin 的「位置」面板：
- 紧凑行高（32px），无额外间距
- 图标 + 文字左对齐
- 分组标题（小字灰色）
"""

from __future__ import annotations

from ui.widgets import apply_aligned_delegate
from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from resources.icons import ICON_SIZE_SIDEBAR, icon

# 导航分组：(组标题 或 None, [(key, 显示名, 图标名), ...])
NAV_GROUPS: list[tuple[str | None, list[tuple[str, str, str]]]] = [
    (None, [
        ("containers", "容器", "container"),
        ("images", "镜像", "image"),
        ("volumes", "卷", "volume"),
        ("networks", "网络", "network"),
        ("pods", "Pod", "pod"),
    ]),
    ("编排", [
        ("compose", "Compose", "compose"),
    ]),
    ("虚拟机", [
        ("vms", "虚拟机", "vm"),
    ]),
    ("系统", [
        ("settings", "设置", "settings"),
    ]),
]

# 扁平化的 key → 显示名（供菜单等使用）
NAV_ITEMS = [
    (key, label, ic)
    for _, items in NAV_GROUPS
    for key, label, ic in items
]

# key → 显示名，供窗口标题使用
NAV_LABELS: dict[str, str] = {key: label for key, label, _ in NAV_ITEMS}

ROW_HEIGHT = 32
SECTION_HEIGHT = 24


class Sidebar(QWidget):
    """左侧导航栏"""

    item_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidebar")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(0)

        self.list = QListWidget()
        # 图标与文字视觉居中（Qt 默认会让图标高 2–3px）
        apply_aligned_delegate(self.list)
        self.list.setObjectName("sidebarList")
        self.list.setIconSize(ICON_SIZE_SIDEBAR)
        self.list.setSpacing(0)
        self.list.setFrameShape(QListWidget.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setUniformItemSizes(False)
        self.list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        self._nav_items: list[QListWidgetItem] = []

        for section_title, items in NAV_GROUPS:
            if section_title:
                self._add_section(section_title)
            for key, label, ic in items:
                self._add_item(key, label, ic)

        layout.addWidget(self.list)
        self.list.currentItemChanged.connect(self._on_change)

        self._fit_width()

    # 图标 + 图标与文字间距 + QSS 的 padding(10×2) / margin(4×2) + 余量
    _CHROME = 56

    def _fit_width(self) -> None:
        """按最长导航项定宽（DPI / 字体变化时重算）

        宽度 = 图标 + 最长文字 + 固定开销。
        固定开销包含 QSS 里 item 的 padding/margin 和图标间距 ——
        这些不体现在 fontMetrics 里，漏掉就会把「Compose」截成「Comp...」。
        """
        if not hasattr(self, "list"):
            return
        if getattr(self, "_fitting", False):
            return
        self._fitting = True
        try:
            fm = self.list.fontMetrics()
            labels = [label for _k, label, _i in NAV_ITEMS]
            labels += [t for t, _items in NAV_GROUPS if t]
            text_w = max((fm.horizontalAdvance(x) for x in labels), default=60)

            width = ICON_SIZE_SIDEBAR.width() + text_w + self._CHROME
            width = max(120, min(width, 220))
            # 值没变就不要 setFixedWidth —— 它会再触发 StyleChange，
            # 和 changeEvent 里的重算凑成无限循环，最终把事件队列打爆
            if width != getattr(self, "_last_width", -1):
                self._last_width = width
                self.setFixedWidth(width)
        finally:
            self._fitting = False

    def changeEvent(self, e) -> None:
        """字体 / 样式 / 屏幕缩放变化后重新定宽

        不做这件事的话，把系统缩放从 100% 调到 150%，
        字体变大了但宽度还是按旧字号算的 —— 「Compose」直接被截断。
        """
        from PyQt6.QtCore import QEvent, QTimer

        # 注意：QEvent.Type.ScreenChangeInternal 是 Qt 私有枚举，
        # PyQt6 里不存在，写上去会 AttributeError → 异常抛进事件处理
        # → Qt fatal → 进程 abort。屏幕变化用主窗口的 screenChanged 信号处理。
        if e.type() in (
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.ApplicationFontChange,
        ) and hasattr(self, "list"):
            # 延后到事件处理之外再算：changeEvent 里同步改自身几何会重入
            QTimer.singleShot(0, self._fit_width)
        super().changeEvent(e)

    def _add_section(self, title: str) -> None:
        """分组标题（不可选中）"""
        item = QListWidgetItem(title)
        item.setFlags(Qt.ItemFlag.NoItemFlags)  # 不可选
        item.setSizeHint(QSize(0, SECTION_HEIGHT))
        item.setData(Qt.ItemDataRole.UserRole, None)
        self.list.addItem(item)

    def _add_item(self, key: str, label: str, ic: str) -> None:
        item = QListWidgetItem(
            icon(ic, ICON_SIZE_SIDEBAR.width()), label
        )
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setSizeHint(QSize(0, ROW_HEIGHT))
        self.list.addItem(item)
        self._nav_items.append(item)

    def _on_change(self, cur: QListWidgetItem | None, _prev) -> None:
        if cur is None:
            return
        key = cur.data(Qt.ItemDataRole.UserRole)
        if key:
            self.item_selected.emit(key)

    def refresh_icons(self) -> None:
        """主题切换后强制重绘图标（QIconEngine 会按新主题色渲染）"""
        for item in self._nav_items:
            key = item.data(Qt.ItemDataRole.UserRole)
            ic = next((i for k, _l, i in NAV_ITEMS if k == key), None)
            if ic:
                item.setIcon(icon(ic, ICON_SIZE_SIDEBAR.width()))
        self.list.viewport().update()

    def select(self, key: str) -> None:
        """程序化选中（不触发 item_selected 信号，避免递归）"""
        for item in self._nav_items:
            if item.data(Qt.ItemDataRole.UserRole) == key:
                if self.list.currentItem() is item:
                    return
                blocked = self.list.blockSignals(True)
                self.list.setCurrentItem(item)
                self.list.blockSignals(blocked)
                return
