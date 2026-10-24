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

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QPainter,
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
from core.vm import dns as vm_dns
from core.vm import manager as vm_manager
from core.vm import memory as vm_memory
from core.vm import network as vm_network
from core.vm import qemu as vm_qemu
from core.vm import ssh as vm_ssh
from resources.icons import ICON_SIZE_TOOLBAR, STATE_COLORS, icon
from ui.utils import fmt_size
from ui.widgets import EmptyState

ROW_HEIGHT = 28

# QEMU 进程的固定内存开销（不受 balloon 影响）：
#   QEMU 基线约 30 MB + OVMF UEFI 固件约 50 MB
# 用于内存条显示上限，避免读数超过 100% 造成困惑。
QEMU_OVERHEAD_MB = 90


# ─────────────────────────────────────────
# 内存条委托
# ─────────────────────────────────────────
class MemoryBarDelegate(QStyledItemDelegate):
    """在单元格内画内存占用条

    显示两段：
      实心段 = 当前实际占用（RSS）
      空槽   = 已分配但未使用（balloon 可回收部分）
    文本叠加在条上，显示 "159 / 1024 MB"
    """

    def __init__(self, page: "VMPage", parent=None):
        super().__init__(parent)
        self.page = page

    def paint(self, painter: QPainter, option, index) -> None:
        data = index.data(Qt.ItemDataRole.UserRole)
        if not data:
            super().paint(painter, option, index)
            return

        used_mb = float(data.get("used_mb", 0))
        total_mb = float(data.get("total_mb", 0)) or 1.0
        guest_mb = float(data.get("guest_mb", 0)) or total_mb
        running = data.get("running", False)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        from core.kde_theme import is_dark_mode

        dark = is_dark_mode()

        r = option.rect.adjusted(6, 6, -6, -6)
        bar_h = min(14.0, r.height())
        bar_y = r.y() + (r.height() - bar_h) / 2.0
        bar = QRectF(float(r.x()), bar_y, float(r.width()), bar_h)

        # 槽背景
        bg = QColor("#3a3d41" if dark else "#dcdfe4")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(bar, bar_h / 2, bar_h / 2)

        if running and total_mb > 0:
            # 压力 = (进程 RSS - QEMU 固定开销) / guest 分配
            # QEMU 进程 RSS 含约 90MB 固定开销（QEMU 基线 + UEFI 固件），
            # 不受 balloon 影响。不剔除的话，刚启动、guest 很空的 VM
            # 也会算出 100% 满格红色，形成误报。
            guest_used = max(0.0, used_mb - QEMU_OVERHEAD_MB)
            ratio = max(0.0, min(1.0, guest_used / max(1.0, guest_mb)))

            # 颜色按压力分级。小内存 VM（<=512MB）阈值更高：
            # 这类 VM 通常只跑轻量服务，页面缓存本身就能轻松吃掉 50%，
            # 用统一阈值会长期显示红色，反而失去告警意义。
            if guest_mb <= 512:
                warn, alarm = 0.80, 0.93
            elif guest_mb <= 2048:
                warn, alarm = 0.70, 0.88
            else:
                warn, alarm = 0.60, 0.82

            if ratio < warn:
                fill = QColor(STATE_COLORS.get("running", "#2ecc71"))
            elif ratio < alarm:
                fill = QColor("#f39c12")
            else:
                fill = QColor("#e74c3c")

            # 选中行时不改色（保持语义），但加亮
            if option.state & QStyle.StateFlag.State_Selected:
                fill = fill.lighter(115)

            w = bar.width() * ratio
            if w > 1:
                filled = QRectF(bar.x(), bar.y(), max(w, bar_h), bar_h)
                painter.setBrush(QBrush(fill))
                painter.drawRoundedRect(filled, bar_h / 2, bar_h / 2)

            if total_mb > guest_mb:
                text = f"{used_mb:.0f} MB 实际 / {guest_mb:.0f} MB 已分配"
            else:
                text = f"{used_mb:.0f} / {total_mb:.0f} MB"
        else:
            text = "已停止"

        # 文本
        f = QFont(option.font)
        f.setPointSizeF(max(7.5, option.font.pointSizeF() - 1.0))
        painter.setFont(f)

        if option.state & QStyle.StateFlag.State_Selected:
            fg = QColor(option.palette.highlightedText().color())
        else:
            fg = QColor(option.palette.text().color())

        # 条内文字用白色（深色条上可读），条外部分用前景色
        painter.setPen(fg)
        painter.drawText(
            option.rect.adjusted(10, 0, -6, 0),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            text,
        )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(s.width(), ROW_HEIGHT)


