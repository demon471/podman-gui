"""Pod 管理页"""

from __future__ import annotations

from PyQt6.QtGui import QAction, QBrush, QColor, QStandardItem
from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QToolBar

from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from resources.icons import icon
from ui.widgets import ask_text
from ui.resource_page import ResourcePage
from ui.utils import fmt_time

def _state_text(s: str) -> str:
    m = {
        "Running": "运行中",
        "Exited": "已停止",
        "Stopped": "已停止",
        "Created": "已创建",
        "Degraded": "异常",
        "Paused": "已暂停",
        "Dead": "已失效",
    }
    return m.get(s, s)

class PodPage(ResourcePage):
    def __init__(self, client: PodmanClient, parent=None):
        super().__init__(
            client,
            columns=["名称", "状态", "容器数", "ID", "创建时间"],
            icon_name="pod",
            unit="Pod",
            auto_refresh_ms=10000,
            parent=parent,
        )

    def fetch_data(self) -> list[dict]:
        return self.client.list_pods()

    def row_from_item(self, item: dict) -> list[QStandardItem]:
        status = item.get("Status") or "-"
        containers = item.get("Containers") or []
        created = fmt_time(item.get("Created"))
        if isinstance(created, str) and len(created) > 16:
            created = created[:16].replace("T", " ")

        state_item = QStandardItem(_state_text(status))
        # 状态图标（映射 Pod 状态到容器状态语义）
        _st = str(status).lower()
        _icon_state = {
            "running": "running",
            "degraded": "paused",
            "exited": "exited",
            "created": "created",
            "paused": "paused",
            "stopped": "exited",
            "dead": "dead",
        }.get(_st, "exited")
        state_item.setIcon(state_icon(_icon_state, 14))
        if status.lower() == "running":
            state_item.setForeground(QBrush(QColor("#27ae60")))
        elif status.lower() in ("exited", "stopped", "dead"):
            state_item.setForeground(QBrush(QColor("#95a5a6")))
        elif status.lower() == "degraded":
            state_item.setForeground(QBrush(QColor("#e74c3c")))

        return [
            QStandardItem(item.get("Name") or "-"),
            state_item,
            QStandardItem(str(len(containers))),
            QStandardItem((item.get("Id") or "")[:12]),
            QStandardItem(str(created)),
        ]

    def empty_hint(self) -> str:
        return "Pod 是一组共享网络命名空间的容器。\n常用于需要 localhost 互通的场景。"

    def empty_action_text(self) -> str:
        return "新建 Pod"

    def empty_action(self):
        return self._create

    def item_id(self, item: dict) -> str:
        return item.get("Id", "")

    def item_name(self, item: dict) -> str:
        return item.get("Name") or ""

    def build_actions(self, tb: QToolBar) -> None:
        self.act_new = QAction(icon("add"), "新建", self)

        self.act_new.setProperty("iconName", "add")
        self.act_new.triggered.connect(self._create)
        tb.addAction(self.act_new)

        self.act_start = QAction(icon("play"), "启动", self)


        self.act_start.setProperty("iconName", "play")
        self.act_start.triggered.connect(lambda: self._pod_action("启动", self.client.start_pod))
        tb.addAction(self.act_start)

        self.act_stop = QAction(icon("stop"), "停止", self)


        self.act_stop.setProperty("iconName", "stop")
        self.act_stop.triggered.connect(lambda: self._pod_action("停止", self.client.stop_pod))
        tb.addAction(self.act_stop)

        self.act_restart = QAction(icon("restart"), "重启", self)


        self.act_restart.setProperty("iconName", "restart")
        self.act_restart.triggered.connect(lambda: self._pod_action("重启", self.client.restart_pod))
        tb.addAction(self.act_restart)

    def _pod_action(self, name: str, fn) -> None:
        item = self._current_item()
        if not item:
            return
        pid = item.get("Id", "")
        pod_name = self.item_name(item)
        self.run_op(
            name, fn, pid,
            busy_text=f"正在{name} Pod {pod_name}…",
            on_success=lambda _r: notifier().pod_action(name, pod_name),
        )

    def _create(self) -> None:
        name, ok = ask_text(
            self, "新建 Pod",
            "请输入 Pod 名称：",
            hint="Pod 内的容器共享网络命名空间",
            width=560, height=190,
        )
        if not ok or not name:
            return
        self.run_op(
            "新建", self.client.create_pod, name,
            busy_text=f"正在创建 Pod {name}…",
            on_success=lambda _r: notifier().container_created(f"Pod {name}"),
        )

    def delete_item(self, item: dict) -> None:
        """批量删除时调用"""
        self.client.remove_pod(item.get("Id", ""), force=True)

    def on_delete(self, item: dict) -> None:
        name = self.item_name(item)
        if not self.confirm_delete(name):
            return
        self.run_op(
            "删除", self.client.remove_pod, item.get("Id", ""), force=True,
            busy_text=f"正在删除 Pod {name}…",
            on_success=lambda _r: notifier().container_deleted(f"Pod {name}"),
        )
