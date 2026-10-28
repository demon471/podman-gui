"""虚拟机页

参照 Dolphin 的列表视图：
- 表格式列表，行高 28px
- 工具栏在顶部（与容器页一致）
- 选中行高亮，危险操作红色图标

内存可视化：
- 表格列绘制「内存条」，直观显示 VM 实际占用 vs 分配上限
- 气球回收后能立刻看到条变短
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedLayout,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.notify import notifier
from core.vm import autoconfig as vm_autoconfig
from core.vm import budget as vm_budget
import time as _time_mod

from core.vm import dns as vm_dns
from core.vm import keepalive as vm_keepalive
from core.vm import manager as vm_manager
from core.vm import network as vm_network
from core.vm import qemu as vm_qemu
from core.vm import ssh as vm_ssh
from resources.icons import ICON_SIZE_TOOLBAR, STATE_COLORS, icon
from ui.utils import fmt_size
from ui.widgets import EmptyState

ROW_HEIGHT = 28



# ─────────────────────────────────────────
# 虚拟机页
# ─────────────────────────────────────────
class VMPage(QWidget):
    """虚拟机管理页"""

    status_message = pyqtSignal(str)
    # 请求主窗口打开外部终端执行 SSH 命令
    request_terminal = pyqtSignal(object)

    COLUMNS = ["名称", "状态", "SSH 地址"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._vms: list = []
        # 域名解析是否就绪（后台检测，避免阻塞 UI）
        self._dns_ready = True
        self._dns_checked = False
        # 自动初始化状态（KSM 等需要权限的项）
        self._init_pending = False
        self._busy = False
        # 正在启动/重启中的 VM 名：行显示 loading、禁用一切交互
        self._pending_vms: set[str] = set()
        self._starting: set[str] = set()      # 区分「启动中」/「重启中」
        self._stopping: set[str] = set()      # 「停止中」文案
        self._mounting: set[str] = set()      # 「目录挂载中」文案
        # loading 图标旋转帧（0-7，对应 8 个角度）
        self._spin_frame = 0
        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(33)   # ~30fps，流畅
        self._spin_timer.timeout.connect(self._spin_tick)
        self._tasks: list = []

        # 常驻保活：空闲超时自动休眠，运行意图保留
        self.keepalive = vm_keepalive.KeepAliveManager(self)
        self.keepalive.auto_slept.connect(self._on_auto_slept)
        self.keepalive.auto_sleep_failed.connect(self._on_auto_sleep_failed)

        self._build_ui()
        self.refresh_silent()
        self._sync_keepalive()

        # 列表状态轮询（5s）
        self._list_timer = QTimer(self)
        self._list_timer.timeout.connect(self.refresh_silent)
        self._list_timer.start(5000)



    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 工具栏 ──
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        self.act_new = QAction(icon("add"), "新建", self)
        self.act_new.setProperty("iconName", "add")
        self.act_new.triggered.connect(self._create)
        tb.addAction(self.act_new)

        self.act_start = QAction(icon("play"), "启动", self)
        self.act_start.setProperty("iconName", "play")
        self.act_start.triggered.connect(self._start_selected)
        tb.addAction(self.act_start)

        self.act_stop = QAction(icon("stop"), "停止", self)
        self.act_stop.setProperty("iconName", "stop")
        self.act_stop.triggered.connect(self._stop_selected)
        tb.addAction(self.act_stop)

        self.act_restart = QAction(icon("restart"), "重启", self)
        self.act_restart.setProperty("iconName", "restart")
        self.act_restart.setToolTip("停止后再启动选中的虚拟机")
        self.act_restart.triggered.connect(self._restart_selected)
        tb.addAction(self.act_restart)

        self.act_ssh = QAction(icon("terminal"), "SSH", self)
        self.act_ssh.setProperty("iconName", "terminal")
        self.act_ssh.setToolTip("在终端中打开 SSH 连接（外部客户端）")
        self.act_ssh.triggered.connect(self._open_ssh)
        tb.addAction(self.act_ssh)

        self.act_purge = QAction(icon("delete", danger=True), "清空所有", self)
        self.act_purge.setProperty("iconName", "delete")
        self.act_purge.setToolTip(
            "删除所有虚拟机及其磁盘，并清理已下载的镜像缓存"
        )
        self.act_purge.triggered.connect(self._purge_all)
        tb.addAction(self.act_purge)

        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().Policy.Expanding,
            spacer.sizePolicy().Policy.Preferred,
        )
        tb.addWidget(spacer)

        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("搜索…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(200)
        self.search.setFixedHeight(26)
        self.search.addAction(
            icon("search"), self.search.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._on_filter)
        tb.addWidget(self.search)

        layout.addWidget(tb)

        # ── 表格 ──
        self.model = QStandardItemModel(0, len(self.COLUMNS), self)
        self.model.setHorizontalHeaderLabels(self.COLUMNS)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableView.Shape.NoFrame)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        # 悬停卡片（不用 QToolTip，见 ui/vm_infocard.py）
        self.table.setMouseTracking(True)
        self.table.viewport().installEventFilter(self)

        # 信息卡片（单例，贴近鼠标显示，可移入卡片内操作复制）
        from ui.vm_infocard import VMInfoCard, SHOW_DELAY_MS

        self._info_card = VMInfoCard(self.window())
        self._card_row = -1

        # 悬停延时：和系统 tooltip 一致，停 500ms 才出现
        self._card_timer = QTimer(self)
        self._card_timer.setSingleShot(True)
        self._card_timer.setInterval(SHOW_DELAY_MS)
        self._card_timer.timeout.connect(self._show_card_now)
        self._pending_row = -1
        self._pending_pos = None

        # 离开表格后延迟收起（给鼠标移入卡片的时间窗口）
        self._leave_timer = QTimer(self)
        self._leave_timer.setSingleShot(True)
        self._leave_timer.setInterval(250)
        self._leave_timer.timeout.connect(self._on_leave_timeout)
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        self.table.doubleClicked.connect(self._on_row_activated)

        hdr = self.table.horizontalHeader()
        hdr.setFixedHeight(26)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(False)

        # 列宽策略：三列均匀分布，平分可用宽度
        #   名称 / 状态 / SSH 地址 各占 1/3，视觉整齐
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # 等宽：各列 initial 相同，Stretch 模式下平分剩余
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 200)

        # 空状态叠加
        self._table_stack = QStackedLayout()
        self._table_stack.addWidget(self.table)
        self._empty = EmptyState(
            icon_name="vm",
            title="还没有虚拟机",
            subtitle=(
                "虚拟机基于 QEMU + KVM，可直接加载各发行版云镜像。\n"
                "默认用户名与宿主机一致，密码 12345，支持 SSH 与端口转发。"
            ),
            action_text="新建虚拟机",
            on_action=self._create,
        )
        self._table_stack.addWidget(self._empty)
        self._table_stack.setCurrentWidget(self.table)
        layout.addLayout(self._table_stack)

        # ── 底部统计 ──
        self.lbl_stats = QLabel("")
        self.lbl_stats.setObjectName("hintLabel")
        layout.addWidget(self.lbl_stats)

        self._update_actions()

    # ─────────────────────────────────────────
    # 数据
    # ─────────────────────────────────────────
    def refresh_silent(self) -> None:
        try:
            self._vms = vm_manager.load_all()
        except Exception as e:
            self.status_message.emit(f"读取虚拟机失败: {e}")
            return
        self._rebuild()
        self._check_dns_once()

    def _check_dns_once(self) -> None:
        """首次刷新时后台检测状态（只做一次）"""
        if self._dns_checked:
            return
        self._dns_checked = True

        def work():
            st = vm_dns.status()
            need, _msg = vm_autoconfig.needs_attention()
            return st.ready, need

        def done(res):
            ready, need = res
            self._dns_ready = bool(ready)
            self._init_pending = bool(need)
            self._rebuild()

        task = run_async(work, on_done=done,
                         on_error=lambda _m: None, parent=self)
        self._tasks.append(task)


    # ─────────────────────────────────────────
    # 悬停信息卡片
    # ─────────────────────────────────────────
    def eventFilter(self, obj, ev) -> bool:
        """悬停行上时调度信息卡片

        显示规则：
          · 悬停行 400ms 后出现，贴近鼠标右下方
          · 鼠标可以移进卡片内（复制按钮可用），不消失
          · 同时离开表格和卡片才收起
        """
        if obj is not self.table.viewport():
            return False

        from PyQt6.QtCore import QEvent

        t = ev.type()

        if t in (QEvent.Type.HoverMove, QEvent.Type.MouseMove):
            # 有弹窗（右键菜单/下拉框）打开时不显示卡片：
            # 弹窗 grab 了鼠标，此时弹卡片只会挡住弹窗
            from PyQt6.QtWidgets import QApplication

            if QApplication.activePopupWidget() is not None:
                self._card_timer.stop()
                self._hide_card()
                return False

            try:
                pos = ev.position().toPoint()
                idx = self.table.indexAt(pos)

                if not idx.isValid():
                    # 表格空白处：若鼠标不朝卡片方向走则收起
                    self._card_timer.stop()
                    self._maybe_hide_for_leave(pos)
                    return False

                # 启动/重启中的行：无悬停效果
                name_item = self.model.item(idx.row(), 0)
                if name_item and name_item.text() in self._pending_vms:
                    self._card_timer.stop()
                    self._hide_card()
                    return False

                # 未启动的虚拟机：不显示信息卡片。
                # 卡片里是 SSH 地址、密码这些「已就绪」才有意义的
                # 信息，机器没跑时显示只会误导。
                if name_item:
                    row_vm = next(
                        (v for v in self._vms
                         if v.spec.name == name_item.text()), None
                    )
                    if row_vm is not None and not self._is_available(row_vm):
                        self._card_timer.stop()
                        self._hide_card()
                        return False

                # 移到不同行 → 重新计时
                if idx.row() != getattr(self, "_pending_row", -1):
                    self._card_timer.stop()
                    self._hide_card()
                    self._pending_row = idx.row()
                    self._pending_pos = pos
                    self._card_timer.start()
                else:
                    # 同一行内移动：更新贴附位置（卡片未显示时才更新目标点）
                    self._pending_pos = pos
                    if self._info_card.isVisible():
                        # 卡片内也在这套坐标里活动，刷新自动消失计时
                        self._info_card._auto_hide.start(8000)
            except Exception:
                pass
            return False

        if t in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            # 离开表格：延迟收起，给鼠标移入卡片的时间窗口
            self._leave_timer.start()
            return False

        if t == QEvent.Type.MouseButtonPress:
            # 按下鼠标（含右键）时收掉卡片和计时：
            # 用户正要点东西，卡片弹出来只会挡视线
            self._card_timer.stop()
            self._hide_card()
            return False

        if t == QEvent.Type.Wheel:
            self._card_timer.stop()
            self._hide_card()
            return False

        return False

    def _maybe_hide_for_leave(self, pos) -> None:
        """鼠标在表格空白处：不立即收起（可能正移向卡片）"""
        # 交给 _leave_timer 的延迟逻辑处理
        self._leave_timer.start()

    def _on_leave_timeout(self) -> None:
        """离开表格的延迟到期：鼠标不在卡片内才真正收起"""
        if getattr(self._info_card, "hovered", False):
            # 鼠标已移入卡片：卡片自己的 leaveEvent 会在
            # 3 秒后自动收起（若期间没回到表格）
            return
        self._hide_card()

    def _show_card_now(self) -> None:
        """延时到期 → 填内容并贴近鼠标显示"""
        row = getattr(self, "_pending_row", -1)
        pos = getattr(self, "_pending_pos", None)
        if row < 0 or pos is None:
            return

        name_item = self.model.item(row, 0)
        if name_item is None:
            return

        vm = next(
            (v for v in self._vms if v.spec.name == name_item.text()), None
        )
        if vm is None:
            return

        self._card_row = row
        self._info_card.show_for(vm, self._is_available(vm))

        g = self.table.viewport().mapToGlobal(pos)
        self._info_card.show_near(g.x(), g.y())

        # 显示后监听卡片自身的 hover（用于「移入卡片不消失」）
        self._info_card.installEventFilter(self)

    def _hide_card(self) -> None:
        self._card_row = -1
        self._pending_row = -1
        if hasattr(self, "_info_card"):
            self._info_card.hide_card()

    # ─────────────────────────────────────────
    # 启动/重启/停止 loading 状态
    # ─────────────────────────────────────────
    def _pending_label(self, name: str) -> str:
        """pending 行的状态文字（启动中 / 重启中 / 停止中）"""
        if name in self._starting:
            return "启动中…"
        if name in self._stopping:
            return "停止中…"
        if name in self._mounting:
            return "目录挂载中…"
        return "重启中…"

    def _spin_icon(self) -> QIcon:
        """旋转 loading 图标（当前帧）

        动画流畅性优化（此前 8 帧 45° 每帧 120ms，卡顿）：
        - 帧数 8 → 30，每帧 12°，角度跳跃从 45° 降到 12°
        - 间隔 120ms → 33ms（约 30fps，与主流 spinner 一致）
        - 帧缓存：每帧的 QPixmap 只渲染一次，之后复用，
          避免 30fps 下反复执行 drawArc（这是之前卡的主因）
        - 带速尾渐变（leading arc 亮，尾部淡出），视觉更顺滑
        """
        # 缓存命中：同帧直接返回，不重画
        cache = getattr(self, "_spin_cache", None)
        if cache is not None and cache[0] == self._spin_frame:
            return cache[1]

        from PyQt6.QtGui import QColor, QPainter, QPixmap

        size = 16
        pm = QPixmap(size * 2, size * 2)   # 2x 高 DPI
        pm.setDevicePixelRatio(2)
        pm.fill(Qt.GlobalColor.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        base_color = self.palette().color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
        )

        margin = 3
        rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)

        # 速尾：沿运动反方向画三段渐淡的短弧，
        # 加上主弧共四段，扫过角度 100°，模拟运动模糊
        base_deg = self._spin_frame * 12          # 每帧 12°
        segments = [
            (0,   70, 255),   # (偏移, 扫角, alpha) 主弧：亮
            (70,  20, 130),   # 尾巴第一段
            (90,  18, 60),    # 尾巴第二段
            (108, 14, 25),    # 尾巴第三段
        ]
        for offset, sweep, alpha in segments:
            c = QColor(base_color)
            c.setAlpha(alpha)
            pen = QPen(c, 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            # Qt 角度逆时针为正，spinner 顺时针转 → 用负角度
            start = -(base_deg + offset) * 16
            p.drawArc(rect, int(start), int(-sweep * 16))
        p.end()

        icon = QIcon(pm)
        self._spin_cache = (self._spin_frame, icon)
        return icon

    def _spin_tick(self) -> None:
        """动画帧推进：只重绘 pending 行的状态列"""
        self._spin_frame = (self._spin_frame + 1) % 30
        for r in range(self.model.rowCount()):
            name = self.model.item(r, 0).text()
            if name in self._pending_vms:
                self.model.item(r, 1).setIcon(self._spin_icon())

    # ─────────────────────────────────────────
    # 常驻保活
    # ─────────────────────────────────────────
    def _sync_keepalive(self) -> None:
        """把当前 VM 列表同步给保活管理器

        启动意图从 SQLite 读回（应用重启后仍知道哪些该运行），
        并且只对「确实在跑」的 VM 开始计时。
        """
        from core import state as _state

        for vm in self._vms:
            name = vm.spec.name
            try:
                want = _state.get_setting(f"vm.desired_running.{name}", False)
            except Exception:
                want = False

            if want:
                self.keepalive.note_started(vm)
                if vm.running:
                    self.keepalive.note_ready(vm)
            elif not vm.running:
                self.keepalive.note_stopped(vm)

        # 应用启动时，仍在休眠的 VM 需要重新建立唤醒代理
        # （代理随上个进程退出而释放了端口）
        self.keepalive._sync_proxies()

        # 上一轮可能因端口 TIME_WAIT 失败。应用刚启动时事件循环
        # 尚未稳定，QTimer 重试不一定及时 —— 这里额外排几轮
        # 主动补偿，确保休眠中的 VM 一定有代理在监听，
        # 否则用户 ssh 会直接「连接被拒绝」。
        self._ensure_proxies_ready(attempt=0)

    def _ensure_proxies_ready(self, attempt: int = 0) -> None:
        """确保所有「休眠中且应运行」的 VM 都有代理在监听

        最多重试 10 次（每次间隔递增），足够覆盖 QEMU 退出后
        端口的 TIME_WAIT 释放窗口。
        """
        if attempt > 10:
            return

        need = [
            vm for vm in self._vms
            if not vm.running
            and self.keepalive.enabled
            and self.keepalive.wants_running(vm.spec.name)
        ]
        if not need:
            return

        pending = []
        for vm in need:
            if vm.spec.name in self.keepalive.proxied_vms():
                continue
            ok, _err = self.keepalive._pool.ensure(vm, retries=1)
            if not ok:
                pending.append(vm)

        if pending:
            delay = 500 + attempt * 500      # 0.5s, 1s, 1.5s …
            QTimer.singleShot(
                delay,
                lambda: self._ensure_proxies_ready(attempt + 1),
            )

    def _on_auto_slept(self, name: str) -> None:
        """空闲资源回收完成

        对用户完全静默：机器在用户看来始终可用，
        底层释放内存属于实现细节，不该弹通知打扰。
        """
        self.refresh_silent()

    def _on_auto_sleep_failed(self, name: str, err: str) -> None:
        """资源回收失败：只记状态栏，不弹窗（不影响用户使用）"""
        self.status_message.emit(f"{name} 资源回收未完成")

    def _rebuild(self) -> None:
        selected = self._selected_name()

        self.model.removeRows(0, self.model.rowCount())

        for vm in self._vms:
            name = vm.spec.name
            running = vm.running

            # 该 VM 是否正在启动/重启（等待后台操作完成）
            pending = name in self._pending_vms
            if pending:
                state = "pending"
                state_text = self._pending_label(name)
            elif running:
                state = "running"
                state_text = "运行中"
            elif self.keepalive.wants_running(name) and self.keepalive.enabled:
                # 用户视角：这台机器「随时可用」。
                # 底层可能是内存快照待恢复（1 秒级）或已驻留内存，
                # 对用户来说没有区别，所以统一显示为就绪，
                # 不暴露「休眠/倒计时/内存状态」这些实现细节。
                state = "running"
                state_text = "就绪"
            else:
                state = "stopped"
                state_text = "已停止"

            # 列 0：名称
            c0 = QStandardItem(name)
            c0.setIcon(icon("vm"))
            c0.setData(name, Qt.ItemDataRole.UserRole)

            # 列 1：状态
            c1 = QStandardItem(state_text)
            if pending:
                c1.setIcon(self._spin_icon())
            else:
                c1.setIcon(state_icon_for(state))

            # 列 2：SSH 地址（域名，端口由 ~/.ssh/config 自动映射）
            c2 = QStandardItem(vm_dns.vm_domain(name))

            for it in (c0, c1, c2):
                it.setEditable(False)
                # 不用 setToolTip：Qt 的 QToolTip HTML 子集太弱，
                # 做不出卡片样式。改用自定义卡片（见 _show_info_card）。
                if pending:
                    # 整行灰色：禁止一切交互的视觉信号
                    it.setForeground(
                        self.palette().color(
                            QPalette.ColorGroup.Disabled,
                            QPalette.ColorRole.Text,
                        )
                    )

            if pending:
                # 该行不可选中（右键/双击/详情全部无从触发）
                c0.setFlags(c0.flags() & ~Qt.ItemFlag.ItemIsSelectable)

            self.model.appendRow([c0, c1, c2])

        # 恢复选中
        if selected:
            for r in range(self.model.rowCount()):
                if self.model.item(r, 0).text() == selected:
                    self.table.selectRow(r)
                    break

        for i in range(len(self.COLUMNS)):
            self.table.resizeColumnToContents(i)

        if not self._vms:
            self._table_stack.setCurrentWidget(self._empty)
        else:
            self._table_stack.setCurrentWidget(self.table)

        # 统计：只报数量，不暴露内存/磁盘/休眠等技术细节
        ready_n = sum(
            1 for vm in self._vms
            if vm.running
            or (self.keepalive.wants_running(vm.spec.name) and self.keepalive.enabled)
        )
        if ready_n:
            self.lbl_stats.setText(
                f"共 {len(self._vms)} 台虚拟机 · {ready_n} 台运行中"
            )
        else:
            self.lbl_stats.setText(f"共 {len(self._vms)} 台虚拟机")

        self._update_actions()

    def _on_filter(self, text: str) -> None:
        t = text.strip().lower()
        for r in range(self.model.rowCount()):
            name = self.model.item(r, 0).text().lower()
            self.table.setRowHidden(r, bool(t) and t not in name)

    # ─────────────────────────────────────────
    # 选中状态
    # ─────────────────────────────────────────
    def _selected_name(self) -> str:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return ""
        item = self.model.item(idx.row(), 0)
        return item.text() if item else ""

    def _selected_vm(self):
        name = self._selected_name()
        if not name:
            return None
        return next((v for v in self._vms if v.spec.name == name), None)

    def _selected_vms(self) -> list:
        vm = self._selected_vm()
        return [vm] if vm else []

    def _is_available(self, vm) -> bool:
        """对用户而言这台机器是否「可用」

        包含两种情况：
          · 真的在运行
          · 资源已释放（内存快照待恢复），访问时 1 秒级恢复

        用户视角两者没区别 —— 都能连上、都能用，
        所以操作按钮按「可用」启用，而不是按进程状态。
        """
        if vm is None:
            return False
        if vm.running:
            return True
        return bool(
            self.keepalive.enabled
            and self.keepalive.wants_running(vm.spec.name)
        )

    def _update_actions(self) -> None:
        vm = self._selected_vm()
        has = vm is not None
        running = bool(vm and vm.running)
        available = self._is_available(vm)
        # 选中行正在启动/重启 → 全部动作禁用
        pending_sel = bool(vm and vm.spec.name in self._pending_vms)

        # 「启动」只在真需要冷启动时可用（未运行且没登记运行意图）
        self.act_start.setEnabled(
            has and not available and not self._busy and not pending_sel
        )
        self.act_stop.setEnabled(
            has and available and not self._busy and not pending_sel
        )
        self.act_restart.setEnabled(
            has and available and not self._busy and not pending_sel
        )
        # SSH：可用即可（资源已释放时点它会在后台恢复，用户感知不到）
        self.act_ssh.setEnabled(
            has and available and not pending_sel
        )
        self.act_purge.setEnabled(
            bool(self._vms) and not self._busy and not pending_sel
        )

    # ─────────────────────────────────────────
    # 操作
    # ─────────────────────────────────────────
    def run_op(self, label: str, fn, *args, busy_text: str = "",
               on_success=None, pending_vm: str | None = None,
               release_when=None, **kwargs) -> None:
        """后台线程执行（避免冻结 UI）

        pending_vm：该操作针对某台 VM 的启动/重启/停止。
        该行进入 loading 态（灰字 + 转圈 + 禁止交互）。

        release_when：可选的异步解除条件。
          - 启动/重启：传 "ssh" —— qemu 起来后继续探针，
            直到 SSH 可连才解除 loading（启动成功的真正标准）
          - 停止：传 None —— stop_vm 返回即解除
            （QEMU 进程消失 = 不可操作，无需探针）
        """
        if self._busy:
            self.status_message.emit("上一个操作还未完成，请稍候…")
            return

        self._busy = True
        self._update_actions()
        self.act_new.setEnabled(False)
        self.status_message.emit(busy_text or f"正在{label}…")

        # 行级 loading
        if pending_vm:
            self._pending_vms.add(pending_vm)
            if label == "启动":
                self._starting.add(pending_vm)
            elif label == "停止":
                self._stopping.add(pending_vm)
            elif label == "挂载":
                self._mounting.add(pending_vm)
            self._spin_timer.start()
            self._hide_card()
            self._rebuild()

        def work():
            return fn(*args, **kwargs)

        def _release(name: str):
            self._pending_vms.discard(name)
            self._starting.discard(name)
            self._stopping.discard(name)
            self._mounting.discard(name)
            if not self._pending_vms:
                self._spin_timer.stop()

        def done(result):
            self._busy = False
            self.act_new.setEnabled(True)

            # 状态栏文案要说实话：
            # · release_when == "ssh" 时，这一步只代表 qemu 进程起来了，
            #   guest 还在启动 —— 说「完成」会让用户以为已经能用，
            #   而列表还显示「启动中…」，两边对不上。
            # · 其他情况（停止/删除）此时确实完成了。
            if release_when == "ssh":
                ok_now = bool(result and result[0])
                if ok_now:
                    self.status_message.emit(f"{label}中，等待系统就绪…")
                else:
                    self.status_message.emit(f"{label}失败")
            else:
                self.status_message.emit(f"{label}完成")

            if on_success:
                on_success(result)
            self.refresh_silent()

            if not pending_vm:
                return

            if release_when == "ssh":
                # 启动/重启：qemu 起来后继续等 guest 就绪
                self._probe_ssh_then_release(pending_vm)
            else:
                # 停止：QEMU 进程已退出，直接解除
                _release(pending_vm)
                self._rebuild()

        def error(msg):
            self._busy = False
            self.act_new.setEnabled(True)
            self.status_message.emit(f"{label}失败: {msg}")
            if pending_vm:
                _release(pending_vm)
                self._rebuild()
            QMessageBox.warning(self, f"{label}失败", msg)
            notifier().op_failed(label, "", msg)
            self.refresh_silent()

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def _probe_ssh_then_release(self, name: str,
                                timeout_s: int = 180) -> None:
        """等待虚拟机就绪，然后解除该行的 loading

        「就绪」的判定：TCP 能连上 SSH 端口并读到 SSH banner。

        为什么不用 `ssh ... echo ok` 探测：
        · 完整 SSH 握手要 0.1–8 秒（密钥交换 + 认证）
        · VM 还在启动时，每次探测都要等满超时才返回，
          180 秒里只能试十几次，很容易误判「启动失败」
        · 同时 GUI 的保活模块也在轮询 SSH，两个客户端抢
          sshd 的 MaxStartups，会互相把对方挤掉

        纯 TCP 探测只要 10 毫秒，可以密集轮询（每 0.5 秒），
        既准确又快，也不会给 sshd 造成压力。
        """
        def work():
            import socket

            vm = vm_manager.load(name)
            if vm is None:
                return name, False, "虚拟机已不存在"

            port = vm.ssh_port
            if not port:
                return name, False, "未配置 SSH 端口"

            t0 = _time_mod.monotonic()
            last_err = ""

            while _time_mod.monotonic() - t0 < timeout_s:
                # QEMU 进程没了 → 明确失败，不用等满超时
                if not vm.running:
                    last_err = "虚拟机进程已退出"
                    break

                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1.5)
                    s.connect(("127.0.0.1", port))
                    # 读到 banner 才算 sshd 真的在服务
                    banner = s.recv(64)
                    s.close()
                    if b"SSH-" in banner:
                        elapsed = _time_mod.monotonic() - t0
                        return name, True, f"{elapsed:.0f}"
                except OSError as e:
                    last_err = str(e)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass

                _time_mod.sleep(0.5)

            return name, False, last_err or f"{timeout_s}s 内未就绪"

        def done(res):
            name, ok, detail = res
            self._pending_vms.discard(name)
            self._starting.discard(name)
            self._stopping.discard(name)
            if not self._pending_vms:
                self._spin_timer.stop()

            # 先刷新列表（让状态立刻变成「运行中」），再发通知。
            # 反过来的话，通知弹出来时列表还显示「启动中…」，
            # 用户会觉得两边对不上。
            self.refresh_silent()

            if ok:
                # 保活空闲计时从这里开始
                vm = next((v for v in self._vms if v.spec.name == name), None)
                if vm is not None:
                    self.keepalive.note_ready(vm)
                    # 自动挂载共享目录。
                    # QEMU 的 -virtfs 只是把目录暴露成 9p 设备，
                    # guest 内还要 mount 才能访问 —— 之前没有任何
                    # 地方做这一步，用户配了共享目录也看不到内容。
                    self._mount_shares_async(vm)
                self.status_message.emit(f"{name} 已就绪（{detail}s）")
                notifier().info("虚拟机已就绪", name)
            else:
                self.status_message.emit(f"{name} 启动失败: {detail}")
                notifier().op_failed("启动", name, detail)

        def error(msg):
            self._pending_vms.discard(name)
            self._starting.discard(name)
            self._stopping.discard(name)
            if not self._pending_vms:
                self._spin_timer.stop()
            self.refresh_silent()

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def _mount_shares_async(self, vm) -> None:
        """后台挂载该 VM 的全部共享目录（不阻塞 UI）"""
        if not vm.spec.shares:
            return

        def work():
            return vm_ssh.mount_all_shares(vm)

        def done(results):
            ok_n = sum(1 for _t, ok, _m in results if ok)
            total = len(results)
            if ok_n == total:
                self.status_message.emit(
                    f"{vm.spec.name} 共享目录已挂载（{ok_n} 个）"
                )
            else:
                failed = [t for t, ok, _m in results if not ok]
                self.status_message.emit(
                    f"{vm.spec.name} 共享目录 {ok_n}/{total} 挂载成功，"
                    f"失败：{', '.join(failed)}"
                )

        def error(msg):
            self.status_message.emit(f"共享目录挂载失败: {msg}")

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def _create(self) -> None:
        from ui.vm_wizard import VMWizard

        wiz = VMWizard(self)
        if wiz.exec() == QDialog.DialogCode.Accepted and wiz.created_vm:
            self.status_message.emit(f"已创建虚拟机 {wiz.created_vm.spec.name}")
            notifier().info("虚拟机已创建", wiz.created_vm.spec.name)
            self.refresh_silent()
            # 选中新建的
            for r in range(self.model.rowCount()):
                if self.model.item(r, 0).text() == wiz.created_vm.spec.name:
                    self.table.selectRow(r)
                    break

    def _start_selected(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        # 启动前内存预检查
        ok, msg = vm_budget.check_vm_start(None, vm.spec.memory_mb)
        if not ok:
            QMessageBox.warning(self, "内存不足", msg)
            return

        # 有内存快照就走恢复（1–2 秒），否则冷启动（十几秒）。
        # resume_vm 内部会自动判断，这里统一用它。
        self.run_op(
            "启动", vm_manager.resume_vm, vm,
            pending_vm=vm.spec.name,
            release_when="ssh",
            busy_text=f"正在启动 {vm.spec.name}…",
            on_success=lambda r: self._after_start(vm, r),
        )

    def _after_start(self, vm, result) -> None:
        """QEMU 进程已启动（但 guest 还没就绪）

        这里**不发「已启动」通知** —— 此时只是 qemu 进程起来了，
        guest 里的系统还在启动，SSH 还连不上。列表行仍然是
        「启动中…」，探测任务会继续等就绪。

        真正的完成通知由 _probe_ssh_then_release 在探测成功后发，
        这样通知与列表状态始终一致。
        """
        ok = bool(result and result[0])
        if not ok:
            # 失败要立刻告诉用户，不用等探测
            err = (result[1] if result and len(result) > 1 else "") or ""
            notifier().op_failed("启动", vm.spec.name, err)
            return

        # 运行意图：保活机制据此判断「可以自动休眠，也要自动恢复」
        self.keepalive.note_started(vm)
        from core import state as _state

        try:
            _state.set_setting(f"vm.desired_running.{vm.spec.name}", True)
        except Exception:
            pass

    def _stop_selected(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        ret = QMessageBox.question(
            self, "确认停止",
            f"确定要停止虚拟机「{vm.spec.name}」吗？\n\n"
            + ("这台虚拟机当前已释放内存，停止后需要手动启动才会恢复。"
               if not vm.running else
               "将发送关机信号，超时后强制结束进程。\n"
               "未保存的 guest 内数据可能丢失。"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self.run_op(
            "停止", vm_manager.stop_vm, vm,
            pending_vm=vm.spec.name,
            busy_text=f"正在停止 {vm.spec.name}…",
            on_success=lambda r: self._after_stop(vm, r),
        )

    def _after_stop(self, vm, result) -> None:
        """停止结果处理：清除保活意图（用户主动停止，不再自动拉起）"""
        ok = bool(result and result[0])
        notifier().info(
            "虚拟机已停止" if ok else "停止失败", vm.spec.name
        )
        if ok:
            self.keepalive.note_stopped(vm)

    def _restart_selected(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        if not vm.running:
            QMessageBox.information(
                self, "未运行", f"「{vm.spec.name}」未运行，直接启动即可。"
            )
            return

        ret = QMessageBox.question(
            self, "确认重启",
            f"确定要重启虚拟机「{vm.spec.name}」吗？\n\n"
            "将先停止再启动，未保存的 guest 内数据可能丢失。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self.run_op(
            "重启", vm_manager.restart_vm, vm,
            pending_vm=vm.spec.name,
            release_when="ssh",
            busy_text=f"正在重启 {vm.spec.name}…（直到 SSH 可连）",
            on_success=lambda r: self._after_restart(vm, r),
        )

    def _after_restart(self, vm, result) -> None:
        ok = bool(result and result[0])
        notifier().info(
            "虚拟机已重启" if ok else "重启失败", vm.spec.name
        )
        if ok:
            self.keepalive.note_started(vm)
            self.keepalive.note_ready(vm)

    def _open_ssh(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        # 资源已释放 → 后台恢复，用户不需要知道发生了什么
        if not vm.running:
            if self.keepalive.needs_wake(vm.spec.name):
                self.status_message.emit(f"{vm.spec.name} 正在连接…")
                self._wake_and_open(vm)
                return
            QMessageBox.information(
                self, "未运行",
                f"「{vm.spec.name}」当前未运行。\n\n"
                "点击工具栏「启动」即可。",
            )
            return

        self.keepalive.note_activity(vm.spec.name)
        cmd = vm.ssh_command()
        self.status_message.emit(f"SSH: {cmd}")
        self.request_terminal.emit(vm)

    def _wake_and_open(self, vm) -> None:
        """恢复已释放资源的 VM，就绪后自动打开 SSH"""
        self.run_op(
            "恢复", vm_manager.resume_vm, vm,
            pending_vm=vm.spec.name,
            release_when="ssh",
            busy_text=f"正在连接 {vm.spec.name}…",
            on_success=lambda r: self._after_wake(vm, r),
        )

    def _after_wake(self, vm, result) -> None:
        ok = bool(result and result[0])
        if not ok:
            notifier().op_failed("恢复", vm.spec.name, result[1] if result else "")
            return
        self.keepalive.note_ready(vm)
        # 唤醒完成后直接打开 SSH，用户感知不到中间状态
        QTimer.singleShot(0, lambda: self._open_ssh_now(vm))

    def _open_ssh_now(self, vm) -> None:
        """SSH 就绪后打开终端"""
        fresh = vm_manager.load(vm.spec.name) or vm
        if not fresh.running:
            # 极少数情况：恢复后进程又退出了，给个提示而不是静默失败
            QMessageBox.information(
                self, "连接失败",
                f"「{vm.spec.name}」未能启动，请查看控制台日志。",
            )
            return
        cmd = fresh.ssh_command()
        self.status_message.emit(f"SSH: {cmd}")
        self.request_terminal.emit(fresh)

    def _on_row_activated(self, index) -> None:
        """双击行：打开详情（仅限已启动的虚拟机）

        未启动的机器不响应双击：
        详情页里的文件、控制台、共享目录都依赖 guest 在运行，
        进去也是一片空白。此时用户该做的是先「启动」。
        """
        vm = self._selected_vm()
        if vm is None:
            return
        if not self._is_available(vm):
            self.status_message.emit(
                f"{vm.spec.name} 未启动，请先启动后再查看详情"
            )
            return
        self._open_detail()

    def _wake_then(self, vm, action) -> None:
        """唤醒 VM，就绪后执行 action"""
        def done(r):
            if r and r[0]:
                self.keepalive.note_ready(vm)
                QTimer.singleShot(0, action)
            else:
                notifier().op_failed(
                    "恢复", vm.spec.name, (r[1] if r else "")
                )

        self.run_op(
            "恢复", vm_manager.resume_vm, vm,
            pending_vm=vm.spec.name,
            release_when="ssh",
            busy_text=f"正在恢复 {vm.spec.name}…",
            on_success=done,
        )

    # 共享目录 tab 在详情页里的索引
    DETAIL_TAB_SHARES = 1

    def _open_detail(self, initial_tab: int | None = None) -> None:
        """打开详情对话框

        initial_tab 用于「重启并挂载」流程结束后自动回到共享目录页。
        """
        vm = self._selected_vm()
        if not vm:
            return

        # 启动/重启中的行：不可打开
        if vm.spec.name in self._pending_vms:
            return

        from ui.vm_detail import VMDetailDialog

        dlg = VMDetailDialog(vm, self, initial_tab=initial_tab)
        dlg.restart_and_mount_requested.connect(self._restart_and_mount)
        dlg.exec()
        self.refresh_silent()

    def _restart_and_mount(self, name: str) -> None:
        """重启虚拟机并挂载共享目录（详情页发起的请求）

        流程：
          1. 详情页已关闭
          2. 列表该行进入 loading（「目录挂载中…」）
          3. 后台：重启 → 等 SSH 就绪 → 挂载共享
          4. 完成后自动重新打开详情页，定位到共享目录 tab
        """
        vm = vm_manager.load(name)
        if vm is None:
            return

        def work():
            ok, err = vm_manager.restart_vm(vm)
            if not ok:
                return False, f"重启失败: {err}"

            fresh = vm_manager.load(name)
            if fresh is None:
                return False, "重启后找不到虚拟机"

            # 等 guest 真正起来（restart_vm 只保证 QEMU 进程）
            deadline = _time_mod.monotonic() + 120
            ready = False
            while _time_mod.monotonic() < deadline:
                o, _ = vm_ssh.test_connection(fresh, timeout=5)
                if o:
                    ready = True
                    break
                _time_mod.sleep(2)

            if not ready:
                return False, "重启后 SSH 未就绪，共享目录未挂载"

            if fresh.spec.shares:
                results = vm_ssh.mount_all_shares(fresh)
                failed = [t for t, ok2, _m in results if not ok2]
                if failed:
                    return True, f"已重启，但 {', '.join(failed)} 挂载失败"
            return True, ""

        def done(res):
            ok, detail = res if isinstance(res, tuple) else (False, "")
            if ok:
                self.status_message.emit(f"{name} {detail or '共享目录已挂载'}")
                notifier().info("共享目录已挂载", name)
                # 自动重新打开详情，定位到共享目录
                QTimer.singleShot(0, lambda: self._reopen_detail_shares(name))
            else:
                self.status_message.emit(f"{name} {detail}")
                notifier().op_failed("挂载共享目录", name, detail)

        self.run_op(
            "挂载", work,
            pending_vm=name,
            busy_text=f"正在重启 {name} 并挂载共享目录…",
            on_success=done,
        )

    def _reopen_detail_shares(self, name: str) -> None:
        """重新打开详情页并定位到共享目录 tab"""
        for r in range(self.model.rowCount()):
            if self.model.item(r, 0).text() == name:
                self.table.selectRow(r)
                break
        self._open_detail(initial_tab=self.DETAIL_TAB_SHARES)

    # ─────────────────────────────────────────
    # 右键菜单
    # ─────────────────────────────────────────
    def _context_menu(self, pos) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        # 启动/重启中的行：不做任何操作
        if vm.spec.name in self._pending_vms:
            return

        # 右键菜单与信息卡片冲突：菜单弹出前必须收掉卡片，
        # 否则卡片压在菜单上面挡住菜单项。
        # exec() 是阻塞的事件循环，期间 viewport 收不到
        # MouseMove，卡片不会自己消失。
        self._card_timer.stop()
        self._hide_card()

        menu = QMenu(self)
        available = self._is_available(vm)

        if not available:
            menu.addAction(icon("play"), "启动", self._start_selected)
        else:
            menu.addAction(icon("stop"), "停止", self._stop_selected)
            menu.addAction(icon("restart"), "重启", self._restart_selected)
            menu.addAction(icon("terminal"), "SSH 连接", self._open_ssh)

            # 详情页里的文件 / 控制台 / 共享目录都依赖 guest 在运行，
            # 未启动时进去是空的 —— 所以这一项只在可用时出现。
            menu.addSeparator()
            # 用 lambda 包一层：QAction.triggered 会传 bool，
            # 直接连 _open_detail 会被当成 initial_tab 参数
            menu.addAction(
                icon("info"), "详情…", lambda: self._open_detail()
            )

        menu.addSeparator()
        act_del = menu.addAction(icon("delete", danger=True), "删除…", self._delete_selected)
        act_del.setProperty("iconName", "delete")

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _purge_all(self) -> None:
        """一键清空：删除所有 VM + 镜像缓存"""
        from core.vm import images as vm_images

        try:
            vm_bytes, img_bytes = vm_manager.vm_data_size()
        except Exception:
            vm_bytes = img_bytes = 0

        vm_count = len(self._vms)
        img_count = sum(
            1 for spec in vm_images.list_images()
            if vm_images.is_downloaded(spec)
        )

        ret = QMessageBox.warning(
            self,
            "清空所有虚拟机",
            f"将删除以下内容，且无法恢复：\n\n"
            f"  · {vm_count} 台虚拟机（含各自磁盘和配置）\n"
            f"  · {img_count} 个已下载的镜像缓存"
            f"（{fmt_size(img_bytes)}，之后可重新下载）\n"
            f"  · 共享磁盘基础\n\n"
            f"合计释放约 {fmt_size(vm_bytes + img_bytes)}。\n\n"
            "确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        # 二次确认（不可恢复操作）
        ret2 = QMessageBox.warning(
            self,
            "再次确认",
            "此操作不可恢复，虚拟机内未备份的数据将永久丢失。\n\n"
            "确定要清空吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret2 != QMessageBox.StandardButton.Yes:
            return

        def work():
            return vm_manager.purge_all(include_images=True)

        def done(res):
            count, freed, err = res
            self._mem_cache.clear()
            self._vms = []
            self._rebuild()
            if err:
                QMessageBox.warning(
                    self, "部分清理失败",
                    f"已删除 {count} 台虚拟机，释放 {fmt_size(freed)}。\n\n"
                    f"以下项清理失败：\n{err}",
                )
            else:
                QMessageBox.information(
                    self, "已清空",
                    f"已删除 {count} 台虚拟机，释放 {fmt_size(freed)}。",
                )
                self.status_message.emit(f"已清空虚拟机，释放 {fmt_size(freed)}")

        self.run_op(
            "清空", work, busy_text="正在删除所有虚拟机…",
            on_success=done,
        )

    def _on_vm_deleted(self, name: str) -> None:
        """VM 删除后清掉保活记录与运行意图"""
        self.keepalive.forget(name)
        try:
            from core import state as _state

            _state.delete_setting(f"vm.desired_running.{name}")
        except Exception:
            pass

    def _delete_selected(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        ret = QMessageBox.warning(
            self, "确认删除",
            f"确定要删除虚拟机「{vm.spec.name}」吗？\n\n"

            "此操作不可恢复！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        if vm.running:
            QMessageBox.information(
                self, "请先停止",
                "虚拟机正在运行，请先停止后再删除。",
            )
            return

        self.run_op(
            "删除", vm_manager.delete_vm, vm,
            busy_text=f"正在删除 {vm.spec.name}…",
            on_success=lambda r: self._after_delete(vm, r),
        )

    def _after_delete(self, vm, result) -> None:
        ok = bool(result and result[0])
        notifier().info(
            "虚拟机已删除" if ok else "删除失败",
            vm.spec.name if ok else (result[1] if result else ""),
        )
        if ok:
            self._on_vm_deleted(vm.spec.name)

    # ─────────────────────────────────────────
    # 主题切换
    # ─────────────────────────────────────────
    def refresh_icons(self) -> None:
        for act, nm in [
            (self.act_new, "add"),
            (self.act_start, "play"),
            (self.act_stop, "stop"),
            (self.act_restart, "restart"),
            (self.act_ssh, "terminal"),
            (self.act_purge, "delete"),
        ]:
            act.setIcon(icon(nm, danger=(nm == "delete")))
        self._rebuild()

    def on_disconnected(self) -> None:
        """Podman 断开不影响 VM 管理"""
        self._hide_card()

    def hideEvent(self, e) -> None:
        self._hide_card()
        super().hideEvent(e)

    def stop_timers(self) -> None:
        self._list_timer.stop()
        self.keepalive.shutdown()



# ─────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────
def state_icon_for(state: str):
    from resources.icons import state_icon

    return state_icon(state, 16)

