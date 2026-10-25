"""虚拟机信息卡片（悬停弹窗）

为什么不用 QToolTip
──────────────────
Qt 的 QToolTip 只支持 HTML 的一个子集：div 的 background-color、
border-radius、box-shadow、flex 布局全部无效。所以再怎么调
HTML/CSS 都做不出「卡片」质感，永远是一块扁平文字。

这里改用自定义 QFrame + QSS：
  · 完整 CSS（圆角、边框、内边距、分隔线、字体颜色）
  · 真实 QLabel 控件，字体/颜色精确可控
  · Qt.WindowType.ToolTip 标志：行为与系统 tooltip 一致
    （不抢焦点、失焦自动消失、不进任务栏）

布局
────
  名称  ....................  ● 状态
  ──────────────────────────────
  系统            alpine-3.22
  账户            john
  初始密码        12345
  ──────────────────────────────
  SSH 连接
  ssh test.vm.local
  ssh -p 2222 john@127.0.0.1
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
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
        # ToolTip 窗口默认无边框，但要给它留出阴影空间：
        # 否则阴影会被裁到边框为止，看起来像贴边
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        # 柔和投影（QFrame 不支持 box-shadow，用 GraphicsEffect）
        self._apply_shadow()

        self._build()

    def _apply_shadow(self) -> None:
        """柔和外阴影，让卡片从内容上「浮」起来

        透明度取低值（0.18）：太重的阴影在暗色主题下很脏。
        """
        try:
            from PyQt6.QtWidgets import QGraphicsDropShadowEffect

            fx = QGraphicsDropShadowEffect(self)
            fx.setBlurRadius(18)
            fx.setOffset(0, 2)
            # 低透明度阴影：暗色主题下过重的阴影会显脏
            c = self.palette().color(
                self.palette().ColorRole.Shadow
            )
            c.setAlpha(48)
            fx.setColor(c)
            self.setGraphicsEffect(fx)
        except Exception:
            pass

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── 标题行：图标 + 名称 + 状态 ──
        head = QHBoxLayout()
        head.setContentsMargins(12, 10, 12, 0)
        head.setSpacing(6)

        self.lbl_icon = QLabel()
        self.lbl_icon.setPixmap(icon("container", 22).pixmap(22, 22))
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
        grid.setContentsMargins(12, 8, 12, 10)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(3)

        self._rows: list[tuple[QLabel, QLabel]] = []
        for i, key in enumerate(("系统", "账户", "初始密码")):
            k = QLabel(key)
            k.setObjectName("cardKey")
            v = QLabel()
            v.setObjectName("cardValue")
            v.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
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
        sv.setContentsMargins(12, 10, 12, 10)
        sv.setSpacing(3)

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
        pv.setContentsMargins(12, 0, 12, 10)
        pv.setSpacing(3)

        self.lbl_ports_head = QLabel("端口转发")
        self.lbl_ports_head.setObjectName("cardSection")
        pv.addWidget(self.lbl_ports_head)

        self.lbl_ports = QLabel()
        self.lbl_ports.setObjectName("cardCmd")
        self.lbl_ports.setWordWrap(False)
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
        """填充并显示

        单例复用：内容变了只更新文本，不重建控件。
        """
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

        # 未运行提示
        if not running:
            self.lbl_hint.setText("未运行 · 启动后即可连接")
            self.lbl_hint.show()
        else:
            self.lbl_hint.hide()

        self.adjustSize()

    def hide_card(self) -> None:
        if self.isVisible():
            self.hide()
