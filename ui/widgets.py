"""自动隐藏滚动条的文本控件

Qt 原生滚动条无法"不滚动时淡出"，这里通过覆写 QPlainTextEdit
的 paintEvent 在视口内自绘滚动指示器，并把原生滚动条完全隐藏。

行为：
- 平时不可见，内容区占满
- 滚动、悬停、编辑时淡入
- 静止 1.2 秒后淡出
- 拖动滑块可跳转
"""

from __future__ import annotations

from PyQt6.QtCore import QPropertyAnimation, QTimer, Qt, pyqtProperty
from PyQt6.QtGui import QColor, QPainter, QPalette
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 指示器尺寸
BAR_WIDTH = 5          # 滑块宽度（细）
BAR_RESERVE = BAR_WIDTH + 1            # viewport 右侧预留宽度
BAR_MIN_HEIGHT = 16    # 滑块最小高度（短）
BAR_MAX_RATIO = 0.38   # 滑块最长不超过可视高度的 38%
FADE_MS = 220          # 淡入淡出时长
IDLE_MS = 1200         # 静止多久后隐藏


class AutoHidePlainTextEdit(QPlainTextEdit):
    """滚动条自动隐藏的纯文本编辑器"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # 彻底关掉原生滚动条
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # 右侧预留出指示器空间（文字不会跑到滑块下面）
        self.setViewportMargins(0, 0, BAR_RESERVE, 0)

        self._opacity = 0.0
        self._hover = False
        self._dragging = False

        # 透明度动画
        self._anim = QPropertyAnimation(self, b"barOpacity", self)
        self._anim.setDuration(FADE_MS)

        # 静止计时器
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(IDLE_MS)
        self._idle.timeout.connect(self._fade_out)

        # 监听滚动
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(self._on_scroll)
        sb.rangeChanged.connect(lambda *_: self._update())

        self.setMouseTracking(True)

    # ─────────────────────────────────────────
    # 透明度属性（供动画驱动）
    # ─────────────────────────────────────────
    def get_bar_opacity(self) -> float:
        return self._opacity

    def set_bar_opacity(self, v: float) -> None:
        self._opacity = max(0.0, min(1.0, float(v)))
        self.viewport().update()

    barOpacity = pyqtProperty(float, fget=get_bar_opacity, fset=set_bar_opacity)

    # ─────────────────────────────────────────
    # 显隐控制
    # ─────────────────────────────────────────
    def _fade_in(self) -> None:
        self._idle.stop()
        if self._opacity < 0.99:
            self._anim.stop()
            self._anim.setEndValue(1.0)
            self._anim.start()

    def _fade_out(self) -> None:
        if self._hover or self._dragging:
            return
        self._anim.stop()
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _on_scroll(self, _v: int) -> None:
        self._update()
        if not self._dragging:
            self._fade_in()
            self._idle.start()

    def _update(self) -> None:
        self.viewport().update()

    # ─────────────────────────────────────────
    # 几何
    # ─────────────────────────────────────────
    def _bar_rect(self):
        """计算滑块矩形（控件坐标，紧贴右边缘）"""
        vp = self.viewport()
        sb = self.verticalScrollBar()
        vh = vp.height()
        if vh <= 0:
            return None

        lo, hi = sb.minimum(), sb.maximum()
        span = hi - lo
        if span <= 0:
            return None

        # 可见比例 → 滑块高度
        page = sb.pageStep()
        total = span + page
        if total <= 0:
            return None

        ratio = page / total
        h = max(BAR_MIN_HEIGHT, int(vh * ratio))
        # 限制最大长度，避免内容稍长时滑块占满整条
        h = min(h, int(vh * BAR_MAX_RATIO))
        h = max(h, BAR_MIN_HEIGHT)

        # 滚动位置 → 滑块偏移
        pos = (sb.value() - lo) / span if span else 0.0
        y = int((vh - h) * pos)

        # viewport 坐标：贴在 viewport 右边缘（预留区内侧）
        x = vp.width() - BAR_WIDTH
        return x, y, BAR_WIDTH, h

    # ─────────────────────────────────────────
    # 绘制
    # ─────────────────────────────────────────
    def paintEvent(self, e) -> None:
        super().paintEvent(e)

        if self._opacity <= 0.01:
            return
        rect = self._bar_rect()
        if rect is None:
            return

        x, y, w, h = rect
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 颜色跟随主题
        base = self.palette().color(QPalette.ColorRole.WindowText)
        color = QColor(base)
        # 悬停或拖动时更明显
        alpha = 0.55 if (self._hover or self._dragging) else 0.32
        color.setAlphaF(alpha * self._opacity)

        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        r = w / 2.0
        p.drawRoundedRect(x, y, w, h, r, r)
        p.end()

    # ─────────────────────────────────────────
    # 鼠标交互
    # ─────────────────────────────────────────
    def _in_bar(self, pos) -> bool:
        """pos 为控件坐标"""
        rect = self._bar_rect()
        if rect is None:
            return False
        x, y, w, h = rect
        return (
            pos.x() >= x - 2
            and pos.y() >= y
            and pos.y() <= y + h
        )

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._in_bar(e.pos()):
            self._dragging = True
            self._fade_in()
            self._jump_to(e.pos().y())
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._dragging:
            self._jump_to(e.pos().y())
            return

        was_hover = self._hover
        self._hover = self._in_bar(e.pos())
        if self._hover != was_hover:
            if self._hover:
                self._fade_in()
            else:
                self._idle.start()
            self.viewport().update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._dragging:
            self._dragging = False
            self._idle.start()
            self.viewport().update()
            return
        super().mouseReleaseEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        self._idle.start()
        self.viewport().update()
        super().leaveEvent(e)

    def _jump_to(self, y: int) -> None:
        """把点击位置（控件坐标）映射成滚动值"""
        rect = self._bar_rect()
        if rect is None:
            return
        _x, by, _w, bh = rect
        vh = self.viewport().height()
        sb = self.verticalScrollBar()

        # 滑块中心对齐鼠标
        target = (y - bh / 2) / max(1, vh - bh)
        target = max(0.0, min(1.0, target))
        sb.setValue(int(sb.minimum() + target * (sb.maximum() - sb.minimum())))

    # ─────────────────────────────────────────
    # 内容变化 / 尺寸变化
    # ─────────────────────────────────────────
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._update()

    def wheelEvent(self, e) -> None:
        super().wheelEvent(e)
        self._fade_in()
        self._idle.start()

    def keyPressEvent(self, e) -> None:
        super().keyPressEvent(e)
        self._fade_in()
        self._idle.start()

    def setPlainText(self, text: str) -> None:
        super().setPlainText(text)
        self._update()

    def appendPlainText(self, text: str) -> None:
        super().appendPlainText(text)
        self._update()


# ─────────────────────────────────────────
# 统一的输入对话框
#
# QInputDialog 尺寸由 Qt 自动决定，通常很小（约 240px 宽），
# 输入长字符串（镜像引用、网络名等）时看不清。这里统一封装。
# ─────────────────────────────────────────

def ask_text(
    parent,
    title: str,
    label: str,
    *,
    default: str = "",
    placeholder: str = "",
    hint: str = "",
    width: int = 640,
    height: int = 200,
) -> tuple[str, bool]:
    """大尺寸文本输入对话框

    返回 (输入内容, 是否确认)
    """
    from PyQt6.QtWidgets import (
    QWidget,
    QPushButton,
    QHBoxLayout,
        QDialog,
        QDialogButtonBox,
        QLabel,
        QLineEdit,
        QVBoxLayout,
    )

    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(width)
    dlg.resize(width, height)

    v = QVBoxLayout(dlg)
    v.setContentsMargins(18, 16, 18, 14)
    v.setSpacing(10)

    lbl = QLabel(label)
    v.addWidget(lbl)

    ed = QLineEdit(default)
    ed.setMinimumHeight(36)
    ed.setMinimumWidth(width - 80)
    if placeholder:
        ed.setPlaceholderText(placeholder)
    ed.setClearButtonEnabled(True)
    ed.selectAll()
    v.addWidget(ed)

    if hint:
        hl = QLabel(hint)
        hl.setObjectName("hintLabel")
        hl.setWordWrap(True)
        v.addWidget(hl)

    v.addStretch()

    btns = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel
    )
    btns.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
    btns.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    btns.accepted.connect(dlg.accept)
    btns.rejected.connect(dlg.reject)
    v.addWidget(btns)

    ed.setFocus()

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return "", False

    return ed.text().strip(), True


# ─────────────────────────────────────────
# 空状态提示
# ─────────────────────────────────────────

class EmptyState(QWidget):
    """列表空状态

    居中显示：大图标 + 主标题 + 副说明 + 可选操作按钮
    """

    def __init__(
        self,
        icon_name: str = "info",
        title: str = "暂无数据",
        subtitle: str = "",
        action_text: str = "",
        on_action=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("emptyState")

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(10)
        v.addStretch(1)

        # 图标
        from resources.icons import icon

        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setPixmap(
            icon(icon_name, 48).pixmap(48, 48)
        )
        self._icon_label.setObjectName("emptyIcon")
        v.addWidget(self._icon_label)

        # 标题
        self._title = QLabel(title)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setObjectName("emptyTitle")
        v.addWidget(self._title)

        # 副说明
        if subtitle:
            self._subtitle = QLabel(subtitle)
            self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._subtitle.setObjectName("emptySubtitle")
            self._subtitle.setWordWrap(True)
            v.addWidget(self._subtitle)
        else:
            self._subtitle = None

        # 操作按钮
        if action_text and on_action:
            row = QHBoxLayout()
            row.addStretch()
            btn = QPushButton(action_text)
            btn.clicked.connect(on_action)
            row.addWidget(btn)
            row.addStretch()
            v.addSpacing(6)
            v.addLayout(row)
            self._action_btn = btn
        else:
            self._action_btn = None

        v.addStretch(1)

    def set_text(self, title: str, subtitle: str = "") -> None:
        self._title.setText(title)
        if self._subtitle is not None:
            self._subtitle.setText(subtitle)


def install_empty_state(
    container_layout,
    table_widget,
    *,
    icon_name: str = "info",
    title: str = "暂无数据",
    subtitle: str = "",
    action_text: str = "",
    on_action=None,
) -> EmptyState:
    """把空状态叠加到表格上（表格隐藏时显示）

    返回 EmptyState 实例，调用方根据行数切换可见性。
    """
    from PyQt6.QtWidgets import QStackedLayout

    state = EmptyState(
        icon_name=icon_name,
        title=title,
        subtitle=subtitle,
        action_text=action_text,
        on_action=on_action,
        parent=table_widget.parentWidget(),
    )
    # 直接作为兄弟控件插入布局
    container_layout.addWidget(state)
    state.hide()
    return state
