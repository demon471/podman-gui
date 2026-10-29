"""容器列表页

功能：
- 表格展示容器（名称/状态/镜像/端口/创建时间）
- 工具栏：启动/停止/重启/删除/终端/日志/刷新
- 搜索过滤
- 双击查看详情
- 右键菜单
- 自动刷新
"""

from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import QSortFilterProxyModel, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from resources.icons import ICON_SIZE_TOOLBAR, icon, state_icon
from ui.utils import fmt_time
from ui.widgets import apply_aligned_delegate

COLUMNS = ["名称", "状态", "镜像", "端口", "创建时间", "ID"]

def _state_text(state: str) -> str:
    """状态中文化"""
    mapping = {
        "running": "运行中",
        "exited": "已停止",
        "stopped": "已停止",
        "created": "已创建",
        "paused": "已暂停",
        "restarting": "重启中",
        "removing": "删除中",
        "dead": "已失效",
        "configured": "已配置",
    }
    return mapping.get(state.lower(), state)

class ContainerPage(QWidget):
    """容器管理页"""

    # 请求打开日志/终端
    request_logs = pyqtSignal(str, str)      # (cid, name)
    request_terminal = pyqtSignal(str, str)  # (cid, name)
    status_message = pyqtSignal(str)

    def __init__(self, client: PodmanClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self._containers: list[dict] = []
        self._build_ui()
        self.refresh_silent()   # 创建时立即加载一次

        # 自动刷新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_silent)
        self._timer.start(5000)

    # ─────────────────────────────────────────
    # UI 构建
    # ─────────────────────────────────────────
    def refresh_icons(self) -> None:
        """主题切换后重建图标（QIconEngine 需重新绑定）"""
        if hasattr(self, 'act_start'):
            self.act_start.setIcon(icon("play"))
        if hasattr(self, 'act_stop'):
            self.act_stop.setIcon(icon("stop"))
        if hasattr(self, 'act_restart'):
            self.act_restart.setIcon(icon("restart"))
        if hasattr(self, 'act_terminal'):
            self.act_terminal.setIcon(icon("terminal"))
        if hasattr(self, 'act_logs'):
            self.act_logs.setIcon(icon("log"))
        if hasattr(self, "act_open"):
            self.act_open.setIcon(icon("network"))
        if hasattr(self, 'act_delete'):
            self.act_delete.setIcon(icon("delete"))
        if hasattr(self, 'act_refresh'):
            self.act_refresh.setIcon(icon("refresh"))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 12)
        layout.setSpacing(8)

        # ── 工具栏 ──
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        self.act_start = QAction(icon("play"), "启动", self)

        self.act_start.setProperty("iconName", "play")
        self.act_start.triggered.connect(self._start)
        tb.addAction(self.act_start)

        self.act_stop = QAction(icon("stop"), "停止", self)

        self.act_stop.setProperty("iconName", "stop")
        self.act_stop.triggered.connect(self._stop)
        tb.addAction(self.act_stop)

        self.act_restart = QAction(icon("restart"), "重启", self)

        self.act_restart.setProperty("iconName", "restart")
        self.act_restart.triggered.connect(self._restart)
        tb.addAction(self.act_restart)

        self.act_terminal = QAction(icon("terminal"), "终端", self)

        self.act_terminal.setProperty("iconName", "terminal")
        self.act_terminal.triggered.connect(self._open_terminal)
        tb.addAction(self.act_terminal)

        self.act_logs = QAction(icon("log"), "日志", self)

        self.act_logs.setProperty("iconName", "log")
        self.act_logs.triggered.connect(self._open_logs)
        tb.addAction(self.act_logs)

        self.act_open = QAction(icon("network"), "访问", self)
        self.act_open.setProperty("iconName", "network")
        self.act_open.setToolTip("在浏览器打开容器暴露的端口")
        self.act_open.triggered.connect(self._open_container_url)
        tb.addAction(self.act_open)

        # 不用红色图标：工具栏里只有它一个红块，显得突兀。
        # 删除的危险性由二次确认对话框承担，不靠颜色。
        self.act_delete = QAction(icon("delete"), "删除", self)

        self.act_delete.setProperty("iconName", "delete")
        self.act_delete.triggered.connect(self._delete)
        tb.addAction(self.act_delete)

        self.act_refresh = QAction(icon("refresh"), "刷新", self)

        self.act_refresh.setProperty("iconName", "refresh")
        self.act_refresh.triggered.connect(self.refresh)
        tb.addAction(self.act_refresh)

        # 搜索框：紧跟图标，不推到右侧
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("搜索容器…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(200)
        self.search.setFixedHeight(26)
        self.search.addAction(
            icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._on_filter)
        tb.addWidget(self.search)

        layout.addWidget(tb)

        # ── 表格 ──
        self.model = QStandardItemModel(0, len(COLUMNS), self)
        self.model.setHorizontalHeaderLabels(COLUMNS)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)  # 所有列
        self.proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.table = QTableView()
        # 图标与文字视觉居中（Qt 默认会让图标高 2–3px）
        apply_aligned_delegate(self.table)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
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
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        self.table.selectionModel().selectionChanged.connect(
            self._refresh_state_icons
        )

        hdr = self.table.horizontalHeader()
        hdr.setFixedHeight(26)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 96)
        self.table.setColumnWidth(3, 200)

        # 表格 + 空状态叠加
        from PyQt6.QtWidgets import QStackedLayout
        from ui.widgets import EmptyState

        self._table_stack = QStackedLayout()
        self._table_stack.addWidget(self.table)

        self._empty = EmptyState(
            icon_name="container",
            title="还没有容器",
        )
        self._table_stack.addWidget(self._empty)
        self._table_stack.setCurrentWidget(self.table)

        layout.addLayout(self._table_stack)

        # ── 底部统计 ──
        self.lbl_stats = QLabel("")
        self.lbl_stats.setObjectName("hintLabel")
        layout.addWidget(self.lbl_stats)

    # ─────────────────────────────────────────
    # 数据刷新
    # ─────────────────────────────────────────
    def refresh(self) -> None:
        """手动刷新（显示状态消息）"""
        self.status_message.emit("正在刷新容器列表…")
        self.refresh_silent()
        self.status_message.emit(f"已刷新，共 {len(self._containers)} 个容器")

    def refresh_silent(self) -> None:
        """静默刷新（自动刷新用）"""
        try:
            self._containers = self.client.list_containers(all=True)
        except PodmanError as e:
            self.status_message.emit(f"获取容器失败: {e}")
            return

        self._rebuild_table()
        self._update_actions()

    def _rebuild_table(self) -> None:
        # 记住当前选中
        selected_id = self._current_cid()

        self.model.removeRows(0, self.model.rowCount())

        running = 0
        for c in self._containers:
            name = (c.get("Names") or ["?"])[0]
            state = c.get("State", "")
            if state == "running":
                running += 1
            image = c.get("Image", "")
            # 端口格式化
            ports = c.get("Ports") or []
            port_str = ", ".join(
                f"{p.get('host_port','')}→{p.get('container_port','')}/{p.get('protocol','tcp')}"
                for p in ports[:3]
            )
            if len(ports) > 3:
                port_str += f" +{len(ports)-3}"

            cid = c.get("Id", "")
            row = [
                QStandardItem(name),
                QStandardItem(_state_text(state)),
                QStandardItem(image),
                QStandardItem(port_str or "-"),
                QStandardItem(fmt_time(c.get("Created"))),
                QStandardItem(cid[:12]),
            ]

            # 状态：图标 + 语义色文字
            state_item = row[1]
            state_item.setData(state, Qt.ItemDataRole.UserRole + 20)
            state_item.setIcon(state_icon(state, 14))

            from resources.icons import STATE_COLORS

            color = QColor(STATE_COLORS.get(state.lower(), "#95a5a6"))
            state_item.setForeground(QBrush(color))
            state_item.setToolTip(f"容器状态：{_state_text(state)}")

            # 保存完整 ID 和状态到第一列
            row[0].setData(cid, Qt.ItemDataRole.UserRole)
            row[0].setData(state, Qt.ItemDataRole.UserRole + 1)
            row[0].setData(name, Qt.ItemDataRole.UserRole + 2)

            # 端口列（第 3 列）存端口详情，供右键菜单/双击使用
            row[3].setData(ports, Qt.ItemDataRole.UserRole)
            row[3].setToolTip(self._ports_tooltip(ports))

            # 图标
            row[0].setIcon(icon("container"))

            for item in row:
                item.setEditable(False)
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

        # 空状态切换
        if len(self._containers) == 0:
            self._table_stack.setCurrentWidget(self._empty)
        else:
            self._table_stack.setCurrentWidget(self.table)

        # 统计
        total = len(self._containers)
        self.lbl_stats.setText(
            f"共 {total} 个容器 · {running} 个运行中 · {total - running} 个已停止"
        )

    # ─────────────────────────────────────────
    # 端口操作
    # ─────────────────────────────────────────
    @staticmethod
    def _ports_tooltip(ports: list) -> str:
        """端口列悬浮提示"""
        if not ports:
            return "无端口映射"
        lines = []
        for p in ports:
            hp = p.get("host_port", "")
            cp = p.get("container_port", "")
            proto = p.get("protocol", "tcp")
            ip = p.get("host_ip", "") or "0.0.0.0"
            if hp:
                lines.append(f"{ip}:{hp} → {cp}/{proto}")
            else:
                lines.append(f"{cp}/{proto}（仅容器内）")
        lines.append("")
        lines.append("双击打开浏览器 · 右键可复制")
        return "\n".join(lines)

    def _port_urls(self, ports: list) -> list[tuple[str, str]]:
        """把端口映射转成可访问的 URL 列表

        返回 [(显示文本, URL), ...]，只包含 TCP 且有宿主端口的项。
        默认使用宿主机局域网 IP，便于从其他设备访问。
        """
        from ui.utils import host_ip

        out = []
        lan_ip = host_ip()

        for p in ports:
            hp = p.get("host_port", "")
            proto = p.get("protocol", "tcp")
            if not hp or proto != "tcp":
                continue

            bind_ip = p.get("host_ip", "") or ""
            # 绑定了具体 IP 就用它；通配地址用宿主机 IP
            if bind_ip and bind_ip not in ("0.0.0.0", "::", "[::]"):
                host = bind_ip
            else:
                host = lan_ip

            url = f"http://{host}:{hp}"
            out.append((f"{host}:{hp}", url))
        return out

    def _port_context_menu(self, pos) -> None:
        """端口列专用右键菜单"""
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return

        # 先选中该行，让状态栏/按钮状态同步
        self.table.selectRow(idx.row())

        src = self.proxy.mapToSource(idx)
        item = self.model.item(src.row(), 3)
        if item is None:
            return

        ports = item.data(Qt.ItemDataRole.UserRole) or []
        if not ports:
            m = QMenu(self)
            act = m.addAction("无端口映射")
            act.setEnabled(False)
            m.exec(self.table.viewport().mapToGlobal(pos))
            return

        m = QMenu(self)

        urls = self._port_urls(ports)

        # 局域网地址
        for label, url in urls:
            act = m.addAction(icon("network"), f"打开 {label}")
            act.triggered.connect(lambda _=False, u=url: self._open_url(u))

        if urls:
            m.addSeparator()

        # 复制全部端口文本
        act_copy_all = m.addAction("复制全部端口")
        act_copy_all.triggered.connect(
            lambda: self._copy_text(item.text())
        )

        # 复制 URL
        if urls:
            act_copy_urls = m.addAction("复制所有地址")
            act_copy_urls.triggered.connect(
                lambda: self._copy_text("\n".join(u for _l, u in urls))
            )

        m.addSeparator()

        # 复制容器名（从当前行读取，不依赖选中状态）
        name_item = self.model.item(src.row(), 0)
        name = name_item.text() if name_item else ""
        if name:
            act_copy_name = m.addAction(f"复制容器名 {name}")
            act_copy_name.triggered.connect(lambda: self._copy_text(name))

        # 复制镜像名
        img_item = self.model.item(src.row(), 2)
        image = img_item.text() if img_item else ""
        if image and image != "-":
            act_copy_img = m.addAction(f"复制镜像 {image}")
            act_copy_img.triggered.connect(lambda: self._copy_text(image))

        m.exec(self.table.viewport().mapToGlobal(pos))

    def _current_ports(self) -> list:
        """当前选中容器的端口列表"""
        idx = self.table.currentIndex()
        if not idx.isValid():
            return []
        src = self.proxy.mapToSource(idx)
        item = self.model.item(src.row(), 3)
        return (item.data(Qt.ItemDataRole.UserRole) if item else None) or []

    def _open_container_url(self) -> None:
        """工具栏「访问」：打开第一个可用端口"""
        ports = self._current_ports()
        if not ports:
            self.status_message.emit("该容器没有暴露端口")
            return

        urls = self._port_urls(ports)
        if not urls:
            self.status_message.emit("该容器没有可访问的 TCP 端口")
            return

        if len(urls) == 1:
            self._open_url(urls[0][1])
            return

        # 多个端口：弹出选择菜单
        from PyQt6.QtWidgets import QMenu

        m = QMenu(self)
        for label, url in urls:
            act = m.addAction(icon("network"), label)
            act.triggered.connect(lambda _=False, u=url: self._open_url(u))

        # 在工具栏按钮下方弹出
        btn = self.findChild(QToolBar)
        pos = btn.mapToGlobal(btn.rect().bottomLeft()) if btn else self.mapToGlobal(self.rect().center())
        m.exec(pos)

    def _open_url(self, url: str) -> None:
        """用默认浏览器打开 URL"""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        ok = QDesktopServices.openUrl(QUrl(url))
        if ok:
            self.status_message.emit(f"已在浏览器打开 {url}")
        else:
            QMessageBox.warning(
                self, "打开失败",
                f"无法打开 {url}\n\n请检查系统默认浏览器设置。",
            )

    def _copy_text(self, text: str) -> None:
        """复制到剪贴板"""
        from PyQt6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        preview = text.replace("\n", " ")
        if len(preview) > 50:
            preview = preview[:50] + "…"
        self.status_message.emit(f"已复制: {preview}")

    def _on_double_click(self, idx) -> None:
        """双击：端口列打开浏览器，其他列看日志"""
        if idx.column() == 3:
            src = self.proxy.mapToSource(idx)
            item = self.model.item(src.row(), 3)
            ports = (item.data(Qt.ItemDataRole.UserRole) if item else None) or []
            urls = self._port_urls(ports)
            if urls:
                self._open_url(urls[0][1])
            else:
                self.status_message.emit("该容器没有可访问的端口映射")
            return
        self._open_logs()

    def on_disconnected(self) -> None:
        """Podman 断开：清空容器列表"""
        self._containers = []
        self.model.removeRows(0, self.model.rowCount())
        self._table_stack.setCurrentWidget(self._empty)
        self._empty.set_text(
            "Podman 服务已断开",
            "无法连接到 Podman，正在等待重连…\n"
            "可尝试：systemctl --user restart podman.socket",
        )
        self._set_actions_enabled(False)
        self.lbl_stats.setText("连接已断开")

    def on_reconnected(self) -> None:
        """重连后恢复"""
        self._empty.set_text("还没有容器")
        self._set_actions_enabled(True)
        self.refresh_silent()

    def _on_filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)

    # ─────────────────────────────────────────
    # 选中状态
    # ─────────────────────────────────────────
    def _current_cid(self) -> str | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        item = self.model.item(src.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _current_state(self) -> str | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        item = self.model.item(src.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 1) if item else None

    def _current_name(self) -> str | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        item = self.model.item(src.row(), 0)
        return item.data(Qt.ItemDataRole.UserRole + 2) if item else None

    def _refresh_state_icons(self) -> None:
        """选中行用高对比状态图标（蓝底上灰图标看不见）"""
        selected = {
            self.proxy.mapToSource(i).row()
            for i in self.table.selectionModel().selectedRows()
        }

        for r in range(self.model.rowCount()):
            item = self.model.item(r, 1)
            if item is None:
                continue
            state = item.data(Qt.ItemDataRole.UserRole + 20) or ""
            item.setIcon(
                state_icon(str(state), 14, on_selected=(r in selected))
            )

    def _update_actions(self) -> None:
        cid = self._current_cid()
        state = self._current_state()
        has = cid is not None
        running = state == "running"

        self.act_start.setEnabled(has and not running)
        self.act_stop.setEnabled(has and running)
        self.act_restart.setEnabled(has)
        self.act_terminal.setEnabled(has and running)
        self.act_logs.setEnabled(has)
        self.act_delete.setEnabled(has)

        # 「访问」按钮：只有运行中且有 TCP 端口时可用
        if hasattr(self, "act_open"):
            can_open = has and running and bool(self._current_ports())
            self.act_open.setEnabled(can_open)

    # ─────────────────────────────────────────
    # 操作
    # ─────────────────────────────────────────
    def _do(self, action_name: str, fn, *args) -> None:
        """统一的操作执行 + 错误处理（后台线程，不阻塞 UI）

        停止/重启容器时 Podman 会等待进程退出（默认 10s），
        同步调用会让界面完全冻结，必须放后台。
        """
        cid = self._current_cid()
        name = self._current_name() or cid
        if not cid:
            return

        # 防止重复点击同一操作
        if getattr(self, "_busy", False):
            self.status_message.emit("上一个操作还未完成，请稍候…")
            return
        self._busy = True
        self._set_actions_enabled(False)

        self.status_message.emit(f"{action_name} {name}…")

        def work():
            fn(*args)
            return True

        def done(_result):
            self._busy = False
            self._set_actions_enabled(True)
            self.status_message.emit(f"{action_name} {name} 成功")
            n = notifier()
            if action_name == "启动":
                n.container_started(name)
            elif action_name == "停止":
                n.container_stopped(name)
            elif action_name == "重启":
                n.container_restarted(name)
            elif action_name == "删除":
                n.container_deleted(name)
            self.refresh_silent()

        def error(msg: str):
            self._busy = False
            self._set_actions_enabled(True)
            self.status_message.emit(f"{action_name}失败: {msg}")
            QMessageBox.warning(
                self, f"{action_name}失败",
                f"操作 {name} 时出错：\n\n{msg}",
            )
            notifier().op_failed(action_name, name, msg)
            self.refresh_silent()

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in getattr(self, "_tasks", []) if t.alive]
        self._tasks.append(task)

    def _set_actions_enabled(self, enabled: bool) -> None:
        """操作进行中禁用按钮，避免重复触发"""
        for act in (
            self.act_start, self.act_stop, self.act_restart,
            self.act_delete, self.act_refresh,
        ):
            act.setEnabled(enabled)
        if enabled:
            self._update_actions()

    def _start(self) -> None:
        cid = self._current_cid()
        if not cid:
            return

        # 内存阈值预检查
        from core.podman_config import check_start_allowed

        allowed, reason = check_start_allowed(self.client, cid)
        if not allowed:
            name = self._current_name() or cid[:12]
            QMessageBox.warning(self, "内存超限", reason)
            self.status_message.emit(f"{name} 启动被阻止：内存超限")
            notifier().op_failed("启动", name, "内存超限")
            return

        self._do("启动", self.client.start_container, cid)

    def _stop(self) -> None:
        cid = self._current_cid()
        if cid:
            self._do("停止", self.client.stop_container, cid)

    def _restart(self) -> None:
        cid = self._current_cid()
        if cid:
            self._do("重启", self.client.restart_container, cid)

    def _delete(self) -> None:
        cid = self._current_cid()
        name = self._current_name() or cid
        if not cid:
            return
        ret = QMessageBox.question(
            self, "确认删除",
            f"确定要删除容器「{name}」吗？\n\n此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._do("删除", self.client.remove_container, cid, True, False)

    def _open_logs(self) -> None:
        cid = self._current_cid()
        name = self._current_name() or ""
        if cid:
            self.request_logs.emit(cid, name)

    def _open_terminal(self) -> None:
        cid = self._current_cid()
        name = self._current_name() or ""
        if cid:
            self.request_terminal.emit(cid, name)

    # ─────────────────────────────────────────
    # 右键菜单
    # ─────────────────────────────────────────
    def _context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return

        # 端口列（第 3 列）显示专用菜单
        if idx.column() == 3:
            self._port_context_menu(pos)
            return

        m = QMenu(self)
        m.addAction(self.act_start)
        m.addAction(self.act_stop)
        m.addAction(self.act_restart)
        m.addSeparator()
        m.addAction(self.act_terminal)
        m.addAction(self.act_logs)
        m.addSeparator()
        m.addAction(self.act_delete)
        m.exec(self.table.viewport().mapToGlobal(pos))
