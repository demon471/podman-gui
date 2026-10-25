"""虚拟机详情对话框

参照 KDE 的标签页式属性对话框（如 Dolphin 属性、Plasma 系统设置）：
- 顶部信息区（名称、状态、系统信息）
- 标签页：概览 / 端口 / 共享 / 内存 / 控制台
"""

from __future__ import annotations

import subprocess

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.notify import notifier
from core.vm import dns as vm_dns
from core.vm import manager as vm_manager
from core.vm import memory as vm_memory
from core.vm import network as vm_network
from core.vm import qemu as vm_qemu
from core.vm import ssh as vm_ssh
from resources.icons import ICON_SIZE_BUTTON, ICON_SIZE_DIALOG, icon
from ui.utils import fmt_size


class VMDetailDialog(QDialog):
    """虚拟机详情"""

    def __init__(self, vm, parent: QWidget | None = None):
        super().__init__(parent)
        self.vm = vm
        self._busy = False
        self._tasks: list = []

        self.setWindowTitle(f"虚拟机 - {vm.spec.name}")
        self.resize(820, 620)

        self._build_ui()
        self.refresh()

        # 状态轮询
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(3000)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 14, 14, 10)
        v.setSpacing(10)

        # ── 头部 ──
        head = QHBoxLayout()
        head.setSpacing(12)

        self.lbl_icon = QLabel()
        self.lbl_icon.setPixmap(icon("container", 48).pixmap(48, 48))
        head.addWidget(self.lbl_icon)

        col = QVBoxLayout()
        col.setSpacing(2)

        self.lbl_name = QLabel(self.vm.spec.name)
        f = QFont(self.lbl_name.font())
        f.setPointSizeF(f.pointSizeF() + 3)
        f.setBold(True)
        self.lbl_name.setFont(f)
        col.addWidget(self.lbl_name)

        self.lbl_sub = QLabel("")
        self.lbl_sub.setObjectName("hintLabel")
        col.addWidget(self.lbl_sub)

        head.addLayout(col, 1)
        v.addLayout(head)

        # ── 标签页 ──
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), "概览")
        self.tabs.addTab(self._build_ports(), "端口转发")
        self.tabs.addTab(self._build_shares(), "共享目录")
        self.tabs.addTab(self._build_memory(), "内存")
        self.tabs.addTab(self._build_console(), "控制台")
        v.addWidget(self.tabs, 1)

        # ── 底部 ──
        bar = QHBoxLayout()
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hintLabel")
        bar.addWidget(self.lbl_status, 1)

        self.btn_ssh = QPushButton("SSH 登录")
        self.btn_ssh.setIcon(icon("terminal", ICON_SIZE_BUTTON.width()))
        self.btn_ssh.clicked.connect(self._copy_ssh)
        bar.addWidget(self.btn_ssh)

        self.btn_reclaim = QPushButton("回收内存")
        self.btn_reclaim.setIcon(icon("clean", ICON_SIZE_BUTTON.width()))
        self.btn_reclaim.clicked.connect(self._reclaim)
        bar.addWidget(self.btn_reclaim)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        bar.addWidget(btns)

        v.addLayout(bar)

    # ── 概览 ──
    def _build_overview(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(10)

        g1 = QGroupBox("配置")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f1.setSpacing(8)
        self.lbl_os = QLabel("-")
        self.lbl_cpu = QLabel("-")
        self.lbl_mem_cfg = QLabel("-")
        self.lbl_disk = QLabel("-")
        self.lbl_created = QLabel("-")
        f1.addRow("操作系统", self.lbl_os)
        f1.addRow("处理器", self.lbl_cpu)
        f1.addRow("内存", self.lbl_mem_cfg)
        f1.addRow("磁盘", self.lbl_disk)
        f1.addRow("创建时间", self.lbl_created)
        v.addWidget(g1)

        g2 = QGroupBox("运行状态")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f2.setSpacing(8)
        self.lbl_state = QLabel("-")
        self.lbl_uptime = QLabel("-")
        self.lbl_usage = QLabel("-")
        self.lbl_host = QLabel("-")
        self.lbl_host_mem = QLabel("-")
        f2.addRow("状态", self.lbl_state)
        f2.addRow("运行时长", self.lbl_uptime)
        f2.addRow("内存占用", self.lbl_usage)
        f2.addRow("guest 系统", self.lbl_host)
        f2.addRow("宿主机内存", self.lbl_host_mem)
        v.addWidget(g2)

        row = QHBoxLayout()
        self.btn_refresh_sys = QPushButton("读取 guest 信息")
        self.btn_refresh_sys.clicked.connect(self._load_guest_info)
        row.addWidget(self.btn_refresh_sys)
        row.addStretch()
        v.addLayout(row)
        v.addStretch()

        return w

    # ── 端口 ──
    def _build_ports(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(8)

        hint = QLabel(
            "端口转发把宿主机端口映射到 VM 端口。\n"
            f"访问方式：http://127.0.0.1:<宿主机端口> 或 "
            f"http://{vm_dns.vm_domain(self.vm.spec.name)}:<宿主机端口>"
            "（域名需一次性配置，见设置页）。\n"
            "修改端口后需重启虚拟机生效。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.tbl_ports = QTableWidget(0, 4)
        self.tbl_ports.setHorizontalHeaderLabels(
            ["宿主机", "VM 端口", "协议", "访问地址"]
        )
        self.tbl_ports.verticalHeader().setVisible(False)
        self.tbl_ports.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tbl_ports.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tbl_ports.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.tbl_ports.setAlternatingRowColors(True)
        self.tbl_ports.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.tbl_ports, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("宿主机端口"))
        self.sp_host_port = QSpinBox()
        self.sp_host_port.setRange(1, 65535)
        self.sp_host_port.setValue(8080)
        row.addWidget(self.sp_host_port)

        row.addWidget(QLabel("→ VM"))
        self.sp_guest_port = QSpinBox()
        self.sp_guest_port.setRange(1, 65535)
        self.sp_guest_port.setValue(8080)
        row.addWidget(self.sp_guest_port)

        self.cmb_proto = QComboBox()
        self.cmb_proto.addItems(["tcp", "udp"])
        row.addWidget(self.cmb_proto)

        btn_add = QPushButton("添加")
        btn_add.setIcon(icon("add", ICON_SIZE_BUTTON.width()))
        btn_add.clicked.connect(self._add_port)
        row.addWidget(btn_add)

        btn_del = QPushButton("删除")
        btn_del.setIcon(icon("delete", ICON_SIZE_BUTTON.width(), danger=True))
        btn_del.clicked.connect(self._del_port)
        row.addWidget(btn_del)

        row.addStretch()
        v.addLayout(row)

        return w

    # ── 共享 ──
    def _build_shares(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(8)

        hint = QLabel(
            "共享目录通过 virtio-9p 把宿主机目录挂载进 VM，实现宿主机与 VM 文件互通。"
            "挂载点在 VM 内为 /mnt/host/<挂载点>。修改后需重启虚拟机生效。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.tbl_shares = QTableWidget(0, 2)
        self.tbl_shares.setHorizontalHeaderLabels(["宿主机目录", "挂载点"])
        self.tbl_shares.verticalHeader().setVisible(False)
        self.tbl_shares.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tbl_shares.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.tbl_shares.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.tbl_shares.setAlternatingRowColors(True)
        self.tbl_shares.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.tbl_shares, 1)

        row = QHBoxLayout()
        self.ed_share_path = QLineEdit()
        self.ed_share_path.setPlaceholderText("宿主机目录，如 /home/john/projects")
        row.addWidget(self.ed_share_path, 2)

        row.addWidget(QLabel("挂载点"))
        self.ed_share_tag = QLineEdit()
        self.ed_share_tag.setPlaceholderText("如 projects")
        self.ed_share_tag.setFixedWidth(140)
        row.addWidget(self.ed_share_tag)

        btn_add = QPushButton("添加")
        btn_add.setIcon(icon("add", ICON_SIZE_BUTTON.width()))
        btn_add.clicked.connect(self._add_share)
        row.addWidget(btn_add)

        btn_del = QPushButton("删除")
        btn_del.setIcon(icon("delete", ICON_SIZE_BUTTON.width(), danger=True))
        btn_del.clicked.connect(self._del_share)
        row.addWidget(btn_del)

        v.addLayout(row)
        return w

    # ── 内存 ──
    def _build_memory(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(10)

        g1 = QGroupBox("当前占用")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f1.setSpacing(8)
        self.lbl_mem_rss = QLabel("-")
        self.lbl_mem_vsz = QLabel("-")
        self.lbl_mem_swap = QLabel("-")
        self.lbl_mem_eff = QLabel("-")
        f1.addRow("物理内存 (RSS)", self.lbl_mem_rss)
        f1.addRow("虚拟内存 (VSZ)", self.lbl_mem_vsz)
        f1.addRow("换出 (Swap)", self.lbl_mem_swap)
        f1.addRow("占用效率", self.lbl_mem_eff)
        v.addWidget(g1)

        g2 = QGroupBox("自动内存管理")
        v2 = QVBoxLayout(g2)
        v2.setSpacing(8)

        hint = QLabel(
            "内存回收已自动开启，无需配置。\n"
            "虚拟机空闲时，guest 内的空闲内存会归还宿主机；"
            "有操作时自动取回，不会导致虚拟机内存不足。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v2.addWidget(hint)

        self.lbl_balloon = QLabel("")
        self.lbl_balloon.setObjectName("hintLabel")
        self.lbl_balloon.setWordWrap(True)
        v2.addWidget(self.lbl_balloon)

        v.addWidget(g2)
        v.addStretch()
        return w

    # ── 控制台 ──
    def _build_console(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(8)

        hint = QLabel(
            "QEMU 串口控制台输出（只读）。VM 启动过程的日志会显示在这里，"
            "可用于排查启动失败。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFontFamily("monospace")
        v.addWidget(self.console, 1)

        row = QHBoxLayout()
        btn_reload = QPushButton("刷新")
        btn_reload.setIcon(icon("refresh", ICON_SIZE_BUTTON.width()))
        btn_reload.clicked.connect(self._load_console)
        row.addWidget(btn_reload)
        row.addStretch()
        v.addLayout(row)

        return w

    # ─────────────────────────────────────────
    # 刷新
    # ─────────────────────────────────────────
    def refresh(self) -> None:
        vm = self.vm
        spec = vm.spec

        self.lbl_sub.setText(
            f"{spec.image_key} · {spec.cpus} vCPU · {spec.memory_mb} MB 内存 · "
            f"{spec.disk_gb} GB 磁盘"
        )

        # 概览
        self.lbl_os.setText(spec.image_key)
        self.lbl_cpu.setText(f"{spec.cpus} vCPU")
        self.lbl_mem_cfg.setText(f"{spec.memory_mb} MB")
        self.lbl_disk.setText(f"{spec.disk_gb} GB")
        import time as _t

        self.lbl_created.setText(
            _t.strftime("%Y-%m-%d %H:%M", _t.localtime(spec.created_at))
            if spec.created_at
            else "-"
        )

        running = vm.running
        self.lbl_state.setText("运行中" if running else "已停止")

        if running:
            up = vm.uptime
            h, rem = divmod(up, 3600)
            m, s = divmod(rem, 60)
            self.lbl_uptime.setText(f"{h} 小时 {m} 分 {s} 秒" if h else f"{m} 分 {s} 秒")
        else:
            self.lbl_uptime.setText("-")

        # 内存
        self._update_memory()

        # 表格
        self._fill_ports()
        self._fill_shares()

        self.btn_ssh.setEnabled(running)
        self.btn_reclaim.setEnabled(running)

    def _tick(self) -> None:
        if not self.isVisible():
            return
        self._update_memory()
        self.lbl_state.setText("运行中" if self.vm.running else "已停止")

    def _update_memory(self) -> None:
        try:
            u = vm_memory.vm_mem_usage(self.vm)
        except Exception:
            return

        self.lbl_mem_rss.setText(f"{u.rss_mb:.0f} MB")
        self.lbl_mem_vsz.setText(f"{u.vsz_mb:.0f} MB")
        self.lbl_mem_swap.setText(f"{u.swap_mb:.0f} MB")

        if self.vm.running and u.guest_mb:
            self.lbl_mem_eff.setText(
                f"{u.efficiency:.0f}%（RSS / 已分配 {u.guest_mb} MB）"
            )
            self.lbl_usage.setText(f"{u.rss_mb:.0f} MB（配置 {u.guest_mb} MB）")
            self.lbl_balloon.setText(
                "当前实际占用 "f"{u.rss_mb:.0f} MB / 已分配 {u.guest_mb} MB。"
                "占用数值可在列表页的内存条上实时查看。"
            )
        else:
            self.lbl_mem_eff.setText("-")
            self.lbl_usage.setText("-")
            self.lbl_balloon.setText("")

        # 宿主机内存写在专用标签，避免覆盖 _load_guest_info 读到的 guest 信息
        try:
            hm = vm_memory.host_memory()
            self.lbl_host_mem.setText(
                f"宿主机内存 {hm.used_mb / 1024:.1f} / "
                f"{hm.total_mb / 1024:.1f} GB"
            )
        except Exception:
            pass

    def _fill_ports(self) -> None:
        forwards = self.vm.spec.forwards
        self.tbl_ports.setRowCount(len(forwards))

        for i, f in enumerate(forwards):
            self.tbl_ports.setItem(i, 0, QTableWidgetItem(f"{f.host_addr}:{f.host_port}"))
            self.tbl_ports.setItem(i, 1, QTableWidgetItem(str(f.guest_port)))
            self.tbl_ports.setItem(i, 2, QTableWidgetItem(f.protocol))

            # 访问地址
            if f.guest_port == 22:
                # 域名方式（~/.ssh/config 已自动映射端口，无需 -p）
                addr = f"ssh {vm_dns.vm_domain(self.vm.spec.name)}"
            elif f.guest_port == 443:
                addr = f"https://127.0.0.1:{f.host_port}"
            else:
                addr = f"http://127.0.0.1:{f.host_port} / http://{vm_network.vm_domain(self.vm.spec.name)}:{f.host_port}"
            self.tbl_ports.setItem(i, 3, QTableWidgetItem(addr))

        hdr = self.tbl_ports.horizontalHeader()
        for c in range(3):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)

    def _fill_shares(self) -> None:
        shares = self.vm.spec.shares
        self.tbl_shares.setRowCount(len(shares))
        for i, (path, tag) in enumerate(shares):
            self.tbl_shares.setItem(i, 0, QTableWidgetItem(path))
            self.tbl_shares.setItem(i, 1, QTableWidgetItem(f"/mnt/host/{tag}"))
        self.tbl_shares.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )

    def _load_console(self) -> None:
        try:
            out = vm_qemu.read_console(self.vm.dir)
        except Exception as e:
            out = f"读取失败: {e}"
        self.console.setPlainText(out[-200000:])
        self.console.verticalScrollBar().setValue(
            self.console.verticalScrollBar().maximum()
        )

    def _load_guest_info(self) -> None:
        if not self.vm.running:
            QMessageBox.information(self, "未运行", "请先启动虚拟机。")
            return

        self.btn_refresh_sys.setEnabled(False)
        self.lbl_status.setText("正在读取 guest 信息…")

        def work():
            return vm_ssh.get_system_info(self.vm)

        def done(info):
            self.btn_refresh_sys.setEnabled(True)
            self.lbl_status.setText("")
            if not info:
                self.lbl_status.setText("无法读取 guest 信息（SSH 未就绪）")
                return
            # get_system_info 直接以 os-release 的键名存入（PRETTY_NAME / ID）
            pretty = (
                info.get("PRETTY_NAME")
                or info.get("ID")
                or info.get("os")
                or "-"
            )
            self.lbl_os.setText(pretty)

            parts = [f"内核 {info.get('kernel', '-')}"]
            if info.get("cpus"):
                parts.append(f"{info['cpus']} 核")
            if info.get("mem_total_mb"):
                parts.append(f"guest {info['mem_total_mb']} MB")
            if info.get("disk"):
                parts.append(f"磁盘 {info['disk']}")
            self.lbl_host.setText(" · ".join(parts))

        def error(msg):
            self.btn_refresh_sys.setEnabled(True)
            self.lbl_status.setText(f"读取失败: {msg}")

        self._spawn(work, done, error)

    # ─────────────────────────────────────────
    # 操作
    # ─────────────────────────────────────────
    def _copy_ssh(self) -> None:
        cmd = self.vm.ssh_command()
        QGuiApplication.clipboard().setText(cmd)
        self.lbl_status.setText(f"已复制到剪贴板：{cmd}")

    def _add_port(self) -> None:
        hp = self.sp_host_port.value()
        gp = self.sp_guest_port.value()
        proto = self.cmb_proto.currentText()

        def work():
            return vm_manager.add_forward(self.vm, hp, gp, proto)

        def done(res):
            ok, err = res
            if not ok:
                QMessageBox.warning(self, "添加失败", err)
                return
            self.refresh()
            self.lbl_status.setText(
                f"已添加 {hp} → VM:{gp}，重启虚拟机后生效"
            )

        self._spawn(work, done, lambda m: QMessageBox.warning(self, "失败", m))

    def _del_port(self) -> None:
        row = self.tbl_ports.currentRow()
        if row < 0 or row >= len(self.vm.spec.forwards):
            return
        f = self.vm.spec.forwards[row]

        if f.guest_port == 22:
            QMessageBox.information(
                self, "无法删除",
                "SSH 端口是虚拟机管理的必需项，不能删除。",
            )
            return

        if QMessageBox.question(
            self, "确认删除",
            f"确定删除端口转发 {f.host_addr}:{f.host_port} → VM:{f.guest_port} 吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        ok, err = vm_manager.remove_forward(self.vm, f.host_port)
        if not ok:
            QMessageBox.warning(self, "删除失败", err)
            return
        self.refresh()
        self.lbl_status.setText("已删除，重启虚拟机后生效")

    def _add_share(self) -> None:
        path = self.ed_share_path.text().strip()
        tag = self.ed_share_tag.text().strip()
        if not path or not tag:
            QMessageBox.information(self, "请填写", "宿主机目录和挂载点都要填写。")
            return

        ok, err = vm_manager.add_share(self.vm, path, tag)
        if not ok:
            QMessageBox.warning(self, "添加失败", err)
            return

        self.ed_share_path.clear()
        self.ed_share_tag.clear()
        self.refresh()
        self.lbl_status.setText(f"已添加共享 {path} → /mnt/host/{tag}，重启后生效")

    def _del_share(self) -> None:
        row = self.tbl_shares.currentRow()
        if row < 0 or row >= len(self.vm.spec.shares):
            return
        path, tag = self.vm.spec.shares[row]

        if QMessageBox.question(
            self, "确认删除",
            f"确定删除共享「{tag}」（{path}）吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        ok, err = vm_manager.remove_share(self.vm, tag)
        if not ok:
            QMessageBox.warning(self, "删除失败", err)
            return
        self.refresh()
        self.lbl_status.setText("已删除，重启虚拟机后生效")

    def _reclaim(self) -> None:
        if not self.vm.running:
            return

        before = 0.0
        try:
            before = vm_memory.vm_mem_usage(self.vm).rss_mb
        except Exception:
            pass

        keep = 128   # 自动管理，保留 128MB 下限
        self.lbl_status.setText("正在回收内存…")

        def work():
            return vm_memory.balloon_reclaim(self.vm, keep)

        def done(res):
            ok, msg = res
            if not ok:
                self.lbl_status.setText(f"回收失败: {msg}")
                QMessageBox.warning(self, "回收失败", msg)
                return
            after = vm_memory.vm_mem_usage(self.vm).rss_mb
            saved = max(0.0, before - after)
            self._update_memory()
            self.lbl_status.setText(
                f"已回收：{before:.0f} MB → {after:.0f} MB（释放 {saved:.0f} MB）"
            )
            notifier().info(
                "内存已回收",
                f"{self.vm.spec.name}\n{before:.0f} MB → {after:.0f} MB",
            )

        self._spawn(work, done, lambda m: self.lbl_status.setText(f"失败: {m}"))

    # ─────────────────────────────────────────
    def _spawn(self, work, done, error) -> None:
        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def closeEvent(self, e) -> None:
        self._timer.stop()
        super().closeEvent(e)
