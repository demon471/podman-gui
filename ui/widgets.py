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

from PyQt6.QtCore import (
    QPropertyAnimation,
    QRect,
    QTimer,
    Qt,
    pyqtProperty,
)
from PyQt6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
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

class AlignedItemDelegate(QStyledItemDelegate):
    """让列表里的图标与文字视觉居中对齐

    问题：Qt 把图标按「单元格矩形」垂直居中，文字却按字体基线
    居中。字体有下伸部（p/g/_ 等），文字的视觉中心被拉低，
    于是图标总是比文字高 2–3px —— 表格里一眼就能看出来。

    做法：先让基类照常画背景和文字（临时把图标换成透明占位，
    保留横向留白），再用样式算出的装饰矩形把真实图标画在
    「文字的视觉中心」高度上。
    """

    def __init__(self, view) -> None:
        super().__init__(view)
        # 记住视图：subElementRect 必须传一个真实 widget，
        # 传 None 会返回空矩形（那样就等于没生效）
        self._view = view

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        icon = opt.icon
        if icon.isNull():
            super().paint(painter, option, index)
            return

        view = self._view if self._view is not None else opt.widget
        style = view.style() if view is not None else QApplication.style()
        # 样式算出的真实装饰矩形：横向位置和尺寸都与 Qt 完全一致
        deco = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemDecoration, opt, view
        )
        if deco.width() <= 0 or deco.height() <= 0:
            super().paint(painter, option, index)
            return

        # 1) 背景 + 文字：用透明占位图标保留横向留白。
        #    这里必须直接调 style.drawControl —— 不能调 super().paint()，
        #    因为 QStyledItemDelegate.paint 内部会重新 initStyleOption，
        #    把图标又装回去，导致画两个图标。
        blank = QPixmap(deco.size())
        blank.fill(Qt.GlobalColor.transparent)
        text_opt = QStyleOptionViewItem(opt)
        text_opt.icon = QIcon(blank)
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, text_opt, painter, view
        )

        # 2) 自己把图标画在「文字的视觉中心」高度上
        fm = opt.fontMetrics
        text_top = opt.rect.top() + (opt.rect.height() - fm.height()) // 2
        baseline = text_top + fm.ascent()
        visual_center = baseline - fm.xHeight() / 2.0

        # 向上取整：图标字形在自身方框里略微偏下（约 0.5px），
        # 用 ceil 让「墨迹中心」而不是「方框中心」落在文字中心上
        import math

        y = int(math.ceil(visual_center - deco.height() / 2.0))
        # 不允许画出单元格
        y = max(opt.rect.top(),
                min(y, opt.rect.bottom() - deco.height() + 1))
        target = QRect(deco.left(), y, deco.width(), deco.height())
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if selected:
            # 选中行：图标要跟着文字变高亮色，否则深色图标
            # 压在蓝底上几乎看不见
            painter.drawPixmap(target, _tint_icon(icon, deco.size(),
                                                  opt.palette.highlightedText().color()))
        else:
            icon.paint(painter, target)


def _tint_icon(icon, size, color) -> QPixmap:
    """把图标整体染成指定颜色（保留透明度）

    QIcon 自身不认识「选中态」颜色，主题引擎只按窗口前景色渲染，
    所以选中行必须自己染色，否则深色图标压在选中蓝底上看不清。
    """
    src_pm = icon.pixmap(size)
    out = QPixmap(src_pm.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, src_pm)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), color)
    p.end()
    return out


def apply_aligned_delegate(view) -> None:
    """给列表/表格装上下图标对齐的委托（幂等）"""
    if isinstance(view.itemDelegate(), AlignedItemDelegate):
        return
    view.setItemDelegate(AlignedItemDelegate(view))


class EmptyState(QWidget):
    """列表空状态

    只显示：大图标 + 主标题（+ 可选的单行副说明）。

    刻意不带操作按钮：
      · 空状态里的按钮和工具栏上的按钮重复，还多占一块视觉重量
      · 「可以点下方按钮查看可用镜像…」这类文字是在教用户用界面，
        而按钮就在工具栏上，一眼就能看到

    副说明只留给「不是空、而是出错」的场景（比如 Podman 断开），
    那种情况下用户确实需要知道该怎么办。
    """

    def __init__(
        self,
        icon_name: str = "info",
        title: str = "暂无数据",
        subtitle: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("emptyState")

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(10)
        v.addStretch(1)

        from resources.icons import icon

        self._icon_label = QLabel()
        self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon_label.setPixmap(icon(icon_name, 48).pixmap(48, 48))
        self._icon_label.setObjectName("emptyIcon")
        v.addWidget(self._icon_label)

        self._title = QLabel(title)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setObjectName("emptyTitle")
        v.addWidget(self._title)

        self._subtitle = QLabel(subtitle)
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._subtitle.setObjectName("emptySubtitle")
        self._subtitle.setWordWrap(True)
        self._subtitle.setVisible(bool(subtitle))
        v.addWidget(self._subtitle)

        v.addStretch(1)

    def set_text(self, title: str, subtitle: str = "") -> None:
        self._title.setText(title)
        self._subtitle.setText(subtitle)
        self._subtitle.setVisible(bool(subtitle))


def install_empty_state(
    container_layout,
    table_widget,
    *,
    icon_name: str = "info",
    title: str = "暂无数据",
) -> EmptyState:
    """把空状态叠加到表格上（表格隐藏时显示）

    返回 EmptyState 实例，调用方根据行数切换可见性。
    """
    from PyQt6.QtWidgets import QStackedLayout

    state = EmptyState(
        icon_name=icon_name,
        title=title,
        parent=table_widget.parentWidget(),
    )
    # 直接作为兄弟控件插入布局
    container_layout.addWidget(state)
    state.hide()
    return state
