"""卷管理页"""

from __future__ import annotations

from PyQt6.QtGui import QAction, QStandardItem
from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QToolBar

from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from resources.icons import icon
from ui.widgets import ask_text
from ui.resource_page import ResourcePage
from ui.utils import fmt_time

class VolumePage(ResourcePage):
    def __init__(self, client: PodmanClient, parent=None):
        super().__init__(
            client,
            columns=["名称", "驱动", "挂载点", "创建时间"],
            icon_name="volume",
            unit="卷",
            auto_refresh_ms=20000,
            parent=parent,
        )

    def fetch_data(self) -> list[dict]:
        return self.client.list_volumes()

    def row_from_item(self, item: dict) -> list[QStandardItem]:
        created = fmt_time(item.get("CreatedAt"))
        if isinstance(created, str) and len(created) > 16:
            created = created[:16].replace("T", " ")
        return [
            QStandardItem(item.get("Name") or "-"),
            QStandardItem(item.get("Driver") or "-"),
            QStandardItem(item.get("Mountpoint") or "-"),
            QStandardItem(str(created)),
        ]

    def empty_hint(self) -> str:
        return "命名卷用于持久化容器数据。\n可以在启动容器时用 -v 卷名:/路径 挂载。"

    def empty_action_text(self) -> str:
        return "新建卷"

    def empty_action(self):
        return self._create

    def item_id(self, item: dict) -> str:
        return item.get("Name", "")

    def item_name(self, item: dict) -> str:
        return item.get("Name") or ""

    def build_actions(self, tb: QToolBar) -> None:
        self.act_new = QAction(icon("add"), "新建", self)

        self.act_new.setProperty("iconName", "add")
        self.act_new.triggered.connect(self._create)
        tb.addAction(self.act_new)

    def _create(self) -> None:
        name, ok = ask_text(
            self, "新建卷",
            "请输入卷名称：",
            hint="卷用于持久化容器数据，位于 Podman 存储目录",
            width=560, height=190,
        )
        if not ok or not name:
            return
        self.run_op(
            "新建", self.client.create_volume, name,
            busy_text=f"正在创建卷 {name}…",
            on_success=lambda _r: notifier().volume_created(name),
        )

    def delete_item(self, item: dict) -> None:
        """批量删除时调用"""
        self.client.remove_volume(self.item_name(item), force=True)

    def on_delete(self, item: dict) -> None:
        name = self.item_name(item)
        if not self.confirm_delete(name):
            return
        self.run_op(
            "删除", self.client.remove_volume, name, force=True,
            busy_text=f"正在删除卷 {name}…",
            on_success=lambda _r: notifier().volume_deleted(name),
        )
