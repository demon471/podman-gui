"""日志查看器

布局：
  ┌─ 工具栏 ────────────────────────────────────────┐
  │ [刷新] [复制] [清屏]        [跟随●] [⏸] [⏹]     │
  ├─ 过滤栏 ────────────────────────────────────────┤
  │ 🔍 [输入关键词…                        ] 12/240 │
  ├─ 日志区 ────────────────────────────────────────┤
  │ 2026-10-24 15:35:11 [1] LOG: ...               │
  │ ...                                             │
  ├─ 状态栏 ────────────────────────────────────────┤
  │ 240 行 · 最后更新 15:35:12                       │
  └─────────────────────────────────────────────────┘
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QFont, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.config import LOG_MAX_LINES
from core.podman_client import PodmanClient, PodmanError
from resources.icons import ICON_SIZE_TOOLBAR, icon
from ui.widgets import AutoHidePlainTextEdit


class LogViewer(QDialog):
    """容器日志查看器（非模态，可同时打开多个）"""

    def __init__(
        self,
        client: PodmanClient,
        cid: str,
        name: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.client = client
        self.cid = cid
        self.name = name
        self._last_text = ""
        self._all_lines: list[str] = []

        self.setWindowTitle(f"日志 · {name}")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.resize(960, 640)
        self.setMinimumSize(560, 360)

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(2000)

        self._poll(initial=True)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ══════ 工具栏 ══════
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        self.act_refresh = QAction(icon("refresh"), "刷新", self)
        self.act_refresh.setProperty("iconName", "refresh")
        self.act_refresh.setToolTip("立即拉取最新日志")
        self.act_refresh.triggered.connect(lambda: self._poll(force=True))
        tb.addAction(self.act_refresh)

        self.act_copy = QAction(icon("save"), "复制", self)
        self.act_copy.setProperty("iconName", "save")
        self.act_copy.setToolTip("复制当前显示的全部内容")
        self.act_copy.triggered.connect(self._copy_all)
        tb.addAction(self.act_copy)

        self.act_clear = QAction(icon("clean"), "清屏", self)
        self.act_clear.setProperty("iconName", "clean")
        self.act_clear.setToolTip("清空显示（不删除容器日志）")
        self.act_clear.triggered.connect(self._clear_view)
        tb.addAction(self.act_clear)

        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().Policy.Expanding,
            spacer.sizePolicy().Policy.Preferred,
        )
        tb.addWidget(spacer)

        # 跟随开关（带状态点）
        self.chk_follow = QCheckBox("实时跟随")
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip("每 2 秒自动拉取新日志并滚动到底部")
        self.chk_follow.toggled.connect(self._on_follow_toggled)
        tb.addWidget(self.chk_follow)

        layout.addWidget(tb)

        # ══════ 过滤栏 ══════
        filter_bar = QFrame()
        filter_bar.setObjectName("filterBar")
        fb = QHBoxLayout(filter_bar)
        fb.setContentsMargins(10, 6, 10, 6)
        fb.setSpacing(8)

        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("过滤关键词（留空显示全部）")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedHeight(26)
        self.search.addAction(
            icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.search.textChanged.connect(self._apply_filter)
        fb.addWidget(self.search, 1)

        self.lbl_match = QLabel("")
        self.lbl_match.setObjectName("hintLabel")
        self.lbl_match.setMinimumWidth(70)
        self.lbl_match.setAlignment(Qt.AlignmentFlag.AlignRight)
        fb.addWidget(self.lbl_match)

        layout.addWidget(filter_bar)

        # ══════ 日志区 ══════
        self.view = AutoHidePlainTextEdit()
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(LOG_MAX_LINES)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.view.setFrameShape(QFrame.Shape.NoFrame)

        font = QFont("Maple Mono NF CN")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(QApplication.font().pointSize() or 10)
        self.view.setFont(font)

        layout.addWidget(self.view, 1)

        # ══════ 状态栏 ══════
        status = QFrame()
        status.setObjectName("logStatusBar")
        sb = QHBoxLayout(status)
        sb.setContentsMargins(10, 4, 10, 4)
        sb.setSpacing(10)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hintLabel")
        sb.addWidget(self.lbl_status, 1)

        self.lbl_updated = QLabel("")
        self.lbl_updated.setObjectName("hintLabel")
        sb.addWidget(self.lbl_updated)

        layout.addWidget(status)

    # ─────────────────────────────────────────
    # 数据
    # ─────────────────────────────────────────
    def _poll(self, initial: bool = False, force: bool = False) -> None:
        if not self.chk_follow.isChecked() and not force and not initial:
            return

        try:
            text = self.client.container_logs(self.cid, tail=LOG_MAX_LINES)
        except PodmanError as e:
            self.lbl_status.setText(f"获取日志失败：{e}")
            return

        if text == self._last_text:
            self.lbl_updated.setText(f"{self._now()}")
            return

        # 记录是否在底部
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 20

        self._last_text = text
        self._all_lines = text.splitlines()

        self._render()

        if at_bottom or initial:
            sb.setValue(sb.maximum())

        self.lbl_status.setText(f"{len(self._all_lines)} 行")
        self.lbl_updated.setText(self._now())

    def _now(self) -> str:
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S")

    def _render(self) -> None:
        """按当前过滤词渲染"""
        kw = self.search.text().strip()

        if not kw:
            lines = self._all_lines
            self.lbl_match.setText("")
        else:
            low = kw.lower()
            lines = [ln for ln in self._all_lines if low in ln.lower()]
            total = len(self._all_lines)
            self.lbl_match.setText(
                f"{len(lines)}/{total}" if total else "0"
            )

        if not lines:
            self.view.setPlainText(
                f"（无匹配「{kw}」的行）" if kw else "（暂无日志输出）"
            )
            return

        sb = self.view.verticalScrollBar()
        pos = sb.value()
        self.view.setPlainText("\n".join(lines))
        sb.setValue(pos)

    def _apply_filter(self, _kw: str = "") -> None:
        """过滤词变化"""
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 20

        self._render()

        if at_bottom or self.chk_follow.isChecked():
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _on_follow_toggled(self, on: bool) -> None:
        self.chk_follow.setText("实时跟随" if on else "已暂停")
        if on:
            self._poll(force=True)

    def _clear_view(self) -> None:
        """清空显示（不删除容器日志）"""
        self.view.clear()
        self.lbl_status.setText("已清屏（下次刷新会重新加载）")
        self.lbl_match.setText("")

    def _copy_all(self) -> None:
        text = self.view.toPlainText()
        QApplication.clipboard().setText(text)
        lines = len(text.splitlines())
        self.lbl_status.setText(f"已复制 {lines} 行到剪贴板")

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)


# ─────────────────────────────────────────
# 单例管理：同一容器只开一个窗口
# ─────────────────────────────────────────
_OPEN_VIEWERS: dict[str, LogViewer] = {}


def open_log_viewer(
    client: PodmanClient,
    cid: str,
    name: str,
    parent: QWidget | None = None,
) -> LogViewer:
    """打开日志窗口（复用已存在的）"""
    key = cid[:12]

    existing = _OPEN_VIEWERS.get(key)
    if existing is not None:
        try:
            if existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                existing._poll(force=True)
                return existing
        except RuntimeError:
            pass
        _OPEN_VIEWERS.pop(key, None)

    dlg = LogViewer(client, cid, name, parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    dlg.destroyed.connect(lambda _=None, k=key: _OPEN_VIEWERS.pop(k, None))
    _OPEN_VIEWERS[key] = dlg

    # 非模态：不阻塞主窗口
    dlg.show()
    return dlg
