"""镜像管理页"""

from __future__ import annotations

from PyQt6.QtGui import QAction, QStandardItem
from PyQt6.QtWidgets import QMenu
from PyQt6.QtWidgets import QInputDialog, QLineEdit, QMessageBox, QToolBar

from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from ui.utils import fmt_time
from ui.widgets import ask_text
from resources.icons import icon
from ui.resource_page import ResourcePage

def _fmt_size(n: int) -> str:
    if not n:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"

class ImagePage(ResourcePage):
    def __init__(self, client: PodmanClient, parent=None):
        super().__init__(
            client,
            columns=["仓库:标签", "ID", "大小", "创建时间"],
            icon_name="image",
            unit="镜像",
            auto_refresh_ms=15000,
            parent=parent,
        )
        self.table.doubleClicked.connect(self._show_detail)

    def fetch_data(self) -> list[dict]:
        return self.client.list_images()

    def row_from_item(self, item: dict) -> list[QStandardItem]:
        names = item.get("Names") or []
        repo = names[0] if names else "<none>"
        if len(names) > 1:
            repo += f" +{len(names)-1}"
        iid = (item.get("Id") or "").replace("sha256:", "")[:12]
        size = _fmt_size(item.get("Size", 0))
        created = fmt_time(item.get("Created") or item.get("CreatedAt"))
        return [
            QStandardItem(repo),
            QStandardItem(iid),
            QStandardItem(size),
            QStandardItem(str(created)),
        ]

    def empty_hint(self) -> str:
        return "可以拉取镜像，或用「构建」从 Dockerfile 创建。"

    def item_id(self, item: dict) -> str:
        return item.get("Id", "")

    def item_name(self, item: dict) -> str:
        names = item.get("Names") or []
        return names[0] if names else item.get("Id", "")[:12]

    def build_actions(self, tb: QToolBar) -> None:
        self.act_pull = QAction(icon("add"), "拉取", self)

        self.act_pull.setProperty("iconName", "add")
        self.act_pull.triggered.connect(self._pull)
        tb.addAction(self.act_pull)

        self.act_prune = QAction(icon("clean"), "清理", self)


        self.act_prune.setProperty("iconName", "clean")
        self.act_prune.triggered.connect(self._prune)
        tb.addAction(self.act_prune)

    def extra_menu(self, menu: QMenu, item: dict) -> None:
        act = QAction(icon("info"), "查看详情", self)
        act.triggered.connect(self._show_detail)
        menu.addAction(act)

    # ── 操作 ──
    def _pull(self) -> None:
        ref, ok = ask_text(
            self, "拉取镜像",
            "请输入镜像名（例如 nginx:latest 或 docker.io/library/redis:7-alpine）：",
            hint="将从配置的仓库拉取；支持完整路径如 docker.io/library/redis:7-alpine",
            width=700, height=210,
        )
        if not ok or not ref:
            return
        self.run_op(
            "拉取", self.client.pull_image, ref,
            busy_text=f"正在拉取 {ref}…（可能需要几分钟）",
            on_success=lambda _r: notifier().image_pulled(ref),
        )

    def _prune(self) -> None:
        ret = QMessageBox.question(
            self, "清理镜像",
            "清理所有未使用的镜像（含悬空镜像）？\n\n这将删除没有容器引用的镜像。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        def show_result(res):
            deleted = (res or {}).get("ImagesDeleted") or []
            reclaimed = (res or {}).get("SpaceReclaimed", 0)
            QMessageBox.information(
                self, "清理完成",
                f"删除 {len(deleted)} 个镜像\n回收空间 {_fmt_size(reclaimed)}",
            )
            notifier().images_pruned(len(deleted), _fmt_size(reclaimed))

        self.run_op(
            "清理", self.client.prune_images, all=True,
            busy_text="正在清理镜像…", on_success=show_result,
        )

    def delete_item(self, item: dict) -> None:
        """批量删除时调用（不弹确认，无通知）"""
        self.client.remove_image(item.get("Id", self.item_name(item)), force=True)

    def on_delete(self, item: dict) -> None:
        name = self.item_name(item)
        if not self.confirm_delete(name):
            return
        self.run_op(
            "删除", self.client.remove_image, item.get("Id", name), force=True,
            busy_text=f"正在删除 {name}…",
            on_success=lambda _r: notifier().image_deleted(name),
        )

    def _show_detail(self) -> None:
        item = self._current_item()
        if not item:
            return
        try:
            info = self.client.inspect_image(item.get("Id", ""))
        except PodmanError as e:
            QMessageBox.warning(self, "获取详情失败", str(e))
            return
        from ui.detail_dialog import show_json_detail
        show_json_detail(self, f"镜像详情 - {self.item_name(item)}", info)
