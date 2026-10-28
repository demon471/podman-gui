"""虚拟机详情对话框

参照 KDE 的标签页式属性对话框（如 Dolphin 属性、Plasma 系统设置）：
- 顶部信息区（名称、状态、系统信息）
- 标签页：文件 / 共享目录 / 账户
- 底部动作栏：重启 / SSH 登录 / 更多

端口转发不再单开标签页——列表页已有端口列，
详情里并进概览页做只读展示，减少一层点击。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import subprocess

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.notify import notifier
from core.vm import manager as vm_manager
from core.vm import ssh as vm_ssh
from resources.icons import ICON_SIZE_BUTTON, ICON_SIZE_DIALOG, icon
from ui.style import semantic_colors
from ui.utils import fmt_size


class _BusyOverlay(QWidget):
    """全屏忙碌遮罩

    用 paintEvent 自绘半透明背景 —— QLabel 的 QSS background
    在部分主题下不生效（父级样式覆盖），白字会看不清。
    自绘最可靠。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def paintEvent(self, e) -> None:
        from PyQt6.QtGui import QColor, QPainter

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 深色半透明底，白字在任何主题下都清晰
        p.fillRect(self.rect(), QColor(20, 20, 20, 190))

        p.setPen(QColor(255, 255, 255))
        f = self.font()
        f.setPointSizeF(f.pointSizeF() + 2)
        f.setBold(True)
        p.setFont(f)
        p.drawText(
            self.rect().adjusted(40, 40, -40, -40),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self._text,
        )
        p.end()


