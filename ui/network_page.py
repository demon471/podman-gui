"""网络管理页"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QBrush, QColor, QStandardItem
from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QToolBar

from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from resources.icons import icon
from ui.widgets import ask_text
from ui.resource_page import ResourcePage

# Podman 内置网络（不可删除）
BUILTIN_NETWORKS = ("podman", "host", "none")


class NetworkPage(ResourcePage):
    def __init__(self, client: PodmanClient, parent=None):
        super().__init__(
            client,
            columns=["名称", "驱动", "ID", "子网", "网关"],
            icon_name="network",
            unit="网络",
            auto_refresh_ms=20000,
            parent=parent,
        )

    def fetch_data(self) -> list[dict]:
        return self.client.list_networks()

    def row_from_item(self, item: dict) -> list[QStandardItem]:
        subnets = item.get("subnets") or []
        subnet = subnets[0].get("subnet", "-") if subnets else "-"
        gateway = subnets[0].get("gateway", "-") if subnets else "-"
        name = item.get("name") or "-"

        row = [
            QStandardItem(name),
            QStandardItem(item.get("driver") or "-"),
            QStandardItem((item.get("id") or "")[:12]),
            QStandardItem(subnet),
            QStandardItem(gateway),
        ]

        # 内置网络：整行置灰 + 禁止选中 + 标记
        if name in BUILTIN_NETWORKS:
            gray = QBrush(QColor("#95a5a6"))
            for it in row:
                it.setForeground(gray)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled)   # 去掉 Selectable
            row[0].setText(f"{name}（内置）")
            row[0].setToolTip("Podman 内置网络，不可删除")
            row[0].setData(True, Qt.ItemDataRole.UserRole + 10)   # builtin 标记

        return row

    def empty_hint(self) -> str:
        return "Podman 内置了 podman/host/none 三个网络。\n也可以创建自定义网络，让容器互相通信。"

    def item_id(self, item: dict) -> str:
        # Podman 网络 API 返回小写字段
        return item.get("id") or item.get("Id") or item.get("name", "")

    def item_name(self, item: dict) -> str:
        return item.get("name") or ""

    def build_actions(self, tb: QToolBar) -> None:
        self.act_new = QAction(icon("add"), "新建", self)

        self.act_new.setProperty("iconName", "add")
        self.act_new.triggered.connect(self._create)
        tb.addAction(self.act_new)

    def _create(self) -> None:
        name, ok = ask_text(
            self, "新建网络",
            "请输入网络名称：",
            hint="网络名称只能包含字母、数字、连字符和下划线",
            width=560, height=190,
        )
        if not ok or not name:
            return
        self.run_op(
            "新建", self.client.create_network, name,
            busy_text=f"正在创建网络 {name}…",
            on_success=lambda _r: notifier().network_created(name),
        )

    def delete_item(self, item: dict) -> None:
        """批量删除时调用（内置网络会抛错，由调用方汇总）"""
        name = self.item_name(item)
        if name in BUILTIN_NETWORKS:
            raise RuntimeError("内置网络不可删除")
        self.client.remove_network(name, force=True)

    def on_delete(self, item: dict) -> None:
        name = self.item_name(item)
        if name in BUILTIN_NETWORKS:
            QMessageBox.information(self, "无法删除", f"「{name}」是 Podman 内置网络，不能删除。")
            return
        if not self.confirm_delete(name):
            return
        self.run_op(
            "删除", self.client.remove_network, name, force=True,
            busy_text=f"正在删除网络 {name}…",
            on_success=lambda _r: notifier().network_deleted(name),
        )
