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
from core.vm import budget as vm_budget
from core.vm import dns as vm_dns
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

    COLUMNS = ["名称", "状态", "SSH 地址", "端口转发"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._vms: list = []
        # 域名解析是否就绪（后台检测，避免阻塞 UI）
        self._dns_ready = True
        self._dns_checked = False
        # 自动初始化状态（KSM 等需要权限的项）
        self._init_pending = False
        self._busy = False
        self._tasks: list = []

        self._build_ui()
        self.refresh_silent()

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

        self.act_more = QAction(icon("more"), "更多", self)
        self.act_more.setProperty("iconName", "more")
        self.act_more.setToolTip("打开虚拟机管理台（详情、文件、端口、控制台）")
        self.act_more.triggered.connect(self._open_detail)
        tb.addAction(self.act_more)

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
        self.table.selectionModel().selectionChanged.connect(self._update_actions)
        self.table.doubleClicked.connect(lambda _i: self._open_detail())

        hdr = self.table.horizontalHeader()
        hdr.setFixedHeight(26)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(False)

        # 列宽策略（与容器页一致）：
        #   名称   固定窄，多数 VM 名很短，不占空间
        #   状态   按内容（「运行中」+ 图标）
        #   SSH    拉伸占满剩余 —— 域名长度不一，给它弹性空间
        #   端口   按内容
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 140)   # 名称
        self.table.setColumnWidth(1, 96)    # 状态
        self.table.setColumnWidth(3, 200)   # 端口转发

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


    def _rebuild(self) -> None:
        selected = self._selected_name()

        self.model.removeRows(0, self.model.rowCount())

        for vm in self._vms:
            name = vm.spec.name
            running = vm.running
            state = "running" if running else "stopped"

            # 列 0：名称
            c0 = QStandardItem(name)
            c0.setIcon(icon("container"))
            c0.setData(name, Qt.ItemDataRole.UserRole)

            # 列 1：状态
            c1 = QStandardItem("运行中" if running else "已停止")
            c1.setIcon(state_icon_for(state))

            # 列 2：SSH 地址（域名，端口由 ~/.ssh/config 自动映射）
            c2 = QStandardItem(vm_dns.vm_domain(name))

            # 列 3：端口转发（只列非 SSH 的，避免和 SSH 列重复）
            #     显示「宿主:guest」，多了才省略
            others = [f for f in vm.spec.forwards if f.guest_port != 22]
            if others:
                if len(others) <= 2:
                    c3 = QStandardItem(
                        "  ".join(
                            f"{f.host_port}→{f.guest_port}"
                            for f in others
                        )
                    )
                else:
                    c3 = QStandardItem(
                        f"{others[0].host_port}→{others[0].guest_port} "
                        f"等 {len(others)} 项"
                    )
            else:
                c3 = QStandardItem("—")

            for it in (c0, c1, c2, c3):
                it.setEditable(False)

            # 悬停提示：默认账户、密码、SSH、端口
            tip = self._vm_tooltip(vm, running)
            for it in (c0, c1, c2, c3):
                it.setToolTip(tip)

            self.model.appendRow([c0, c1, c2, c3])

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

        # 统计：只报数量，不暴露内存/磁盘等技术细节
        running_n = sum(1 for vm in self._vms if vm.running)
        if running_n:
            self.lbl_stats.setText(
                f"共 {len(self._vms)} 台虚拟机 · {running_n} 台运行中"
            )
        else:
            self.lbl_stats.setText(f"共 {len(self._vms)} 台虚拟机")

        self._update_actions()

    def _vm_tooltip(self, vm, running: bool) -> str:
        """行悬停提示（HTML 卡片式）

        纯文本换行拼接在 Qt 里会渲染成等宽单色块，键值对不齐、
        层次不清。改用 HTML 表格：左列标签固定宽度，视觉上对齐。

        密码标注「初始密码」而非「密码」——用户改过后宿主机读不到
        guest 内的真实密码，避免误导。
        """
        from core.vm.cloudinit import DEFAULT_PASSWORD

        spec = vm.spec
        domain = vm_dns.vm_domain(spec.name)

        # ── 分隔线 ──
        sep = ('<hr style="border:none;border-top:1px solid '
               'rgba(128,128,128,0.3);margin:6px 0;">')

        # ── 键值表 ──
        rows = [
            ("系统", spec.image_key),
            ("账户", spec.username),
            ("初始密码", DEFAULT_PASSWORD),
        ]
        rows_html = "".join(
            '<tr><td style="padding:1px 14px 1px 0;opacity:0.65;">%s</td>'
            '<td style="padding:1px 0;">%s</td></tr>' % (k, v)
            for k, v in rows
        )

        # ── SSH 命令 ──
        ssh_lines = ["ssh " + domain]
        if vm.ssh_port:
            ssh_lines.append(
                "ssh -p %d %s@127.0.0.1" % (vm.ssh_port, spec.username)
            )
        ssh_html = "<br>".join(
            '<span style="font-family:monospace;">%s</span>' % line
            for line in ssh_lines
        )

        # ── 端口转发（排除 SSH，避免与上面重复）──
        others = [f for f in spec.forwards if f.guest_port != 22]
        ports_html = ""
        if others:
            items = "<br>".join(
                '<span style="font-family:monospace;">'
                '127.0.0.1:%d &rarr; VM:%d</span>'
                % (f.host_port, f.guest_port)
                for f in others
            )
            ports_html = (
                sep
                + '<div style="opacity:0.65;margin-bottom:2px;">端口转发</div>'
                + "<div>" + items + "</div>"
            )

        # ── 未运行提示 ──
        stopped_html = ""
        if not running:
            stopped_html = (
                '<div style="opacity:0.6;margin-top:6px;">'
                '未运行 &middot; 启动后可连接</div>'
            )

        return (
            '<div style="white-space:nowrap;">'
            '<div style="font-weight:600;margin-bottom:4px;">'
            + spec.name
            + '</div>'
            '<table style="border-collapse:collapse;">'
            + rows_html
            + '</table>'
            + sep
            + '<div style="opacity:0.65;margin-bottom:2px;">SSH</div>'
            + '<div>' + ssh_html + '</div>'
            + ports_html
            + stopped_html
            + '</div>'
        )

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
        self.act_purge.setEnabled(bool(self._vms) and not self._busy)

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
        ok, msg = vm_budget.check_vm_start(None, vm.spec.memory_mb)
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

        menu.addSeparator()
        menu.addAction(icon("info"), "详情…", self._open_detail)

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
            (self.act_purge, "delete"),
        ]:
            act.setIcon(icon(nm, danger=(nm == "delete")))
        self._rebuild()

    def on_disconnected(self) -> None:
        """Podman 断开不影响 VM 管理"""
        pass

    def stop_timers(self) -> None:
        self._list_timer.stop()



# ─────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────
def state_icon_for(state: str):
    from resources.icons import state_icon

    return state_icon(state, 16)