# ─────────────────────────────────────────
# 虚拟机页
# ─────────────────────────────────────────
class VMPage(QWidget):
    """虚拟机管理页"""

    status_message = pyqtSignal(str)
    # 请求主窗口打开外部终端执行 SSH 命令
    request_terminal = pyqtSignal(object)

    COLUMNS = ["名称", "状态", "内存占用", "CPU / 内存", "磁盘", "SSH", "地址"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._vms: list = []
        self._mem_cache: dict[str, dict] = {}
        # 域名解析是否就绪（后台检测，避免阻塞 UI）
        self._dns_ready = True
        self._dns_checked = False
        # 自动初始化状态（KSM 等需要权限的项）
        self._init_pending = False
        self._busy = False
        self._tasks: list = []

        self._build_ui()
        self.refresh_silent()

        # 内存占用轮询（3s，实时反映 balloon 回收效果）
        self._mem_timer = QTimer(self)
        self._mem_timer.timeout.connect(self._refresh_memory)
        self._mem_timer.start(3000)

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

        self.act_ssh = QAction(icon("terminal"), "SSH", self)
        self.act_ssh.setProperty("iconName", "terminal")
        self.act_ssh.setToolTip("在终端中打开 SSH 连接（外部客户端）")
        self.act_ssh.triggered.connect(self._open_ssh)
        tb.addAction(self.act_ssh)

        self.act_reclaim = QAction(icon("clean"), "回收内存", self)
        self.act_reclaim.setProperty("iconName", "clean")
        self.act_reclaim.setToolTip("立即用 virtio-balloon 回收空闲内存到宿主机")
        self.act_reclaim.triggered.connect(self._reclaim_selected)
        tb.addAction(self.act_reclaim)

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
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        self.table.doubleClicked.connect(lambda _i: self._open_detail())

        hdr = self.table.horizontalHeader()
        hdr.setFixedHeight(26)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(True)
        for i in range(len(self.COLUMNS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 210)
        self.table.setColumnWidth(5, 108)
        self.table.setColumnWidth(2, 210)

        self._mem_delegate = MemoryBarDelegate(self, self.table)
        self.table.setItemDelegateForColumn(2, self._mem_delegate)

        # 空状态叠加
        self._table_stack = QStackedLayout()
        self._table_stack.addWidget(self.table)
        self._empty = EmptyState(
            icon_name="container",
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
        self._refresh_memory()
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

    def _refresh_memory(self) -> None:
        """刷新内存占用（轻量，3s 一次）"""
        changed = False
        for vm in self._vms:
            if vm.spec.name in self._mem_cache:
                old = self._mem_cache[vm.spec.name]
            else:
                old = None
            try:
                u = vm_memory.vm_mem_usage(vm)
                # QEMU 进程 RSS 含固定开销（QEMU 基线约 30MB + UEFI 固件约 50MB），
                # 这部分不受 balloon 影响。用 guest 分配 + 固定开销作为显示上限，
                # 否则会出现「287 / 256 MB」这种让人困惑的读数。
                overhead = QEMU_OVERHEAD_MB if vm.running else 0
                cur = {
                    "used_mb": u.rss_mb,
                    "total_mb": vm.spec.memory_mb + overhead if vm.running else 0,
                    "running": vm.running,
                    "guest_mb": vm.spec.memory_mb if vm.running else 0,
                }
            except Exception:
                cur = {
                    "used_mb": 0, "total_mb": 0,
                    "running": False, "guest_mb": 0,
                }
            if old != cur:
                changed = True
            self._mem_cache[vm.spec.name] = cur

        if changed:
            self._rebuild()

    def _rebuild(self) -> None:
        selected = self._selected_name()

        self.model.removeRows(0, self.model.rowCount())
        total_mem = 0.0

        for vm in self._vms:
            name = vm.spec.name
            mem = self._mem_cache.get(name, {})

            running = mem.get("running", False)
            state = "running" if running else "stopped"

            # 列 0：名称 + 状态图标
            c0 = QStandardItem(name)
            c0.setIcon(icon("container"))
            c0.setData(name, Qt.ItemDataRole.UserRole)

            # 列 1：状态
            c1 = QStandardItem("运行中" if running else "已停止")
            c1.setIcon(state_icon_for(state))

            # 列 2：内存条（委托绘制）
            c2 = QStandardItem("")
            c2.setData(mem, Qt.ItemDataRole.UserRole)
            c2.setTextAlignment(
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
            )

            # 列 3：CPU / 内存配置
            c3 = QStandardItem(f"{vm.spec.cpus} vCPU · {vm.spec.memory_mb} MB")

            # 列 4：磁盘实际占用（disk_usage 返回字节）
            try:
                du = vm_manager.disk_usage(vm)
                c4 = QStandardItem(f"{fmt_size(du)} / {vm.spec.disk_gb} GB")
            except Exception:
                c4 = QStandardItem(f"{vm.spec.disk_gb} GB")

            # 列 5：SSH（只显示端口，完整命令在详情里给）
            c5 = QStandardItem(f"127.0.0.1:{vm.ssh_port}")

            # 列 6：访问地址（域名，VM 内服务可经此访问）
            addr = vm_network.vm_domain(name)
            if running:
                for fwd in vm.spec.forwards:
                    if fwd.guest_port in (80, 443, 3000, 8000, 8080, 5000, 9000):
                        scheme = "https" if fwd.guest_port == 443 else "http"
                        addr = f"{scheme}://{addr}:{fwd.host_port}"
                        break
            c6 = QStandardItem(addr)

            for it in (c0, c1, c2, c3, c4, c5, c6):
                it.setEditable(False)

            self.model.appendRow([c0, c1, c2, c3, c4, c5, c6])

            if running:
                total_mem += mem.get("used_mb", 0.0)

        # 恢复选中
        if selected:
            for r in range(self.model.rowCount()):
                if self.model.item(r, 0).text() == selected:
                    self.table.selectRow(r)
                    break

        for i in (0, 1, 3, 4, 6):
            self.table.resizeColumnToContents(i)

        if not self._vms:
            self._table_stack.setCurrentWidget(self._empty)
        else:
            self._table_stack.setCurrentWidget(self.table)

        # 统计
        running_n = sum(1 for vm in self._vms if vm.running)
        parts = [f"共 {len(self._vms)} 台虚拟机"]
        if running_n:
            parts.append(f"{running_n} 台运行中")
            parts.append(f"VM 内存合计 {total_mem:.0f} MB")
        try:
            hm = vm_memory.host_memory()
            parts.append(f"宿主机可用 {hm.available_mb / 1024:.1f} GB")
        except Exception:
            pass

        # 需要权限的优化项未启用时提示一次（不弹窗，只写在统计栏）
        if not self._dns_ready:
            parts.append(
                f"⚠ *.{vm_dns.DOMAIN_SUFFIX} 域名未启用，"
                "仅能用 127.0.0.1:<端口> 访问"
            )
        elif self._init_pending:
            _, msg = vm_autoconfig.needs_attention()
            if msg:
                parts.append("⚠ " + msg)

        self.lbl_stats.setText(" · ".join(parts))

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

    def _update_actions(self) -> None:
        vm = self._selected_vm()
        has = vm is not None
        running = bool(vm and vm.running)

        self.act_start.setEnabled(has and not running and not self._busy)
        self.act_stop.setEnabled(has and running and not self._busy)
        self.act_ssh.setEnabled(has and running)
        self.act_reclaim.setEnabled(has and running and not self._busy)

    # ─────────────────────────────────────────
    # 操作
    # ─────────────────────────────────────────
    def run_op(self, label: str, fn, *args, busy_text: str = "",
               on_success=None, **kwargs) -> None:
        """后台线程执行（避免冻结 UI）"""
        if self._busy:
            self.status_message.emit("上一个操作还未完成，请稍候…")
            return

        self._busy = True
        self._update_actions()
        self.act_new.setEnabled(False)
        self.status_message.emit(busy_text or f"正在{label}…")

        def work():
            return fn(*args, **kwargs)

        def done(result):
            self._busy = False
            self.act_new.setEnabled(True)
            self.status_message.emit(f"{label}完成")
            if on_success:
                on_success(result)
            self._mem_cache.clear()
            self.refresh_silent()

        def error(msg):
            self._busy = False
            self.act_new.setEnabled(True)
            self.status_message.emit(f"{label}失败: {msg}")
            QMessageBox.warning(self, f"{label}失败", msg)
            notifier().op_failed(label, "", msg)
            self.refresh_silent()

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
        ok, msg = vm_memory.check_memory_before_start(vm.spec.memory_mb)
        if not ok:
            QMessageBox.warning(self, "内存不足", msg)
            return

        self.run_op(
            "启动", vm_manager.start_vm, vm,
            busy_text=f"正在启动 {vm.spec.name}（首次启动需初始化系统）…",
            on_success=lambda r: notifier().info(
                "虚拟机已启动" if r[0] else "启动失败",
                f"{vm.spec.name}\n{r[1]}" if r[1] else vm.spec.name,
            ),
        )

    def _stop_selected(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        ret = QMessageBox.question(
            self, "确认停止",
            f"确定要停止虚拟机「{vm.spec.name}」吗？\n\n"
            "将发送关机信号，超时后强制结束进程。\n"
            "未保存的 guest 内数据可能丢失。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self.run_op(
            "停止", vm_manager.stop_vm, vm,
            busy_text=f"正在停止 {vm.spec.name}…",
            on_success=lambda r: notifier().info(
                "虚拟机已停止" if r[0] else "停止失败", vm.spec.name
            ),
        )

    def _reclaim_selected(self) -> None:
        vm = self._selected_vm()
        if not vm or not vm.running:
            return

        before = self._mem_cache.get(vm.spec.name, {}).get("used_mb", 0)

        def after(r):
            ok, msg = r
            if ok:
                after_mb = vm_memory.vm_mem_usage(vm).rss_mb
                saved = max(0.0, before - after_mb)
                notifier().info(
                    "内存已回收",
                    f"{vm.spec.name}\n{before:.0f} MB → {after_mb:.0f} MB"
                    f"（释放 {saved:.0f} MB）",
                )

        self.run_op(
            "回收内存", vm_memory.balloon_reclaim, vm,
            busy_text=f"正在回收 {vm.spec.name} 的空闲内存…",
            on_success=after,
        )

    def _open_ssh(self) -> None:
        vm = self._selected_vm()
        if not vm or not vm.running:
            return
        # 同时显示命令（方便用户复制到自己的终端）并请求主窗口打开终端
        cmd = vm.ssh_command()
        self.status_message.emit(f"SSH: {cmd}")
        self.request_terminal.emit(vm)

    def _open_detail(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return
        from ui.vm_detail import VMDetailDialog

        dlg = VMDetailDialog(vm, self)
        dlg.exec()
        self.refresh_silent()

    # ─────────────────────────────────────────
    # 右键菜单
    # ─────────────────────────────────────────
    def _context_menu(self, pos) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        menu = QMenu(self)
        running = vm.running

        if not running:
            menu.addAction(icon("play"), "启动", self._start_selected)
        else:
            menu.addAction(icon("stop"), "停止", self._stop_selected)
            menu.addAction(icon("terminal"), "SSH 连接", self._open_ssh)
            menu.addAction(icon("clean"), "回收内存", self._reclaim_selected)

        menu.addSeparator()
        menu.addAction(icon("info"), "详情…", self._open_detail)

        menu.addSeparator()
        act_del = menu.addAction(icon("delete", danger=True), "删除…", self._delete_selected)
        act_del.setProperty("iconName", "delete")

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _delete_selected(self) -> None:
        vm = self._selected_vm()
        if not vm:
            return

        ret = QMessageBox.warning(
            self, "确认删除",
            f"确定要删除虚拟机「{vm.spec.name}」吗？\n\n"
            f"磁盘文件将被删除，占用约 {fmt_mb(vm_manager.disk_usage(vm))}。\n"
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
            on_success=lambda r: notifier().info("虚拟机已删除", vm.spec.name),
        )

    # ─────────────────────────────────────────
    # 主题切换
    # ─────────────────────────────────────────
    def refresh_icons(self) -> None:
        for act, nm in [
            (self.act_new, "add"),
            (self.act_start, "play"),
            (self.act_stop, "stop"),
            (self.act_ssh, "terminal"),
            (self.act_reclaim, "clean"),
        ]:
            act.setIcon(icon(nm))
        self._rebuild()

    def on_disconnected(self) -> None:
        """Podman 断开不影响 VM 管理"""
        pass

    def stop_timers(self) -> None:
        self._mem_timer.stop()
        self._list_timer.stop()



# ─────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────
def state_icon_for(state: str):
    from resources.icons import state_icon

    return state_icon(state, 16)


def fmt_mb(n: float) -> str:
    if n >= 1024:
        return f"{n / 1024:.1f} GB"
    return f"{n:.0f} MB"
