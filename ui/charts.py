"""资源占用可视化

参考 Grafana Stat Panel / Docker Desktop 的做法：
  · 大数字作为视觉焦点
  · 一条堆叠进度条表达 composition（实际 / 声明 / 剩余）
  · 语义色随饱和度变化
  · 紧凑排布，不留无意义空白
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

COLOR_OK = "#27ae60"
COLOR_WARN = "#f39c12"
COLOR_DANGER = "#e74c3c"
COLOR_IDLE = "#95a5a6"


def _alpha(color, a: int) -> QColor:
    c = QColor(color)
    c.setAlpha(a)
    return c


def _usage_color(ratio: float) -> QColor:
    if ratio > 0.9:
        return QColor(COLOR_DANGER)
    if ratio > 0.7:
        return QColor(COLOR_WARN)
    return QColor(COLOR_OK)


class MemorySummary(QWidget):
    """内存总览

    ██████▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
    实际 0.39 · 声明 2.50 · 剩余 5.50 GB

    数值和状态徽标由外层卡片标题行显示，这里只画条形与统计。

    堆叠条含义：
      ███ 实心   = 实际占用
      ▓▓▓ 半透明 = 已声明但未使用
      ░░░ 轨道   = 剩余额度
    """

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(74)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._host_gb = 0.0
        self._limit_gb = 0.0
        self._declared_gb = 0.0
        self._actual_gb = 0.0

    def set_data(self, host_gb, limit_gb, declared_gb, actual_gb) -> None:
        self._host_gb = max(0.0, host_gb)
        self._limit_gb = max(0.0, limit_gb)
        self._declared_gb = max(0.0, declared_gb)
        self._actual_gb = max(0.0, actual_gb)
        self.update()

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()
        super().mousePressEvent(e)

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        pal = self.palette()
        fg = pal.color(pal.ColorRole.WindowText)
        dim = QColor(fg)
        dim.setAlpha(140)
        track = QColor(fg)
        track.setAlpha(20)

        base = self._limit_gb if self._limit_gb > 0 else self._host_gb
        act_ratio = (self._actual_gb / base) if base > 0 else 0.0
        dec_ratio = (self._declared_gb / base) if base > 0 else 0.0
        color = _usage_color(max(act_ratio, dec_ratio))

        m = 12
        w = self.width() - m * 2
        if w <= 0:
            p.end()
            return

        # ══════ 第二行：堆叠条 ══════
        bar_y = 2
        bar_h = 10
        r = bar_h / 2
        p.setPen(Qt.PenStyle.NoPen)

        # 轨道
        p.setBrush(track)
        p.drawRoundedRect(QRectF(m, bar_y, w, bar_h), r, r)

        # 声明值（半透明，覆盖在轨道上）
        if dec_ratio > 0:
            dw = min(1.0, dec_ratio) * w
            p.setBrush(_alpha(color, 75))
            p.drawRoundedRect(QRectF(m, bar_y, max(dw, bar_h), bar_h), r, r)

        # 实际值（实心）
        if act_ratio > 0:
            aw = min(1.0, act_ratio) * w
            p.setBrush(color)
            p.drawRoundedRect(QRectF(m, bar_y, max(aw, bar_h), bar_h), r, r)

        # 阈值标记（当有阈值且小于刻度时）
        if self._limit_gb > 0 and base > 0:
            lx = m + min(1.0, self._limit_gb / base) * w
            if lx < m + w - 2:
                pen = QPen(QColor(COLOR_WARN))
                pen.setWidth(2)
                p.setPen(pen)
                p.drawLine(int(lx), int(bar_y - 3), int(lx), int(bar_y + bar_h + 3))
                p.setPen(Qt.PenStyle.NoPen)

        # ══════ 第三行：明细 ══════
        f4 = p.font()
        f4.setPointSize(max(7, f4.pointSize() - 1))
        p.setFont(f4)
        p.setPen(QPen(dim))

        parts = [f"实际 {self._actual_gb:.2f} GB", f"声明 {self._declared_gb:.2f} GB"]
        if self._limit_gb > 0:
            remain = self._limit_gb - self._declared_gb
            if remain >= 0:
                parts.append(f"剩余 {remain:.2f} GB")
            else:
                parts.append(f"超出 {abs(remain):.2f} GB")

        p.drawText(m, bar_y + bar_h + 16, "  ·  ".join(parts))

        # 第四行：预算范围说明（容器与 VM 共用，用户常困惑的点）
        f5 = p.font()
        f5.setPointSize(max(7, f5.pointSize() - 1))
        p.setFont(f5)
        p.drawText(
            m, bar_y + bar_h + 30,
            "容器与虚拟机共用此预算 · 启动前会预检，防止宿主机 OOM",
        )

        p.end()


CARD_H = 62          # 每个占用卡片的高度
CARD_GAP = 10        # 卡片间距


def _card_bg(widget) -> QColor:
    """卡片底色：跟随主题的 Base（比窗口稍亮/稍暗）"""
    return widget.palette().color(widget.palette().ColorRole.Base)


def _border_color(widget) -> QColor:
    c = QColor(widget.palette().color(widget.palette().ColorRole.WindowText))
    c.setAlpha(45)
    return c


class ContainerRows(QWidget):
    """容器 + 虚拟机内存列表（两列网格布局）

    VM 与容器同屏展示（同一份内存预算），VM 项名称前带 VM 前缀。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._items: list[dict] = []
        self._host_gb = 1.0

    def set_items(self, items, scale_gb: float = 0, host_gb: float = 0,
                  vms=None) -> None:
        """items：容器列表；vms：虚拟机列表（同构 dict，标记 is_vm）"""
        merged = list(items or [])
        for v in (vms or []):
            v = dict(v)
            v["is_vm"] = True
            merged.append(v)

        self._items = sorted(
            [i for i in merged if i.get("state") == "running"],
            key=lambda x: (
                -(x.get("actual_gb", 0) / x.get("limit_gb", 1)
                  if x.get("limit_gb", 0) > 0 else 0)
            ),
        )
        self._host_gb = max(host_gb, 0.1)
        cols = 2
        rows = max(1, (len(self._items) + cols - 1) // cols)
        # 必须把行间距算进去，否则最后一行会被裁掉半截
        self.setMinimumHeight(rows * (CARD_H + CARD_GAP))
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        pal = self.palette()
        fg = pal.color(pal.ColorRole.WindowText)
        dim = QColor(fg)
        dim.setAlpha(150)
        track = QColor(fg)
        track.setAlpha(20)

        if not self._items:
            p.setPen(QPen(dim))
            p.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "无运行中的容器 / 虚拟机",
            )
            p.end()
            return

        cols = 2
        m = 0
        gap = 12
        cell_w = (self.width() - m * 2 - gap) / cols
        row_h = CARD_H + CARD_GAP
        bar_h = 6
        dot = 10

        f = p.font()
        f.setPointSize(max(7, f.pointSize() - 1))
        p.setFont(f)
        fm = p.fontMetrics()

        f_pct = p.font()
        f_pct.setPointSize(max(7, f_pct.pointSize() - 1))
        f_pct.setBold(True)
        fm_pct = QFont(f_pct)

        for idx, it in enumerate(self._items):
            col = idx % cols
            row = idx // cols
            card_x = m + col * (cell_w + gap)
            card_y = row * row_h

            # 卡片底：与设置页其他卡片一致（浅底 + 细边框 + 圆角）
            p.setPen(QPen(QColor(_border_color(self)), 1))
            p.setBrush(QColor(_card_bg(self)))
            p.drawRoundedRect(QRectF(card_x, card_y, cell_w, CARD_H), 6, 6)

            # 用局部变量算内边距，绝不能改动 cell_w ——
            # 它在循环外算好，被改掉会让下一列卡片错位、内容溢出边框
            cx = card_x + 12
            cy = card_y + 8
            inner_w = cell_w - 24

            name = it["name"]
            limit = it.get("limit_gb", 0)
            actual = it.get("actual_gb", 0)
            unlimited = it.get("unlimited", False)

            if it.get("is_vm"):
                # 虚拟机只展示当前占用，条形相对整机内存，
                # 颜色保持中性 —— 拿它跟自己的配置额度比会莫名报红
                ratio = (actual / self._host_gb) if self._host_gb > 0 else 0.0
                color = QColor(COLOR_IDLE)
                pct_text = ""
            elif unlimited or limit <= 0:
                color = QColor(COLOR_IDLE)
                ratio = 0.0
                pct_text = "∞"
            else:
                ratio = actual / limit
                color = _usage_color(ratio)
                pct_text = f"{int(round(ratio * 100))}%"

            # ── 第一行：状态点 + 名称 + 百分比 ──
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(QRectF(cx, cy + 5, 6, 6))

            p.setPen(QPen(fg))
            # VM 项加前缀，与容器区分（同一预算里的两类资源）
            if it.get("is_vm"):
                name = "VM · " + name
            elif it.get("project"):
                # 标出 compose 项目归属，一眼看出是哪个编排里的容器
                name = f"{name} · {it['project']}"
            name_max = inner_w - 60
            elided = fm.elidedText(name, Qt.TextElideMode.ElideRight, int(name_max))
            p.drawText(int(cx + dot + 2), int(cy + 11), elided)

            # 百分比（右侧，用语义色）
            # VM 不显示：既然只展示当前值，没有目标值作参照，
            # 百分比只会让人困惑（RSS 略超配置值时还会红成 104%）
            if not it.get("is_vm"):
                p.setFont(f_pct)
                p.setPen(QPen(color))
                fm2 = p.fontMetrics()
                pw = fm2.horizontalAdvance(pct_text)
                p.drawText(int(cx + inner_w - pw), int(cy + 11), pct_text)
                p.setFont(f)

            # ── 第二行：条形 ──
            by = cy + 19
            bar_w = inner_w
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(track)
            p.drawRoundedRect(
                QRectF(cx, by, bar_w, bar_h), bar_h / 2, bar_h / 2
            )

            fill = min(1.0, ratio)

            if fill > 0:
                aw = max(fill * bar_w, bar_h)
                p.setBrush(color)
                p.drawRoundedRect(
                    QRectF(cx, by, aw, bar_h), bar_h / 2, bar_h / 2
                )

            # ── 数值（条形下方小字）──
            p.setPen(QPen(dim))
            if it.get("is_vm"):
                # 虚拟机只显示当前占用（配置值是预留额度，不是用量）
                val = f"{actual:.2f} GB"
            elif unlimited:
                val = f"{actual:.2f} GB"
            else:
                val = f"{actual:.2f} / {limit:.2f} GB"
            p.drawText(int(cx), int(cy + 38), val)

        p.end()
