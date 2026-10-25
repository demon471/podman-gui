"""虚拟机详情对话框

参照 KDE 的标签页式属性对话框（如 Dolphin 属性、Plasma 系统设置）：
- 顶部信息区（名称、状态、系统信息）
- 标签页：概览 / 文件 / 共享目录 / 控制台
- 底部动作栏：重启 / SSH 登录 / 更多

端口转发不再单开标签页——列表页已有端口列，
详情里并进概览页做只读展示，减少一层点击。
"""

from __future__ import annotations

import subprocess

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
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
        self.tabs.addTab(self._build_files(), "文件")
        self.tabs.addTab(self._build_shares(), "共享目录")
        self.tabs.addTab(self._build_console(), "控制台")
        v.addWidget(self.tabs, 1)

        # ── 底部动作栏 ──
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hintLabel")
        bar.addWidget(self.lbl_status, 1)

        # 启停（VM 未运行时显示「启动」，运行中显示「停止」）
        self.btn_toggle = QPushButton("启动")
        self.btn_toggle.setIcon(icon("play", ICON_SIZE_BUTTON.width()))
        self.btn_toggle.clicked.connect(self._toggle_power)
        bar.addWidget(self.btn_toggle)

        self.btn_restart = QPushButton("重启")
        self.btn_restart.setIcon(icon("restart", ICON_SIZE_BUTTON.width()))
        self.btn_restart.setToolTip("先停止再启动虚拟机")
        self.btn_restart.clicked.connect(self._restart)
        bar.addWidget(self.btn_restart)

        self.btn_ssh = QPushButton("SSH 登录")
        self.btn_ssh.setIcon(icon("terminal", ICON_SIZE_BUTTON.width()))
        self.btn_ssh.setToolTip("复制 SSH 命令到剪贴板")
        self.btn_ssh.clicked.connect(self._copy_ssh)
        bar.addWidget(self.btn_ssh)

        # 「更多」：把不常用操作收进菜单，保持底部清爽
        self.btn_more = QPushButton("更多")
        self.btn_more.setIcon(icon("more", ICON_SIZE_BUTTON.width()))

        menu = QMenu(self)
        menu.addAction(
            icon("terminal"), "打开终端（外部）",
            self._open_ssh_terminal,
        )
        menu.addAction(
            icon("edit"), "重命名…", self._rename_vm,
        )
        menu.addSeparator()
        menu.addAction(
            icon("folder"), "打开 VM 数据目录", self._open_data_dir,
        )
        menu.addAction(
            icon("copy"), "复制 VM 名称", self._copy_name,
        )
        menu.addSeparator()
        menu.addAction(
            icon("delete", danger=True), "删除虚拟机…", self._delete_vm,
        )
        self.btn_more.setMenu(menu)
        bar.addWidget(self.btn_more)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        bar.addWidget(btns)

        v.addLayout(bar)

        # 文件面板的状态栏信息接到 lbl_status
        # （此处在底部栏之后，lbl_status 已存在）
        if hasattr(self, "file_panel"):
            self.file_panel.status.connect(self.lbl_status.setText)

    # ── 概览 ──
    def _build_overview(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(10)

        g1 = QGroupBox("系统")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f1.setSpacing(8)
        self.lbl_os = QLabel("-")
        self.lbl_created = QLabel("-")
        f1.addRow("操作系统", self.lbl_os)
        f1.addRow("创建时间", self.lbl_created)
        v.addWidget(g1)

        g2 = QGroupBox("运行状态")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f2.setSpacing(8)
        self.lbl_state = QLabel("-")
        self.lbl_uptime = QLabel("-")
        self.lbl_host = QLabel("-")
        f2.addRow("状态", self.lbl_state)
        f2.addRow("运行时长", self.lbl_uptime)
        f2.addRow("guest 系统", self.lbl_host)
        v.addWidget(g2)

        # 端口转发（只读展示，原独立的「端口转发」标签页）
        #
        # 为什么并进概览而不是单开一页：
        # 端口是「看一眼地址」的信息，不需要重交互。
        # 列表页已有端口列，这里给出完整访问地址即可。
        self.g_ports = QGroupBox("端口转发")
        f3 = QFormLayout(self.g_ports)
        f3.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f3.setSpacing(8)

        self.lbl_ports = QLabel("-")
        self.lbl_ports.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lbl_ports.setWordWrap(True)
        f3.addRow("映射", self.lbl_ports)

        self.lbl_access = QLabel("-")
        self.lbl_access.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lbl_access.setWordWrap(True)
        f3.addRow("访问地址", self.lbl_access)

        v.addWidget(self.g_ports)

        row = QHBoxLayout()
        self.btn_refresh_sys = QPushButton("读取 guest 信息")
        self.btn_refresh_sys.clicked.connect(self._load_guest_info)
        row.addWidget(self.btn_refresh_sys)
        row.addStretch()
        v.addLayout(row)
        v.addStretch()

        return w

    # ── 文件 ──
    def _build_files(self) -> QWidget:
        """VM 内文件浏览（SFTP，走已有 SSH 通道，零配置）"""
        from ui.vm_files import FilePanel

        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.file_panel = FilePanel(self.vm, self)
        v.addWidget(self.file_panel)

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


    # ── 控制台 ──
    def _build_console(self) -> QWidget:
        """控制台：串口日志 + 账户密码管理"""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(8)

        # ── 账户 ──
        g1 = QGroupBox("账户")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f1.setSpacing(8)

        self.lbl_account = QLabel(self.vm.spec.username)
        self.lbl_account.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        f1.addRow("用户名", self.lbl_account)

        v.addWidget(g1)

        # ── 修改密码 ──
        g2 = QGroupBox("修改密码")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f2.setSpacing(8)

        self.ed_old_pwd = QLineEdit()
        self.ed_old_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_old_pwd.setPlaceholderText("当前密码")
        f2.addRow("当前密码", self.ed_old_pwd)

        self.ed_new_pwd = QLineEdit()
        self.ed_new_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_new_pwd.setPlaceholderText("新密码")
        f2.addRow("新密码", self.ed_new_pwd)

        self.ed_new_pwd2 = QLineEdit()
        self.ed_new_pwd2.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_new_pwd2.setPlaceholderText("再输一次")
        f2.addRow("确认密码", self.ed_new_pwd2)

        btn_row = QHBoxLayout()
        self.btn_chpwd = QPushButton("修改密码")
        self.btn_chpwd.setIcon(icon("save", ICON_SIZE_BUTTON.width()))
        self.btn_chpwd.clicked.connect(self._change_password)
        btn_row.addWidget(self.btn_chpwd)

        self.lbl_chpwd_status = QLabel("")
        self.lbl_chpwd_status.setObjectName("hintLabel")
        self.lbl_chpwd_status.setWordWrap(True)
        btn_row.addWidget(self.lbl_chpwd_status, 1)

        f2.addRow("", btn_row)

        hint2 = QLabel(
            "修改后新密码立即生效，"
            "建议同时更新自己的记录。"
        )
        hint2.setObjectName("hintLabel")
        hint2.setWordWrap(True)
        f2.addRow("", hint2)

        v.addWidget(g2)

        # ── 串口日志 ──
        g3 = QGroupBox("控制台输出")
        v3 = QVBoxLayout(g3)
        v3.setSpacing(6)

        hint3 = QLabel(
            "QEMU 串口输出（只读）。"
            "VM 启动过程的日志会显示在这里，"
            "可用于排查启动失败。"
        )
        hint3.setObjectName("hintLabel")
        hint3.setWordWrap(True)
        v3.addWidget(hint3)

        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setFontFamily("monospace")
        v3.addWidget(self.console, 1)

        row = QHBoxLayout()
        btn_reload = QPushButton("刷新")
        btn_reload.setIcon(icon("refresh", ICON_SIZE_BUTTON.width()))
        btn_reload.clicked.connect(self._load_console)
        row.addWidget(btn_reload)
        row.addStretch()
        v3.addLayout(row)

        v.addWidget(g3, 1)

        return w

    def _change_password(self) -> None:
        """修改 guest 内账户密码（需先验证当前密码）"""
        old = self.ed_old_pwd.text()
        new1 = self.ed_new_pwd.text()
        new2 = self.ed_new_pwd2.text()

        if not old:
            self.lbl_chpwd_status.setText("请输入当前密码")
            return
        if not new1:
            self.lbl_chpwd_status.setText("请输入新密码")
            return
        if new1 != new2:
            self.lbl_chpwd_status.setText("两次输入的新密码不一致")
            return
        if len(new1) < 4:
            self.lbl_chpwd_status.setText("新密码至少 4 位")
            return
        if not self.vm.running:
            self.lbl_chpwd_status.setText("虚拟机未运行")
            return

        self.btn_chpwd.setEnabled(False)
        self.lbl_chpwd_status.setText("正在修改…")

        user = self.vm.spec.username

        def work():
            # 用 chpasswd 非交互修改；先验证旧密码是否正确。
            # passwd -S 只能看状态，不能验证旧密码，
            # 所以用「旧密码登录测试」来校验。
            import shlex

            # 1) 验证旧密码：用 su 切过去试
            verify_cmd = (
                f"echo {shlex.quote(old)} | su - {shlex.quote(user)} "
                f"-c 'echo OLD_OK' 2>/dev/null"
            )
            ok, out = vm_ssh.run_command(
                self.vm, verify_cmd, timeout=20
            )
            if "OLD_OK" not in out:
                return False, "当前密码不正确"

            # 2) 设置新密码
            cmd = (
                f"echo {shlex.quote(user)}:{shlex.quote(new1)} | "
                f"sudo chpasswd"
            )
            ok, out = vm_ssh.run_command(self.vm, cmd, timeout=20)
            if not ok:
                return False, out.strip()[:120] or "修改失败"

            return True, ""

        def done(res):
            ok, msg = res
            self.btn_chpwd.setEnabled(True)
            if ok:
                self.lbl_chpwd_status.setText("✓ 密码已修改")
                self.ed_old_pwd.clear()
                self.ed_new_pwd.clear()
                self.ed_new_pwd2.clear()
                notifier().info("密码已修改", self.vm.spec.name)
            else:
                self.lbl_chpwd_status.setText(f"✗ {msg}")

        self._spawn(work, done,
                    lambda m: (
                        setattr(self.btn_chpwd, "enabled", True),
                        setattr(self.lbl_chpwd_status, "text", f"✗ {m}"),
                    ))

    # ─────────────────────────────────────────
    # 刷新
    # ─────────────────────────────────────────
    def refresh(self) -> None:
        vm = self.vm
        spec = vm.spec

        self.lbl_sub.setText(spec.image_key)

        # 概览
        self.lbl_os.setText(spec.image_key)
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

        # 端口（标签形式）
        self._fill_ports()

        # 共享目录
        self._fill_shares()

        # 底部按钮状态
        self.btn_ssh.setEnabled(running)
        self.btn_restart.setEnabled(running)
        self.btn_toggle.setText("停止" if running else "启动")
        self.btn_toggle.setIcon(
            icon("stop" if running else "play", ICON_SIZE_BUTTON.width())
        )

    def _tick(self) -> None:
        if not self.isVisible():
            return
        self.lbl_state.setText("运行中" if self.vm.running else "已停止")


    def _fill_ports(self) -> None:
        """端口转发 —— 文本标签形式（原表格版改为只读摘要）"""
        forwards = self.vm.spec.forwards

        if not forwards:
            self.lbl_ports.setText("无")
            self.lbl_access.setText("-")
            return

        # 映射关系
        self.lbl_ports.setText(
            "\n".join(
                f"127.0.0.1:{f.host_port}  →  VM:{f.guest_port} "
                f"({f.protocol})"
                for f in forwards
            )
        )

        # 访问地址
        domain = vm_dns.vm_domain(self.vm.spec.name)
        addrs = []
        for f in forwards:
            if f.guest_port == 22:
                addrs.append(f"ssh {domain}")
            elif f.guest_port == 443:
                addrs.append("https://" + domain + ":" + str(f.host_port))
            elif f.guest_port == 80:
                addrs.append("http://" + domain + ":" + str(f.host_port))
            else:
                addrs.append(domain + ":" + str(f.host_port))

        self.lbl_access.setText("\n".join(addrs))

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


    # ─────────────────────────────────────────
    # 底部动作
    # ─────────────────────────────────────────
    def _toggle_power(self) -> None:
        """启动 / 停止"""
        if self.vm.running:
            self._do_stop()
        else:
            self._do_start()

    def _do_start(self) -> None:
        self._set_busy(True)
        self.lbl_status.setText("正在启动…")

        def work():
            return vm_manager.start_vm(self.vm)

        def done(res):
            self._set_busy(False)
            ok, err = res
            if ok:
                self.lbl_status.setText("已启动")
                notifier().info("虚拟机已启动", self.vm.spec.name)
            else:
                self.lbl_status.setText("启动失败: " + str(err))
                QMessageBox.warning(self, "启动失败", str(err))
            self.refresh()

        self._spawn(work, done, self._op_error)

    def _do_stop(self) -> None:
        r = QMessageBox.question(
            self, "确认停止",
            "停止虚拟机「{self.vm.spec.name}」？\n\n"
            "guest 内未保存的数据会丢失。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self.lbl_status.setText("正在停止…")

        def work():
            return vm_manager.stop_vm(self.vm)

        def done(res):
            self._set_busy(False)
            ok, err = res
            self.lbl_status.setText("已停止" if ok else "停止失败: " + str(err))
            self.refresh()

        self._spawn(work, done, self._op_error)

    def _restart(self) -> None:
        """重启：先停止再启动"""
        r = QMessageBox.question(
            self, "确认重启",
            "重启虚拟机「{self.vm.spec.name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self.lbl_status.setText("正在重启…")

        def work():
            ok, err = vm_manager.stop_vm(self.vm)
            if not ok:
                return False, "停止失败: " + str(err)
            ok, err = vm_manager.start_vm(self.vm)
            if not ok:
                return False, "启动失败: " + str(err)
            return True, ""

        def done(res):
            self._set_busy(False)
            ok, err = res
            if ok:
                self.lbl_status.setText("已重启")
                notifier().info("虚拟机已重启", self.vm.spec.name)
            else:
                self.lbl_status.setText(str(err))
                QMessageBox.warning(self, "重启失败", str(err))
            self.refresh()

        self._spawn(work, done, self._op_error)

    def _open_ssh_terminal(self) -> None:
        """用外部终端打开 SSH 会话"""
        if not self.vm.running:
            QMessageBox.information(self, "未运行", "请先启动虚拟机。")
            return

        cmd = self.vm.ssh_command()

        # 依次尝试常见终端（KDE 环境优先 konsole）
        import shutil

        for term, args in (
            ("konsole", ["-e", "bash", "-lc"]),
            ("x-terminal-emulator", ["-e"]),
            ("gnome-terminal", ["--"]),
            ("kitty", []),
            ("alacritty", ["-e"]),
            ("foot", []),
        ):
            path = shutil.which(term)
            if not path:
                continue
            try:
                subprocess.Popen([path, *args, cmd], start_new_session=True)
                self.lbl_status.setText("已在终端中打开：" + cmd)
                return
            except OSError:
                continue

        QMessageBox.information(
            self, "未找到终端",
            "没有找到可用的终端模拟器。\n\n可手动执行：\n" + cmd,
        )

    def _open_data_dir(self) -> None:
        """在文件管理器中打开 VM 数据目录"""
        import shutil

        path = str(self.vm.dir)
        for opener in ("dolphin", "nautilus", "thunar", "xdg-open"):
            exe = shutil.which(opener)
            if not exe:
                continue
            try:
                subprocess.Popen([exe, path], start_new_session=True)
                self.lbl_status.setText("已打开 " + path)
                return
            except OSError:
                continue

        QGuiApplication.clipboard().setText(path)
        self.lbl_status.setText("已复制数据目录路径到剪贴板")

    def _copy_name(self) -> None:
        QGuiApplication.clipboard().setText(self.vm.spec.name)
        self.lbl_status.setText("已复制名称")

    def _rename_vm(self) -> None:
        """重命名虚拟机（目录 + 配置 + SSH 配置同步）"""
        if self.vm.running:
            QMessageBox.information(
                self, "无法重命名", "请先停止虚拟机再重命名。"
            )
            return

        new, ok = QInputDialog.getText(
            self, "重命名虚拟机", "新名称：", text=self.vm.spec.name
        )
        if not ok:
            return

        new = (new or "").strip()
        if not new or new == self.vm.spec.name:
            return

        ok2, err = vm_manager.rename_vm(self.vm, new)
        if not ok2:
            QMessageBox.warning(self, "重命名失败", err)
            return

        notifier().info("虚拟机已重命名", new)
        self.setWindowTitle("虚拟机 - " + new)
        self.lbl_name.setText(new)
        self.lbl_status.setText("已重命名为 " + new)
        self.refresh()

    def _delete_vm(self) -> None:
        """删除虚拟机（二次确认：先确认，再输入名称）"""
        name = self.vm.spec.name

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("删除虚拟机")
        box.setText("确定删除虚拟机「" + name + "」？")
        box.setInformativeText(
            "将删除虚拟机配置与磁盘数据，此操作不可撤销。"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("删除")
        box.button(QMessageBox.StandardButton.No).setText("取消")

        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        # 二次确认：输入名称，防止误删
        typed, ok = QInputDialog.getText(
            self, "再次确认",
            "请输入虚拟机名称「" + name + "」以确认删除：",
        )
        if not ok or (typed or "").strip() != name:
            self.lbl_status.setText("名称不匹配，已取消")
            return

        def work():
            return vm_manager.delete_vm(self.vm)

        def done(res):
            ok2, err = res
            if ok2:
                notifier().info("虚拟机已删除", name)
                self.accept()
            else:
                QMessageBox.warning(self, "删除失败", err)

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    def _op_error(self, msg: str) -> None:
        self._set_busy(False)
        self.lbl_status.setText("操作失败: " + str(msg))

    def _set_busy(self, busy: bool) -> None:
        for b in (self.btn_toggle, self.btn_restart, self.btn_ssh,
                  self.btn_more):
            b.setEnabled(not busy)

    def _spawn(self, work, done, error) -> None:
        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def closeEvent(self, e) -> None:
        self._timer.stop()
        super().closeEvent(e)
