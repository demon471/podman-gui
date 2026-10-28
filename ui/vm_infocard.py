"""虚拟机信息卡片（悬停弹窗）

行为：
  · 悬停约 400ms 后出现，位置贴近鼠标（右下 8px 偏移）
  · 鼠标可以从行移进卡片内（卡片自身接收 hover，不消失）
  · SSH 命令后有复制按钮：点击复制，图标变绿勾 1.5 秒
  · 无圆角、无阴影
  · 移出卡片和表格范围即消失，悬停 8 秒不动自动收起

为什么不用 QToolTip
──────────────────
Qt 的 QToolTip 只支持 HTML 的一个子集，且无法含交互控件
（复制按钮）。用自定义 QFrame 才能放 QPushButton。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.vm import dns as vm_dns
from resources.icons import icon


# 状态点颜色（与状态图标一致）
RUNNING_COLOR = "#35a45e"
STOPPED_COLOR = "#8a8d91"

# 悬停多久后显示（ms）
SHOW_DELAY_MS = 400
# 复制成功后绿勾回退时长（ms）
COPY_FEEDBACK_MS = 1500
# 悬停不动自动收起（ms）
AUTO_HIDE_MS = 8000


class VMInfoCard(QFrame):
    """悬停信息卡片（单例复用，不反复创建）"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(
            parent,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
        )
        self.setObjectName("vmInfoCard")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 接收 hover：鼠标进入卡片时不触发表格的 Leave
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self._build()

        # 自动消失
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self.hide_card)

        # 复制反馈回退
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._reset_copy_icons)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 标题行：图标 + 名称 + 状态 ──
        head = QHBoxLayout()
        head.setContentsMargins(14, 10, 14, 0)
        head.setSpacing(8)

        self.lbl_icon = QLabel()
        self.lbl_icon.setPixmap(icon("vm", 20).pixmap(20, 20))
        head.addWidget(self.lbl_icon)

        self.lbl_name = QLabel()
        self.lbl_name.setObjectName("cardTitle")
        head.addWidget(self.lbl_name)

        head.addStretch()

        self.lbl_state = QLabel()
        head.addWidget(self.lbl_state)

        root.addLayout(head)

        # ── 信息网格 ──
        grid = QGridLayout()
        grid.setContentsMargins(14, 8, 14, 12)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(4)

        self._rows: list[tuple[QLabel, QLabel]] = []
        for i, key in enumerate(("系统", "账户", "初始密码")):
            k = QLabel(key)
            k.setObjectName("cardKey")
            v = QLabel()
            v.setObjectName("cardValue")
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self._rows.append((k, v))

        root.addLayout(grid)

        # ── 分隔线 ──
        self._sep1 = QFrame()
        self._sep1.setObjectName("cardSep")
        self._sep1.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(self._sep1)

        # ── SSH 区（每条命令后面跟复制按钮）──
        ssh_wrap = QWidget()
        sv = QVBoxLayout(ssh_wrap)
        sv.setContentsMargins(14, 10, 14, 12)
        sv.setSpacing(4)

        self.lbl_ssh_head = QLabel("SSH 连接")
        self.lbl_ssh_head.setObjectName("cardSection")
        sv.addWidget(self.lbl_ssh_head)

        # 命令 1 + 复制按钮
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        self.lbl_cmd1 = QLabel()
        self.lbl_cmd1.setObjectName("cardCmd")
        row1.addWidget(self.lbl_cmd1)
        self.btn_copy1 = self._make_copy_btn()
        row1.addWidget(self.btn_copy1)
        row1.addStretch()
        sv.addLayout(row1)

        # 命令 2 + 复制按钮
        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.lbl_cmd2 = QLabel()
        self.lbl_cmd2.setObjectName("cardCmd")
        row2.addWidget(self.lbl_cmd2)
        self.btn_copy2 = self._make_copy_btn()
        row2.addWidget(self.btn_copy2)
        row2.addStretch()
        sv.addLayout(row2)

        root.addWidget(ssh_wrap)

        # ── 端口区（按需显隐）──
        self._ports_wrap = QWidget()
        pv = QVBoxLayout(self._ports_wrap)
        pv.setContentsMargins(14, 0, 14, 12)
        pv.setSpacing(4)

        self.lbl_ports_head = QLabel("端口转发")
        self.lbl_ports_head.setObjectName("cardSection")
        pv.addWidget(self.lbl_ports_head)

        self.lbl_ports = QLabel()
        self.lbl_ports.setObjectName("cardCmd")
        pv.addWidget(self.lbl_ports)

        self._ports_wrap.hide()
        root.addWidget(self._ports_wrap)

        # ── 未运行提示 ──
        self.lbl_hint = QLabel()
        self.lbl_hint.setObjectName("cardHint")
        self.lbl_hint.hide()
        root.addWidget(self.lbl_hint)

    def _make_copy_btn(self) -> QToolButton:
        """命令后的复制小按钮"""
        b = QToolButton()
        b.setObjectName("cardCopyBtn")
        b.setIcon(icon("copy", 14))
        b.setAutoRaise(True)
        b.setFixedSize(22, 22)
        b.setToolTip("复制")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: self._copy_clicked(b))
        return b

    # ─────────────────────────────────────────
    # 数据
    # ─────────────────────────────────────────
    def show_for(self, vm, available: bool) -> None:
        """填充内容（不显示，由调用方决定何时 show）

        available 表示「对用户可用」—— 包含真在运行和
        资源已释放（访问时秒级恢复）两种底层状态。
        用户视角没有区别，所以统一显示为运行中。
        """
        from core.vm.cloudinit import DEFAULT_PASSWORD

        self._reset_copy_icons()
        self._current_vm = vm

        spec = vm.spec
        domain = vm_dns.vm_domain(spec.name)

        # 标题
        self.lbl_name.setText(spec.name)
        if available:
            self.lbl_state.setText("● 运行中")
            self.lbl_state.setStyleSheet(
                f"color: {RUNNING_COLOR}; font-weight: 500;"
            )
        else:
            self.lbl_state.setText("● 已停止")
            self.lbl_state.setStyleSheet(
                f"color: {STOPPED_COLOR};"
            )

        # 信息
        values = (spec.image_key, spec.username, DEFAULT_PASSWORD)
        for (_k, v), val in zip(self._rows, values):
            v.setText(val)

        # SSH 命令
        self._cmd1_text = "ssh " + domain
        self.lbl_cmd1.setText(self._cmd1_text)
        if vm.ssh_port:
            self._cmd2_text = (
                "ssh -p %d %s@127.0.0.1" % (vm.ssh_port, spec.username)
            )
            self.lbl_cmd2.setText(self._cmd2_text)
            self.lbl_cmd2.show()
            self.btn_copy2.show()
        else:
            self._cmd2_text = ""
            self.lbl_cmd2.hide()
            self.btn_copy2.hide()

        # 端口转发（排除 SSH）
        others = [f for f in spec.forwards if f.guest_port != 22]
        if others:
            self.lbl_ports.setText(
                "\n".join(
                    "127.0.0.1:%d → VM:%d" % (f.host_port, f.guest_port)
                    for f in others
                )
            )
            self._ports_wrap.show()
        else:
            self._ports_wrap.hide()

        # 未运行提示
        # 注意：参数已从 running 改名为 available（语义是「对用户可用」，
        # 包含真在运行和资源已释放两种情况）。这里必须同步，
        # 否则会 NameError → Qt 事件循环里抛异常 → 进程 abort。
        if not available:
            self.lbl_hint.setText("未运行 · 启动后即可连接")
            self.lbl_hint.show()
        else:
            self.lbl_hint.hide()

        self.adjustSize()

    # ─────────────────────────────────────────
    # 复制
    # ─────────────────────────────────────────
    def _copy_clicked(self, btn: QToolButton) -> None:
        """复制对应命令，按钮变绿勾"""
        text = (
            self._cmd1_text if btn is self.btn_copy1 else self._cmd2_text
        )
        if not text:
            return

        QGuiApplication.clipboard().setText(text)

        # 变绿勾
        btn.setIcon(icon("check", 14, danger=False))
        btn.setStyleSheet(f"QToolButton {{ color: {RUNNING_COLOR}; }}")
        btn.setToolTip("已复制")

        self._feedback_timer.start(COPY_FEEDBACK_MS)

    def _reset_copy_icons(self) -> None:
        """复制按钮回灰色复制图标"""
        self._feedback_timer.stop()
        for b in (getattr(self, "btn_copy1", None),
                  getattr(self, "btn_copy2", None)):
            if b is None:
                continue
            b.setIcon(icon("copy", 14))
            b.setStyleSheet("")
            b.setToolTip("复制")

    # ─────────────────────────────────────────
    # 显示 / 隐藏
    # ─────────────────────────────────────────
    def show_near(self, global_x: int, global_y: int) -> None:
        """贴近鼠标显示（右下 8px 偏移，越界翻转）"""
        self.adjustSize()

        x = global_x + 8
        y = global_y + 8

        screen = self.screen().availableGeometry()
        if x + self.width() > screen.right():
            x = global_x - self.width() - 8
        if y + self.height() > screen.bottom():
            y = global_y - self.height() - 8
        # 兜底：翻完还越界就贴边
        x = max(screen.left() + 4, min(x, screen.right() - self.width() - 4))
        y = max(screen.top() + 4, min(y, screen.bottom() - self.height() - 4))

        self.move(x, y)
        if not self.isVisible():
            self.show()

        self._auto_hide.start(AUTO_HIDE_MS)

    def hide_card(self) -> None:
        self._auto_hide.stop()
        if self.isVisible():
            self.hide()

    # ─────────────────────────────────────────
    # hover 状态（供列表页判断「鼠标是否在卡片上」）
    # ─────────────────────────────────────────
    hovered = False

    def enterEvent(self, e) -> None:
        """鼠标进入卡片：标记 + 停掉自动消失"""
        self.hovered = True
        self._auto_hide.stop()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        """鼠标离开卡片：清除标记；表格那边若也已离开就收起"""
        self.hovered = False
        self._auto_hide.start(3000)
        super().leaveEvent(e)

    def event(self, ev) -> bool:
        # HoverEnter/HoverLeave 与 enter/leaveEvent 等价，
        # 但 ToolTip 窗口有时只发 Hover 事件，两边都接住
        t = ev.type()
        if t == ev.Type.HoverEnter:
            self.hovered = True
            self._auto_hide.stop()
        elif t == ev.Type.HoverLeave:
            self.hovered = False
            self._auto_hide.start(3000)
        return super().event(ev)