class VMDetailDialog(QDialog):
    # 请求「重启并挂载共享目录」
    # 由列表页接管：关闭本对话框 → 列表显示 loading →
    # 后台重启 + 挂载 → 完成后自动重新打开详情（定位到共享目录 tab）
    restart_and_mount_requested = pyqtSignal(str)
    """虚拟机详情"""

    def __init__(self, vm, parent: QWidget | None = None,
                 initial_tab: int | None = None):
        super().__init__(parent)
        self.vm = vm
        self._busy = False
        self._tasks: list = []
        # 共享目录配置是否已变更、待重启生效
        self._shares_dirty = False

        self.setWindowTitle(f"虚拟机 - {vm.spec.name}")
        self.resize(820, 620)

        self._build_ui()

        # 全屏忙碌遮罩：重启等耗时操作时盖住整个对话框，
        # 明确告诉用户「正在做什么」，并挡住所有点击。
        self._overlay = _BusyOverlay(self)
        self._overlay.hide()

        # 从「重启并挂载」流程回来时直接落在共享目录页
        if initial_tab is not None and 0 <= initial_tab < self.tabs.count():
            self.tabs.setCurrentIndex(initial_tab)

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
        self.lbl_icon.setPixmap(icon("vm", 48).pixmap(48, 48))
        head.addWidget(self.lbl_icon)

        col = QVBoxLayout()
        col.setSpacing(2)

        self.lbl_name = QLabel(self.vm.spec.name)
        f = QFont(self.lbl_name.font())
        f.setPointSizeF(f.pointSizeF() + 3)
        f.setBold(True)
        self.lbl_name.setFont(f)
        col.addWidget(self.lbl_name)

        # 副标题：镜像 + 状态（原来在「概览」页的两项，合并到头部）
        self.lbl_sub = QLabel("")
        self.lbl_sub.setObjectName("hintLabel")
        col.addWidget(self.lbl_sub)

        # SSH 访问地址：详情页唯一真正需要「看一眼」的信息。
        # 可选中复制，右侧按钮一键复制完整命令。
        ssh_row = QHBoxLayout()
        ssh_row.setContentsMargins(0, 2, 0, 0)
        ssh_row.setSpacing(6)

        self.lbl_ssh = QLabel("")
        self.lbl_ssh.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        ssh_row.addWidget(self.lbl_ssh)

        self.btn_copy_ssh = QToolButton()
        self.btn_copy_ssh.setIcon(icon("copy", 16))
        self.btn_copy_ssh.setToolTip("复制 SSH 命令")
        self.btn_copy_ssh.setAutoRaise(True)
        self.btn_copy_ssh.clicked.connect(self._copy_ssh)
        ssh_row.addWidget(self.btn_copy_ssh)
        ssh_row.addStretch()

        col.addLayout(ssh_row)

        head.addLayout(col, 1)
        v.addLayout(head)

        # ── 标签页（概览已合并到头部，这里只留实际操作面板）──
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_files(), "文件")
        self.tabs.addTab(self._build_shares(), "共享目录")
        self.tabs.addTab(self._build_account(), "账户")

        # 状态栏是三个 tab 共用的，切页时清掉上一页的残留
        # （比如文件页的「加载中…」会一直挂在那里）
        self.tabs.currentChanged.connect(lambda _i: self.lbl_status.setText(""))
        v.addWidget(self.tabs, 1)

        # ── 底部动作栏 ──
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hintLabel")
        bar.addWidget(self.lbl_status, 1)

        # 底部只保留「关闭」。启动/停止/重启/SSH 这些操作在
        # 主列表页的工具栏里已经齐全，详情页里重复一遍反而
        # 让界面变嘈杂 —— 详情页的职责是「查看和调整配置」。
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn_close = btns.button(QDialogButtonBox.StandardButton.Close)
        btn_close.setText("关闭")
        # 保存引用：文件面板忙碌时要禁用这个按钮
        self.btn_close = btn_close
        # 关闭按钮用红色（离开/危险语义），与其它页的删除按钮一致。
        # 用 stop 图标（方框形，视觉上接近「关闭」），因为图标库里
        # 没有单独的 close 线条图标。
        btn_close.setIcon(icon("stop", ICON_SIZE_BUTTON.width(), danger=True))
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        bar.addWidget(btns)

        v.addLayout(bar)

        # 文件面板的状态栏信息接到 lbl_status
        # （此处在底部栏之后，lbl_status 已存在）
        if hasattr(self, "file_panel"):
            self.file_panel.status.connect(self._on_file_status)

    # ── 概览 ──
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

        # 下载/上传进行中禁用「关闭」：
        # 中途关窗口会让后台线程 emit 命中已销毁对象 → abort
        self.file_panel.busy_changed.connect(self._on_file_busy)

        return w

    # ── 共享 ──
    def _build_shares(self) -> QWidget:
        """共享目录：宿主机目录 ↔ VM 内 /mnt/host/<挂载点>

        操作设计原则（用户视角）：
          · 选目录用文件浏览器，不用手打路径
          · 挂载点自动从目录名推导，可改
          · 一眼看出哪些已挂载、哪些需要重启
          · 改完给一个「应用并重启」的直达按钮
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(8)

        hint = QLabel(
            "把宿主机目录挂进 VM，在 VM 内访问 /mnt/host/<挂载点>。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.tbl_shares = QTableWidget(0, 3)
        self.tbl_shares.setHorizontalHeaderLabels(
            ["宿主机目录", "VM 内路径", "状态"]
        )
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
        hdr = self.tbl_shares.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.tbl_shares, 1)

        # 状态提示（需要重启时出现）
        self.lbl_share_hint = QLabel("")
        self.lbl_share_hint.setObjectName("hintLabel")
        self.lbl_share_hint.setWordWrap(True)
        self.lbl_share_hint.setVisible(False)
        v.addWidget(self.lbl_share_hint)

        # 操作行
        row = QHBoxLayout()
        row.setSpacing(6)

        self.btn_share_add = QPushButton("添加目录…")
        self.btn_share_add.setIcon(icon("add", ICON_SIZE_BUTTON.width()))
        self.btn_share_add.clicked.connect(self._add_share)
        row.addWidget(self.btn_share_add)

        self.btn_share_mount = QPushButton("立即挂载")
        self.btn_share_mount.setIcon(icon("play", ICON_SIZE_BUTTON.width()))
        self.btn_share_mount.setToolTip(
            "在 VM 内挂载选中的共享目录（需要 QEMU 已带 9p 设备，"
            "即添加后重启过）"
        )
        self.btn_share_mount.clicked.connect(self._mount_selected_share)
        row.addWidget(self.btn_share_mount)

        self.btn_share_del = QPushButton("移除")
        self.btn_share_del.setIcon(
            icon("delete", ICON_SIZE_BUTTON.width(), danger=True)
        )
        self.btn_share_del.clicked.connect(self._del_share)
        row.addWidget(self.btn_share_del)

        row.addStretch()

        self.btn_share_apply = QPushButton("应用并重启")
        self.btn_share_apply.setToolTip(
            "共享目录的增删需要重启 VM 才能让 QEMU 加载 9p 设备"
        )
        self.btn_share_apply.clicked.connect(self._apply_shares)
        self.btn_share_apply.setVisible(False)
        row.addWidget(self.btn_share_apply)

        v.addLayout(row)
        return w


    # ── 账户 ──
    def _build_account(self) -> QWidget:
        """账户：用户名 + 修改密码

        设计要点（都来自实际操作时的痛点）：
          · 每个密码框带「显示/隐藏」眼睛 —— 打错一个字符
            就要整段重输，看不见非常难受
          · 校验实时反馈：确认密码一输入就提示一致/不一致，
            不用等点了按钮才知道
          · 状态分色：成功绿 / 失败红 / 进行中灰，一眼可辨
          · 三个输入框都支持回车提交，不用去够按钮
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(10)

        self._eye_actions: list = []
        # 防抖：改密码进行中 / 成功提示期间，忽略重复触发
        self._pwd_busy = False

        # ── 账户信息 ──
        g1 = QGroupBox("账户")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f1.setSpacing(8)

        self.lbl_account = QLabel(self.vm.spec.username)
        self.lbl_account.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        f1.addRow("用户名", self.lbl_account)

        # SSH 地址放这里：改完密码最关心的就是「怎么连、用哪个密码」
        row_ssh = QHBoxLayout()
        row_ssh.setSpacing(6)
        self.lbl_ssh = QLabel(self.vm.ssh_command())
        self.lbl_ssh.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lbl_ssh.setObjectName("monoLabel")
        row_ssh.addWidget(self.lbl_ssh)
        btn_copy = QToolButton()
        btn_copy.setIcon(icon("copy", ICON_SIZE_BUTTON.width()))
        btn_copy.setToolTip("复制 SSH 命令")
        btn_copy.clicked.connect(self._copy_ssh)
        row_ssh.addWidget(btn_copy)
        row_ssh.addStretch(1)
        f1.addRow("SSH", row_ssh)

        v.addWidget(g1)

        # ── 修改密码 ──
        g2 = QGroupBox("修改密码")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f2.setSpacing(8)

        # 警示放最前面：用户点进来第一眼就该知道改了密码的后果，
        # 而不是填完、点提交之后才在底部看到一行小字
        hint2 = QLabel(
            "修改后立即生效：外部 ssh 连接必须改用新密码"
            "（本应用使用密钥登录，不受影响）。"
        )
        hint2.setObjectName("warnLabel")
        hint2.setWordWrap(True)
        # 单参数 addRow 占满整行：跨列的 wordWrap 标签
        # QFormLayout 不会正确计算高度，第二行会被裁掉
        f2.addRow(hint2)

        self.ed_old_pwd = self._add_pwd_field(
            f2, "当前密码", "输入当前登录密码"
        )

        self.ed_new_pwd = self._add_pwd_field(f2, "新密码", "至少 4 位")

        # 长度规则：实时显示，别等提交才报错
        self.lbl_pwd_rule = QLabel("至少 4 位字符")
        self.lbl_pwd_rule.setObjectName("hintLabel")
        self.lbl_pwd_rule.setWordWrap(True)
        f2.addRow("", self.lbl_pwd_rule)

        self.ed_new_pwd2 = self._add_pwd_field(f2, "确认密码", "再输一次")

        # 一致性提示：实时显示，占位保持高度稳定避免跳动
        self.lbl_pwd_match = QLabel("")
        self.lbl_pwd_match.setObjectName("hintLabel")
        self.lbl_pwd_match.setWordWrap(True)
        self.lbl_pwd_match.setMinimumHeight(18)
        f2.addRow("", self.lbl_pwd_match)

        # 按钮行
        row_btn = QHBoxLayout()
        row_btn.setSpacing(8)

        self.btn_chpwd = QPushButton("修改密码")
        self.btn_chpwd.setIcon(icon("save", ICON_SIZE_BUTTON.width()))
        self.btn_chpwd.clicked.connect(self._change_password)
        row_btn.addWidget(self.btn_chpwd)

        self.btn_pwd_clear = QPushButton("清空")
        self.btn_pwd_clear.clicked.connect(self._clear_pwd_fields)
        row_btn.addWidget(self.btn_pwd_clear)

        row_btn.addStretch()
        f2.addRow("", row_btn)

        # 操作状态：成功绿 / 失败红 / 进行中灰
        self.lbl_chpwd_status = QLabel("")
        self.lbl_chpwd_status.setObjectName("hintLabel")
        self.lbl_chpwd_status.setWordWrap(True)
        self.lbl_chpwd_status.setMinimumHeight(20)
        # 整行：这条消息可能有两行（含「外部 ssh 请使用新密码」），
        # 跨列 wordWrap 会被裁掉第二行
        f2.addRow(self.lbl_chpwd_status)

        v.addWidget(g2)

        # 实时校验
        self.ed_new_pwd.textChanged.connect(self._validate_pwd)
        self.ed_new_pwd2.textChanged.connect(self._validate_pwd)

        v.addStretch(1)

        # 关键：不允许布局把内容压到 sizeHint 以下。
        # 否则窗口不够高时 QLabel（wordWrap）会被裁掉半行，
        # 用户看到的是「提示文字突然断掉」。
        v.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        return w

    def _add_pwd_field(self, form: QFormLayout, label: str,
                       placeholder: str) -> QLineEdit:
        """生成一个带「显示/隐藏」按钮的密码输入框"""
        ed = QLineEdit()
        ed.setEchoMode(QLineEdit.EchoMode.Password)
        ed.setPlaceholderText(placeholder)
        # 不开 QLineEdit 自带的清除按钮：Breeze 下渲染成一坨深色块，
        # 和眼睛按钮挤在一起很乱；表单底部已有「清空」按钮。

        act = ed.addAction(
            icon("eye", ICON_SIZE_BUTTON.width()),
            QLineEdit.ActionPosition.TrailingPosition,
        )
        act.setToolTip("显示密码")
        act.triggered.connect(
            lambda _checked=False, e=ed, a=act: self._toggle_echo(e, a)
        )
        # QAction 由 QLineEdit 持有，但 Python 侧也要留引用，
        # 否则对象被回收后点击无反应
        self._eye_actions.append(act)

        # 回车即提交：三个框都能用
        ed.returnPressed.connect(self._change_password)

        form.addRow(label, ed)
        return ed

    @staticmethod
    def _toggle_echo(ed: QLineEdit, act) -> None:
        """切换密码可见性"""
        if ed.echoMode() == QLineEdit.EchoMode.Password:
            ed.setEchoMode(QLineEdit.EchoMode.Normal)
            act.setIcon(icon("eye_off", ICON_SIZE_BUTTON.width()))
            act.setToolTip("隐藏密码")
        else:
            ed.setEchoMode(QLineEdit.EchoMode.Password)
            act.setIcon(icon("eye", ICON_SIZE_BUTTON.width()))
            act.setToolTip("显示密码")

    @staticmethod
    def _apply_label_kind(lbl: QLabel, kind: str) -> None:
        """切换标签配色（'' 普通 / 'ok' 绿 / 'err' 红）

        Qt 不会因为 objectName 变化自动重算样式，
        必须 unpolish + polish 才会生效。
        """
        name = {"ok": "okLabel", "err": "errLabel"}.get(kind, "hintLabel")
        if lbl.objectName() == name:
            return
        lbl.setObjectName(name)
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)

    def _set_pwd_status(self, text: str, kind: str = "") -> None:
        self._apply_label_kind(self.lbl_chpwd_status, kind)
        self.lbl_chpwd_status.setText(text)

    def _validate_pwd(self) -> None:
        """实时校验：长度规则 + 两次输入一致性"""
        new1 = self.ed_new_pwd.text()
        new2 = self.ed_new_pwd2.text()

        # 长度
        if not new1:
            self.lbl_pwd_rule.setText("至少 4 位字符")
            self._apply_label_kind(self.lbl_pwd_rule, "")
        elif len(new1) < 4:
            self.lbl_pwd_rule.setText(f"还差 {4 - len(new1)} 位（至少 4 位）")
            self._apply_label_kind(self.lbl_pwd_rule, "err")
        else:
            self.lbl_pwd_rule.setText("✓ 长度符合要求")
            self._apply_label_kind(self.lbl_pwd_rule, "ok")

        # 一致性
        if not new2:
            self.lbl_pwd_match.setText("")
            self._apply_label_kind(self.lbl_pwd_match, "")
        elif new1 == new2:
            self.lbl_pwd_match.setText("✓ 两次输入一致")
            self._apply_label_kind(self.lbl_pwd_match, "ok")
        else:
            self.lbl_pwd_match.setText("✗ 两次输入不一致")
            self._apply_label_kind(self.lbl_pwd_match, "err")

    def _clear_pwd_fields(self) -> None:
        for ed in (self.ed_old_pwd, self.ed_new_pwd, self.ed_new_pwd2):
            ed.clear()
        self._validate_pwd()
        self._set_pwd_status("")
        self.ed_old_pwd.setFocus()

    def _pwd_error(self, msg: str, field: QLineEdit | None = None) -> None:
        """报错并把焦点送到出错的输入框

        只弹一行字在底部、不告诉用户是哪一栏出问题，
        用户还得自己一个个试。
        """
        self._set_pwd_status(f"✗ {msg}", "err")
        if field is not None:
            field.setFocus()
            field.selectAll()

    PWD_BTN_TEXT = "修改密码"

    def _set_pwd_busy(self, busy: bool) -> None:
        """进行中：按钮文字变「修改中…」并整组置灰

        三个按钮都要灰掉（含关闭）—— 只灰一个的话，用户还是能在
        改密码的同时点「清空」把正在提交的内容清掉，
        或者直接关窗口把操作打断。
        """
        self._pwd_busy = busy
        for w in (self.btn_chpwd, self.btn_pwd_clear,
                  self.ed_old_pwd, self.ed_new_pwd, self.ed_new_pwd2):
            w.setEnabled(not busy)
        self._lock_close(busy)
        self.btn_chpwd.setText("修改中…" if busy else self.PWD_BTN_TEXT)

    def _lock_close(self, locked: bool) -> None:
        """锁定/解锁底部关闭按钮（控件可能还没创建）"""
        btn = getattr(self, "btn_close", None)
        if btn is None:
            return
        # 整窗遮罩忙碌时由 _set_busy 管，这里不要抢
        if locked:
            btn.setEnabled(False)
        elif not getattr(self, "_busy", False):
            btn.setEnabled(True)

    def _flash_pwd_success(self) -> None:
        """成功反馈落在按钮上：绿色对勾 + 「保存成功」

        文案走禁用态渲染成灰色，同时把「清空」「关闭」一起锁住：
          · 三个按钮都点不动 → 天然防抖，不会连点两次改密码
          · 灰掉也传达了「这一轮已经结束」的信息
        1.5 秒后自动复原。
        """
        color = semantic_colors()["success"]
        self.btn_chpwd.setIcon(
            icon("check", ICON_SIZE_BUTTON.width(), color=color)
        )
        self.btn_chpwd.setText("保存成功")
        self.btn_chpwd.setStyleSheet("")
        self.btn_chpwd.setToolTip("密码已修改")

        self._pwd_busy = True
        self.btn_chpwd.setEnabled(False)
        self.btn_pwd_clear.setEnabled(False)
        self._lock_close(True)

        QTimer.singleShot(1500, self._restore_pwd_button)

    def _restore_pwd_button(self) -> None:
        """把按钮恢复成常态（保存图标 + 修改密码），解锁按钮"""
        try:
            self._pwd_busy = False
            self.btn_chpwd.setIcon(
                icon("save", ICON_SIZE_BUTTON.width())
            )
            self.btn_chpwd.setText(self.PWD_BTN_TEXT)
            self.btn_chpwd.setStyleSheet("")
            self.btn_chpwd.setToolTip("")
            self.btn_chpwd.setEnabled(True)
            self.btn_pwd_clear.setEnabled(True)
            self._lock_close(False)
        except RuntimeError:
            # 对话框已关闭，控件已销毁
            pass

    def _verify_old_password(self, user: str, old: str) -> tuple[bool, str]:
        """校验当前密码

        优先从宿主机用密码登录 VM 的 SSH —— 这是用户实际登录的
        同一条路径，最权威。若 guest 关了密码登录（或端口不通），
        退回 guest 内 su 验证。
        """
        port = getattr(self.vm, "ssh_port", None)

        if port:
            try:
                import paramiko

                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                try:
                    cli.connect(
                        "127.0.0.1",
                        port=int(port),
                        username=user,
                        password=old,
                        timeout=12,
                        allow_agent=False,
                        look_for_keys=False,
                        banner_timeout=12,
                        auth_timeout=12,
                    )
                    return True, ""
                except paramiko.AuthenticationException:
                    # 密码确实不对 —— 这是权威结论，不用再试别的
                    return False, "当前密码不正确"
                except Exception:
                    # 端口/网络问题，走下面的 su 兜底
                    pass
                finally:
                    try:
                        cli.close()
                    except Exception:
                        pass
            except ImportError:
                pass

        import shlex

        verify = (
            f"command -v su >/dev/null 2>&1 || echo NO_SU; "
            f"echo {shlex.quote(old)} | su - {shlex.quote(user)} "
            f"-c 'echo OLD_OK' 2>/dev/null"
        )
        try:
            _ok, out = vm_ssh.run_command(self.vm, verify, timeout=25)
        except Exception as e:
            return False, f"无法连接虚拟机校验密码: {e}"

        out = out or ""
        if "OLD_OK" in out:
            return True, ""
        if "NO_SU" in out:
            return False, "无法校验当前密码（guest 内没有 su 命令）"
        return False, "当前密码不正确"

    def _change_password(self) -> None:
        """修改 guest 内账户密码"""
        # 防抖：进行中或成功提示期间重复触发一律忽略
        # （回车 + 连点按钮很容易各触发一次）
        if self._pwd_busy:
            return

        old = self.ed_old_pwd.text()
        new1 = self.ed_new_pwd.text()
        new2 = self.ed_new_pwd2.text()

        # 逐项校验，出错直接定位到对应输入框
        if not old:
            self._pwd_error("请输入当前密码", self.ed_old_pwd)
            return
        if not new1:
            self._pwd_error("请输入新密码", self.ed_new_pwd)
            return
        if len(new1) < 4:
            self._pwd_error("新密码至少 4 位", self.ed_new_pwd)
            return
        if new1 != new2:
            self._pwd_error("两次输入的新密码不一致", self.ed_new_pwd2)
            return
        if new1 == old:
            self._pwd_error("新密码不能与当前密码相同", self.ed_new_pwd)
            return

        if not self.vm.running:
            # 资源已释放 → 先恢复再改密码，不让用户手动启动
            self._set_pwd_status("正在恢复虚拟机…", "")
            self._ensure_running(self._change_password)
            return

        # 改密码会影响外部 SSH 登录，先确认
        if QMessageBox.question(
            self, "修改密码",
            f"确定修改「{self.vm.spec.username}」的登录密码？\n\n"
            "修改后：\n"
            "· 外部 ssh 连接必须使用新密码\n"
            "· 本应用使用密钥登录，不受影响",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._set_pwd_busy(True)
        self._set_pwd_status("正在修改…", "")

        user = self.vm.spec.username

        def work():
            # 1) 校验旧密码
            ok, msg = self._verify_old_password(user, old)
            if not ok:
                return False, msg

            # 2) 设置新密码（chpasswd 需要 root，走免密 sudo）
            import shlex

            cmd = (
                f"echo {shlex.quote(user)}:{shlex.quote(new1)} | "
                f"sudo chpasswd"
            )
            try:
                ok2, out = vm_ssh.run_command(self.vm, cmd, timeout=25)
            except Exception as e:
                return False, f"执行失败: {e}"

            if not ok2:
                detail = (out or "").strip()[:160]
                return False, detail or "修改失败（无输出）"

            # chpasswd 成功时无输出；有输出且带 error 才算失败
            low = (out or "").lower()
            if "error" in low or "失败" in low:
                return False, (out or "").strip()[:160]
            return True, ""

        def done(res):
            self._set_pwd_busy(False)
            ok3, msg = res
            if ok3:
                self.ed_old_pwd.clear()
                self.ed_new_pwd.clear()
                self.ed_new_pwd2.clear()
                self._validate_pwd()
                self._set_pwd_status("")
                # 记住新密码：悬停卡片等地方要显示实际值
                vm_manager.set_password(self.vm.spec.name, new1)
                self._flash_pwd_success()
                notifier().info("密码已修改", self.vm.spec.name)
            else:
                self._set_pwd_status(f"✗ {msg}", "err")

        def error(m):
            self._set_pwd_busy(False)
            self._set_pwd_status(f"✗ {m}", "err")

        self._spawn(work, done, error)

    # ─────────────────────────────────────────
    # 刷新
    # ─────────────────────────────────────────
    def _is_available(self) -> bool:
        """对用户而言是否可用（含资源已释放、访问时秒级恢复的情况）"""
        if self.vm.running:
            return True
        try:
            from core.vm import keepalive as ka

            enabled, _ = ka.load_config()
            if not enabled:
                return False
            from core import state

            return bool(state.get_setting(
                f"vm.desired_running.{self.vm.spec.name}", False
            ))
        except Exception:
            return False

    def _ensure_running(self, then) -> None:
        """确保 VM 在跑；资源已释放则先恢复，然后执行 then"""
        if self.vm.running:
            then()
            return

        self.lbl_status.setText("正在恢复…")
        self._set_busy(True)

        def work():
            from core.vm import manager as mgr

            return mgr.resume_vm(self.vm)

        def done(res):
            self._set_busy(False)
            ok = bool(res and res[0])
            if not ok:
                QMessageBox.warning(
                    self, "恢复失败",
                    (res[1] if res else "") or "未知错误",
                )
                self.lbl_status.setText("恢复失败")
                return
            self.lbl_status.setText("")
            self.refresh()
            then()

        self._spawn(work, done, self._op_error)

    def refresh(self) -> None:
        vm = self.vm
        spec = vm.spec
        available = self._is_available()

        # 副标题：镜像 · 状态
        state_text = "运行中" if available else "已停止"
        self.lbl_sub.setText(f"{spec.image_key} · {state_text}")

        # SSH 地址
        from core.vm.dns import vm_domain

        self.lbl_ssh.setText(f"ssh {vm_domain(spec.name)}")

        # 共享目录
        self._fill_shares()

    def _on_file_busy(self, busy: bool) -> None:
        """文件面板忙碌时禁用关闭按钮（避免中途关窗口导致崩溃）

        不再在底部写「操作进行中，请稍候…」——
        文件面板自己已有加载指示，底部这行只是噪音。
        """
        btn = getattr(self, "btn_close", None)
        if btn is not None:
            btn.setEnabled(not busy)

    def _tick(self) -> None:
        """定时刷新副标题里的状态文字"""
        if not self.isVisible():
            return
        state_text = "运行中" if self._is_available() else "已停止"
        self.lbl_sub.setText(f"{self.vm.spec.image_key} · {state_text}")

    def _fill_shares(self) -> None:
        """填充共享目录表格：目录 / VM 内路径 / 挂载状态

        状态通过 SSH 查询 guest 的 /proc/mounts，失败时统一显示
        「未挂载」，不阻塞界面（查询放后台）。
        """
        shares = self.vm.spec.shares
        self.tbl_shares.setRowCount(len(shares))

        for r, (host_path, tag) in enumerate(shares):
            self.tbl_shares.setItem(r, 0, QTableWidgetItem(host_path))
            self.tbl_shares.setItem(
                r, 1, QTableWidgetItem(f"/mnt/host/{tag}")
            )
            # 先占位，异步查询后回填
            state_item = QTableWidgetItem("…")
            state_item.setData(Qt.ItemDataRole.UserRole, tag)
            self.tbl_shares.setItem(r, 2, state_item)

        # 按钮可用性
        has = bool(shares)
        self.btn_share_mount.setEnabled(has)
        self.btn_share_del.setEnabled(has)

        if not has:
            return

        if not self.vm.running:
            for r in range(len(shares)):
                self.tbl_shares.item(r, 2).setText("未挂载")
            return

        # 后台查挂载状态
        def work():
            return vm_ssh.share_states(self.vm)

        def done(states):
            for r in range(self.tbl_shares.rowCount()):
                item = self.tbl_shares.item(r, 2)
                if item is None:
                    continue
                tag = item.data(Qt.ItemDataRole.UserRole)
                mounted = states.get(tag, False)
                item.setText("已挂载" if mounted else "未挂载")

        def error(_msg):
            for r in range(self.tbl_shares.rowCount()):
                item = self.tbl_shares.item(r, 2)
                if item is not None:
                    item.setText("未挂载")

        self._spawn(work, done, error)


    def _on_file_status(self, text: str) -> None:
        """文件面板的状态只在「文件」tab 显示

        状态栏是共用的，否则切到账户页还挂着文件页的
        「加载中…」，看着像界面卡住了。
        """
        if self.tabs.currentIndex() == 0:
            self.lbl_status.setText(text)

    def _copy_ssh(self) -> None:
        cmd = self.vm.ssh_command()
        QGuiApplication.clipboard().setText(cmd)
        self.lbl_status.setText(f"已复制到剪贴板：{cmd}")

    @staticmethod
    def _derive_share_tag(path: str) -> tuple[str, bool]:
        """从目录名推导 9p 挂载点

        返回 (tag, 是否因无法推导而回退到 share)。

        要点：不能把非 ASCII 字符逐个替换成 `-` ——
        `/home/john/下载` 会变成 `--`，输入框里显示两个减号，
        非常诡异。这里直接丢弃非 ASCII，结果为空时回退到 share。
        """
        name = Path(path).name
        # 只保留 ASCII 字母数字下划线连字符
        tag = re.sub(r"[^A-Za-z0-9_-]", "", name)
        # 清理连续的 -/_ 和首尾残留
        tag = re.sub(r"[-_]{2,}", "-", tag).strip("-_")

        if not tag:
            return "share", True
        return tag, False

    def _add_share(self) -> None:
        """添加共享目录：选目录 → 自动推导挂载点 → 确认"""
        from PyQt6.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(
            self, "选择要共享的宿主机目录", str(Path.home()),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return

        tag, auto = self._derive_share_tag(path)

        existing = {t for _p, t in self.vm.spec.shares}
        if tag in existing:
            i = 2
            while f"{tag}-{i}" in existing:
                i += 1
            tag = f"{tag}-{i}"

        # 用 QInputDialog 实例而不是 getText 静态方法，
        # 这样才能给输入框设置 placeholder（提示格式）。
        dlg = QInputDialog(self)
        dlg.setWindowTitle("挂载点")
        dlg.setLabelText(
            f"宿主机目录：{path}\n\n"
            f"VM 内访问路径：/mnt/host/<挂载点>"
            + ("\n\n（目录名含中文等字符，已自动命名为 share）" if auto else "")
        )
        dlg.setTextValue(tag)
        dlg.setTextEchoMode(QLineEdit.EchoMode.Normal)
        dlg.setOkButtonText("添加")
        dlg.setCancelButtonText("取消")
        line = dlg.findChild(QLineEdit)
        if line is not None:
            line.setPlaceholderText("挂载点名称，如 projects")
            line.selectAll()
        if dlg.exec() != QInputDialog.DialogCode.Accepted:
            return
        tag = dlg.textValue().strip()
        if not tag:
            return
        # 挂载点必须能安全用作 9p tag
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            QMessageBox.warning(
                self, "挂载点不合法",
                "挂载点只能用字母、数字、下划线和连字符。\n"
                "（VM 内路径不能含中文或空格）",
            )
            return

        ok, err = vm_manager.add_share(self.vm, path, tag)
        if not ok:
            QMessageBox.warning(self, "添加失败", err)
            return

        self.refresh()
        self._mark_shares_changed(f"已添加 {path} → /mnt/host/{tag}")

    def _del_share(self) -> None:
        """移除共享目录（若已挂载，先卸载再删配置）"""
        row = self.tbl_shares.currentRow()
        if row < 0 or row >= len(self.vm.spec.shares):
            return
        path, tag = self.vm.spec.shares[row]

        if QMessageBox.question(
            self, "移除共享",
            f"移除「{tag}」？\n\n宿主机目录 {path} 本身不会被删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        ok, err = vm_manager.remove_share(self.vm, tag)
        if not ok:
            QMessageBox.warning(self, "移除失败", err)
            return

        if self.vm.running:
            self._spawn(
                lambda: vm_ssh.unmount_share(self.vm, tag),
                lambda _r: self.refresh(),
                lambda _m: self.refresh(),
            )
        else:
            self.refresh()

        self._mark_shares_changed(f"已移除 {tag}")

    def _mount_selected_share(self) -> None:
        """在 VM 内立即挂载选中的共享目录"""
        row = self.tbl_shares.currentRow()
        if row < 0 or row >= len(self.vm.spec.shares):
            self.lbl_status.setText("请先选择一个共享目录")
            return

        _path, tag = self.vm.spec.shares[row]
        if not self.vm.running:
            self.lbl_status.setText("虚拟机未运行，启动后会自动挂载")
            return

        self._spawn(
            lambda: vm_ssh.mount_share(self.vm, tag, f"/mnt/host/{tag}"),
            lambda res: self._on_mount_result(tag, res),
            lambda m: self.lbl_status.setText(f"挂载失败: {m}"),
        )

    def _on_mount_result(self, tag: str, result) -> None:
        ok, msg = result if isinstance(result, tuple) else (False, "")
        if ok:
            self.lbl_status.setText(f"已挂载 /mnt/host/{tag}")
        else:
            text = (msg or "").strip()
            low = text.lower()
            if "not found" in low or "no such device" in low:
                self.lbl_status.setText(
                    f"挂载点 {tag} 的设备不存在 —— 请先「应用并重启」"
                )
            else:
                self.lbl_status.setText(f"挂载失败: {text[:120]}")
        self.refresh()

    def _mark_shares_changed(self, message: str) -> None:
        """共享配置变更后：说明需要重启，并直接问要不要现在重启

        用户视角的友好做法：
          · 不要求用户自己去点「重启」按钮
          · 明确说清「为什么需要重启」（QEMU 的共享设备只在启动时加载）
          · 默认选「现在重启」，一键走完
        """
        self._shares_dirty = True
        self.lbl_status.setText(message)

        ret = QMessageBox.question(
            self, "需要重启",
            f"{message}\n\n"
            "共享目录是虚拟机的启动参数，修改后需要重启才能生效。\n\n"
            "现在重启吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret == QMessageBox.StandardButton.Yes:
            self._apply_shares()
            return

        # 选择稍后：留一个显眼的入口，别让用户找不到
        self.lbl_share_hint.setText(
            f"{message}。重启虚拟机后生效 —— 点右下角「应用并重启」。"
        )
        self.lbl_share_hint.setVisible(True)
        self.btn_share_apply.setVisible(True)

    def _apply_shares(self) -> None:
        """应用共享配置：交给列表页去重启

        为什么不在这里重启：
        重启要几十秒，用户需要看到列表里的 loading 状态，
        而不是盯着一个被遮罩盖住的对话框干等。
        所以这里只做一件事 —— 发信号、关自己，
        后续由列表页统一管理（显示 loading → 重启 → 挂载 →
        自动重新打开详情页并定位到共享目录）。
        """
        if QMessageBox.question(
            self, "需要重启",
            f"重启「{self.vm.spec.name}」以应用共享目录变更？\n\n"
            "重启期间虚拟机会短暂不可用，完成后会自动打开这个页面。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return

        name = self.vm.spec.name
        self.restart_and_mount_requested.emit(name)
        # 立刻关闭自己，把舞台交给列表页
        self.accept()

    # ─────────────────────────────────────────
    # 底部动作
    # ─────────────────────────────────────────
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
        """重启：先停止再启动，直到 SSH 可连才算成功

        与列表页的标准一致：qemu 进程起来不等于启动成功，
        能 SSH 进去才算。等待期间底部按钮保持禁用。
        """
        r = QMessageBox.question(
            self, "确认重启",
            "重启虚拟机「" + self.vm.spec.name + "」？\n\n"
            "将先停止再启动，直到 SSH 可连才算完成，"
            "可能需要一到几分钟。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self.lbl_status.setText("正在重启（等待 SSH 就绪）…")

        def work():
            ok, err = vm_manager.restart_vm(self.vm)
            if not ok:
                return False, err

            # qemu 已起，轮询 SSH 直到可连（最多 180 秒）
            import time as _t

            t0 = _t.monotonic()
            while _t.monotonic() - t0 < 180:
                ok2, out = vm_ssh.run_command(
                    self.vm, "echo SSH_OK", timeout=8
                )
                if ok2 and "SSH_OK" in out:
                    return True, ""
                _t.sleep(4)
            return False, "180s 内 SSH 未就绪"

        def done(res):
            self._set_busy(False)
            ok, err = res
            if ok:
                self.lbl_status.setText("已重启（SSH 可连）")
                notifier().info("虚拟机已重启", self.vm.spec.name)
            else:
                self.lbl_status.setText(str(err))
                QMessageBox.warning(self, "重启失败", str(err))
            self.refresh()

        self._spawn(work, done, self._op_error)

    def _open_ssh_terminal(self) -> None:
        """用外部终端打开 SSH 会话（资源已释放则先恢复）"""
        if not self.vm.running:
            self._ensure_running(self._open_ssh_terminal)
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

    def _set_busy(self, busy: bool, text: str = "") -> None:
        """切换忙碌态：盖一层遮罩 + 禁用所有交互

        用户视角：重启这类操作要几十秒，界面必须明确表达
        「正在做什么」并且不能再接受任何点击 —— 否则用户会以为
        卡死，或者点了按钮触发新操作把状态搞乱。
        """
        self._busy = busy

        if busy:
            self._overlay.set_text(text or "正在处理…")
            self._overlay.setGeometry(self.rect())
            self._overlay.show()
            self._overlay.raise_()
        else:
            self._overlay.hide()

        # 遮罩能挡住点击，但这些也一并禁用，语义更明确
        for w in (self.tabs, self.btn_close, self.lbl_status):
            try:
                w.setEnabled(not busy)
            except Exception:
                pass

    def resizeEvent(self, e) -> None:
        """遮罩始终铺满对话框"""
        super().resizeEvent(e)
        try:
            self._overlay.setGeometry(self.rect())
        except Exception:
            pass

    def _spawn(self, work, done, error) -> None:
        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def closeEvent(self, e) -> None:
        self._timer.stop()
        super().closeEvent(e)
