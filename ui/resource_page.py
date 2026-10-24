"""通用资源表格页

镜像、卷、网络、Pod 都复用这个基类，只需提供：
- 列定义
- 数据获取函数
- 行转换函数
- 工具栏动作
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QSize, QSortFilterProxyModel, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from resources.icons import ICON_SIZE_TOOLBAR, icon


class ResourcePage(QWidget):
    """通用资源页基类"""

    status_message = pyqtSignal(str)

    def __init__(
        self,
        client: PodmanClient,
        *,
        columns: list[str],
        icon_name: str,
        unit: str = "项",
        auto_refresh_ms: int = 10000,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.client = client
        self.columns = columns
        self.icon_name = icon_name
        self.unit = unit
        self._rows: list[dict] = []

        self._build_ui()
        self.refresh_silent()   # 创建时立即加载一次

        if auto_refresh_ms > 0:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.refresh_silent)
            self._timer.start(auto_refresh_ms)

    # ── 子类必须实现 ──
    def fetch_data(self) -> list[dict]:
        raise NotImplementedError

    def row_from_item(self, item: dict) -> list[QStandardItem]:
        raise NotImplementedError

    def item_id(self, item: dict) -> str:
        return ""

    def item_name(self, item: dict) -> str:
        return ""

    def build_actions(self, tb: QToolBar) -> None:
        """子类追加自己的工具栏动作"""
        pass

    def on_delete(self, item: dict) -> None:
        """子类实现删除逻辑"""
        pass

    def extra_menu(self, menu: QMenu, item: dict) -> None:
        """子类追加右键菜单项"""
        pass

    # ── UI ──
    def refresh_icons(self) -> None:
        """主题切换后重建图标（QIconEngine 需重新绑定）"""
        if hasattr(self, 'act_refresh'):
            self.act_refresh.setIcon(icon("refresh"))

    def keyPressEvent(self, e) -> None:
        """Delete 键删除选中项"""
        if e.key() == Qt.Key.Key_Delete and hasattr(self, "act_delete"):
            if self.act_delete.isEnabled():
                self._delete_selected()
                return
        super().keyPressEvent(e)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 12)
        layout.setSpacing(8)

        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        self.act_refresh = QAction(icon("refresh"), "刷新", self)
        self.act_refresh.setProperty("iconName", "refresh")
        self.act_refresh.triggered.connect(self.refresh)
        tb.addAction(self.act_refresh)

        self.build_actions(tb)

        # 删除按钮（紧接其他操作，图标红色）
        self.act_delete = QAction(icon("delete", danger=True), "删除", self)
        self.act_delete.setProperty("iconName", "delete")
        self.act_delete.setToolTip("删除选中项（支持多选，Delete 键）")
        self.act_delete.triggered.connect(self._delete_selected)
        tb.addAction(self.act_delete)

        btn = tb.widgetForAction(self.act_delete)
        if btn is not None:
            btn.setObjectName("dangerDelete")

        # 搜索框
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("搜索…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(200)
        self.search.setFixedHeight(26)
        self.search.addAction(
            icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._on_filter)
        tb.addWidget(self.search)

        layout.addWidget(tb)

        self.model = QStandardItemModel(0, len(self.columns), self)
        self.model.setHorizontalHeaderLabels(self.columns)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableView.Shape.NoFrame)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(
            self._update_selection_ui
        )

        hdr = self.table.horizontalHeader()
        hdr.setFixedHeight(26)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(False)
        for i in range(len(self.columns)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        if len(self.columns) > 1:
            hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        # 表格 + 空状态叠加
        from PyQt6.QtWidgets import QStackedLayout
        from ui.widgets import EmptyState

        self._table_stack = QStackedLayout()
        self._table_stack.addWidget(self.table)

        self._empty = EmptyState(
            icon_name=self.icon_name,
            title=f"还没有{self.unit}",
            subtitle=self.empty_hint(),
            action_text=self.empty_action_text(),
            on_action=self.empty_action(),
        )
        self._table_stack.addWidget(self._empty)
        self._table_stack.setCurrentWidget(self.table)

        layout.addLayout(self._table_stack)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setObjectName("hintLabel")
        layout.addWidget(self.lbl_stats)

    # ── 数据 ──
    def refresh(self) -> None:
        self.status_message.emit("正在刷新…")
        self.refresh_silent()
        self.status_message.emit(f"已刷新，共 {len(self._rows)} 个{self.unit}")

    def refresh_silent(self) -> None:
        try:
            self._rows = self.fetch_data()
        except PodmanError as e:
            self.status_message.emit(f"获取数据失败: {e}")
            return
        self._rebuild()

    def _rebuild(self) -> None:
        # 记住当前选中的资源（用 item_id 标识）
        selected_id = self._current_item_id()

        self.model.removeRows(0, self.model.rowCount())
        for item in self._rows:
            try:
                row = self.row_from_item(item)
            except Exception:
                continue
            for it in row:
                it.setEditable(False)
            if row:
                row[0].setIcon(icon(self.icon_name))
                # 把资源 ID 存进第一列，供刷新后恢复选中
                row[0].setData(self.item_id(item), Qt.ItemDataRole.UserRole)
            self.model.appendRow(row)

        # 恢复选中：源模型行号 → 代理模型行号
        if selected_id:
            for r in range(self.model.rowCount()):
                if self.model.item(r, 0).data(Qt.ItemDataRole.UserRole) == selected_id:
                    src_idx = self.model.index(r, 0)
                    proxy_idx = self.proxy.mapFromSource(src_idx)
                    if proxy_idx.isValid():
                        self.table.selectRow(proxy_idx.row())
                    break

        for i in range(len(self.columns)):
            self.table.resizeColumnToContents(i)

        # 空状态切换
        if len(self._rows) == 0:
            self._table_stack.setCurrentWidget(self._empty)
        else:
            self._table_stack.setCurrentWidget(self.table)

        # 重置删除按钮状态
        if hasattr(self, "act_delete"):
            self.act_delete.setEnabled(False)

        self.lbl_stats.setText(f"共 {len(self._rows)} 个{self.unit}")


    def run_op(
        self,
        label: str,
        fn,
        *args,
        on_success=None,
        busy_text: str = "",
        **kwargs,
    ) -> None:
        """在后台线程执行耗时操作，避免冻结 UI

        label     : 操作名，用于状态栏和通知（如「删除」）
        fn        : 要调用的函数（通常是 self.client.xxx）
        on_success: 成功后的回调（可选）
        busy_text : 状态栏提示文案，默认「正在{label}…」
        """
        if getattr(self, "_busy", False):
            self.status_message.emit("上一个操作还未完成，请稍候…")
            return

        self._busy = True
        self.act_refresh.setEnabled(False)
        self.status_message.emit(busy_text or f"正在{label}…")

        def work():
            return fn(*args, **kwargs)

        def done(result):
            self._busy = False
            self.act_refresh.setEnabled(True)
            self.status_message.emit(f"{label}完成")
            if on_success:
                on_success(result)
            self.refresh_silent()

        def error(msg):
            self._busy = False
            self.act_refresh.setEnabled(True)
            self.status_message.emit(f"{label}失败: {msg}")
            QMessageBox.warning(self, f"{label}失败", msg)
            notifier().op_failed(label, "", msg)
            self.refresh_silent()

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in getattr(self, "_tasks", []) if t._thread is not None]
        self._tasks.append(task)

    # ─────────────────────────────────────────
    # 多选与批量删除
    # ─────────────────────────────────────────
    def _selected_items(self) -> list[dict]:
        """当前选中的所有资源（按行号顺序，跳过不可删除项）"""
        rows = self.table.selectionModel().selectedRows()
        out = []
        for idx in sorted(rows, key=lambda i: i.row()):
            src = self.proxy.mapToSource(idx)
            item = self.model.item(src.row(), 0)
            if item is None:
                continue
            if not self.is_deletable(item):
                continue
            rid = item.data(Qt.ItemDataRole.UserRole)
            if not rid:
                continue
            for r in self._rows:
                if self.item_id(r) == rid:
                    out.append(r)
                    break
        return out

    def _update_selection_ui(self) -> None:
        """选中变化：启用/禁用删除按钮

        若选中的项全部不可删除（如内置网络），按钮置灰。
        """
        if not hasattr(self, "act_delete"):
            return

        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.act_delete.setEnabled(False)
            return

        deletable = 0
        for idx in rows:
            src = self.proxy.mapToSource(idx)
            item = self.model.item(src.row(), 0)
            if item is None:
                continue
            if self.is_deletable(item):
                deletable += 1

        self.act_delete.setEnabled(deletable > 0)
        if deletable == 0:
            self.act_delete.setToolTip("选中的项不可删除（内置资源）")
        else:
            self.act_delete.setToolTip("删除选中项（支持多选，Delete 键）")

    def is_deletable(self, name_item) -> bool:
        """判断某行是否可删除（子类可覆盖）

        name_item: 第一列的 QStandardItem
        """
        # 通用约定：UserRole+10 为 True 表示受保护
        return not bool(name_item.data(Qt.ItemDataRole.UserRole + 10))

    def _delete_selected(self) -> None:
        """删除选中项（支持批量）"""
        items = self._selected_items()
        if not items:
            return

        if len(items) == 1:
            # 单个：走子类原有确认流程
            self.on_delete(items[0])
            return

        # 批量：列出清单确认
        names = [self.item_name(i) for i in items]
        preview = "\n".join(f"  · {n}" for n in names[:12])
        if len(names) > 12:
            preview += f"\n  … 还有 {len(names) - 12} 项"

        r = QMessageBox.warning(
            self, f"删除 {len(items)} 个{self.unit}",
            f"将删除以下 {len(items)} 个{self.unit}：\n\n{preview}\n\n"
            f"此操作不可恢复，确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self._batch_delete(items)

    def _batch_delete(self, items: list[dict]) -> None:
        """后台批量删除"""
        total = len(items)
        names = [self.item_name(i) for i in items]

        self.status_message.emit(f"正在删除 {total} 个{self.unit}…")
        self.act_delete.setEnabled(False)

        def work():
            ok, failed = 0, []
            for it in items:
                try:
                    self.delete_item(it)
                    ok += 1
                except Exception as e:
                    failed.append((self.item_name(it), str(e)))
            return ok, failed

        def done(result):
            ok, failed = result
            self.act_delete.setEnabled(True)

            if failed:
                detail = "\n".join(f"  · {n}: {e}" for n, e in failed[:8])
                if len(failed) > 8:
                    detail += f"\n  … 还有 {len(failed) - 8} 项"
                QMessageBox.warning(
                    self, "部分删除失败",
                    f"成功 {ok} 个，失败 {len(failed)} 个：\n\n{detail}",
                )
                notifier().op_failed("批量删除", f"{len(failed)} 个", "")
            else:
                notifier().send(
                    f"已删除 {ok} 个{self.unit}",
                    "、".join(names[:5]) + ("…" if len(names) > 5 else ""),
                    icon_name="delete",
                )

            self.status_message.emit(f"删除完成：成功 {ok} 个")
            self.refresh_silent()

        def error(msg):
            self.act_delete.setEnabled(True)
            self.status_message.emit(f"删除失败: {msg}")
            QMessageBox.critical(self, "删除失败", msg)

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in getattr(self, "_tasks", []) if t._thread is not None]
        self._tasks.append(task)

    def delete_item(self, item: dict) -> None:
        """删除单个资源（子类必须实现，供批量删除调用）"""
        raise NotImplementedError(f"{self.__class__.__name__} 未实现 delete_item")

    def on_disconnected(self) -> None:
        """Podman 断开：清空列表，显示空状态"""
        self._rows = []
        self.model.removeRows(0, self.model.rowCount())
        self._table_stack.setCurrentWidget(self._empty)
        self._empty.set_text(
            "Podman 服务已断开",
            "无法连接到 Podman，正在等待重连…\n"
            "可尝试：systemctl --user restart podman.socket",
        )
        if hasattr(self, "act_delete"):
            self.act_delete.setEnabled(False)
        self.lbl_stats.setText("连接已断开")

    def on_reconnected(self) -> None:
        """重连后恢复空状态文案并刷新"""
        self._empty.set_text(
            f"还没有{self.unit}",
            self.empty_hint(),
        )
        self.refresh_silent()

    def empty_hint(self) -> str:
        """空状态副说明（子类可覆盖）"""
        return f"当前没有{self.unit}。可以点下方按钮创建。"

    def empty_action_text(self) -> str:
        """空状态按钮文字（返回空串表示不显示按钮）"""
        return ""

    def empty_action(self):
        """空状态按钮回调"""
        return None

    def _current_item_id(self) -> str:
        """当前选中行的资源 ID（未选中返回空串）"""
        idx = self.table.currentIndex()
        if not idx.isValid():
            return ""
        src = self.proxy.mapToSource(idx)
        item = self.model.item(src.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _current_row(self) -> dict | None:
        """当前选中行对应的原始数据"""
        rid = self._current_item_id()
        if not rid:
            return None
        for item in self._rows:
            if self.item_id(item) == rid:
                return item
        return None

    def _on_filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)

    # ── 选中 ──
    def _current_item(self) -> dict | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        if src.row() >= len(self._rows):
            return None
        return self._rows[src.row()]

    def _context_menu(self, pos) -> None:
        if not self.table.indexAt(pos).isValid():
            return
        item = self._current_item()
        if not item:
            return
        m = QMenu(self)
        self.extra_menu(m, item)
        m.addSeparator()
        act_del = QAction(icon("delete"), "删除", self)
        act_del.triggered.connect(lambda: self.on_delete(item))
        m.addAction(act_del)
        m.exec(self.table.viewport().mapToGlobal(pos))

    def confirm_delete(self, name: str) -> bool:
        ret = QMessageBox.question(
            self, "确认删除",
            f"确定要删除「{name}」吗？\n\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ret == QMessageBox.StandardButton.Yes
