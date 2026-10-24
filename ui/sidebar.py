"""侧边栏导航

参照 Dolphin 的「位置」面板：
- 紧凑行高（32px），无额外间距
- 图标 + 文字左对齐
- 分组标题（小字灰色）
"""

from __future__ import annotations

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
        self.setFixedWidth(184)
        self.setObjectName("sidebar")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(0)

        self.list = QListWidget()
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
