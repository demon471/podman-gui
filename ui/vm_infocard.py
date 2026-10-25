"""虚拟机信息卡片（悬停弹窗）

行为对齐系统 tooltip：
  · 悬停约 500ms 后出现（同 QToolTip 的 wake-up 延迟）
  · 出现在行的右侧垂直居中（不跟鼠标移动，位置固定）
  · 移开即消失
  · 不抢焦点、不进任务栏

为什么不用 QToolTip
──────────────────
Qt 的 QToolTip 只支持 HTML 的一个子集：div 的 background-color、
border-radius、box-shadow、flex 布局全部无效，做不出卡片质感。
这里用自定义 QFrame + QSS 完整控制样式。

为什么不做阴影
──────────────────
用户反馈：不要花哨的，正常悬上去显示即可。阴影在 Wayland 下
还会有半透明边缘发灰的问题。就去掉。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.vm import dns as vm_dns
from resources.icons import icon


# 状态点颜色（与状态图标一致）
RUNNING_COLOR = "#35a45e"
STOPPED_COLOR = "#8a8d91"

# 悬停多久后显示（ms），与系统 tooltip 手感一致
SHOW_DELAY_MS = 500


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

        self._build()

        # 自动消失定时器：tooltip 的标准行为，悬停不动也会自己收起
        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self.hide_card)

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
        self.lbl_icon.setPixmap(icon("container", 20).pixmap(20, 20))
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

        # ── SSH 区 ──
        ssh_wrap = QWidget()
        sv = QVBoxLayout(ssh_wrap)
        sv.setContentsMargins(14, 10, 14, 12)
        sv.setSpacing(4)

        self.lbl_ssh_head = QLabel("SSH 连接")
        self.lbl_ssh_head.setObjectName("cardSection")
        sv.addWidget(self.lbl_ssh_head)

        self.lbl_cmd1 = QLabel()
        self.lbl_cmd1.setObjectName("cardCmd")
        sv.addWidget(self.lbl_cmd1)

        self.lbl_cmd2 = QLabel()
        self.lbl_cmd2.setObjectName("cardCmd")
        sv.addWidget(self.lbl_cmd2)

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

    # ─────────────────────────────────────────
    # 数据
    # ─────────────────────────────────────────
    def show_for(self, vm, running: bool) -> None:
        """填充内容（不显示，由调用方决定何时 show）"""
        from core.vm.cloudinit import DEFAULT_PASSWORD

        spec = vm.spec
        domain = vm_dns.vm_domain(spec.name)

        # 标题
        self.lbl_name.setText(spec.name)
        if running:
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
        self.lbl_cmd1.setText("ssh " + domain)
        if vm.ssh_port:
            self.lbl_cmd2.setText(
                "ssh -p %d %s@127.0.0.1" % (vm.ssh_port, spec.username)
            )
            self.lbl_cmd2.show()
        else:
            self.lbl_cmd2.hide()

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
        self.adjustSize()

    def show_at_row(self, global_x: int, global_y: int, row_height: int) -> None:
        """在指定位置显示（行的右侧、垂直居中）

        位置固定：悬停期间不跟鼠标移动，避免「追着跑」的躁动感。
        """
        self.adjustSize()

        # 行右侧 12px，垂直居中
        x = global_x + 12
        y = global_y + (row_height - self.height()) // 2

        # 底部越界时往上抬
        screen = self.screen().availableGeometry()
        if y + self.height() > screen.bottom():
            y = screen.bottom() - self.height() - 4
        # 右侧越界时贴右边
        if x + self.width() > screen.right():
            x = screen.right() - self.width() - 4

        self.move(x, y)

        if not self.isVisible():
            self.show()

        # 悬停 6 秒不动自动收起（同 tooltip 行为）
        self._auto_hide.start(6000)

    def hide_card(self) -> None:
        self._auto_hide.stop()
        if self.isVisible():
            self.hide()
