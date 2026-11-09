"""Compose 项目管理页

左右分栏：
  左侧：项目列表（树形，项目 → 服务）
  右侧：详情 / YAML 编辑器（标签页）

工具栏：新建 / 启动 / 停止 / 重启 / 拉取 / 日志 / 刷新 / 切换根目录
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import (
    QStyledItemDelegate,
    QApplication,
    QFrame,
    QDialog,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import compose_manager as cm
from core.async_task import run_async
from core.notify import notifier
from core.podman_client import PodmanClient
from resources.icons import ICON_SIZE_TOOLBAR, icon, state_icon
from ui.widgets import make_list_card, apply_aligned_delegate, AutoHidePlainTextEdit, ask_text

# 默认根目录（可在界面切换，记住到 QSettings）
DEFAULT_COMPOSE_ROOT = "/data"

# 状态着色
STATE_COLORS = {
    "running": "#27ae60",
    "exited": "#95a5a6",
    "created": "#3498db",
    "paused": "#f39c12",
    "missing": "#bdc3c7",
}

STATE_TEXT = {
    "running": "运行中",
    "exited": "已停止",
    "created": "已创建",
    "paused": "已暂停",
    "missing": "未创建",
}

class YamlHighlighter(QSyntaxHighlighter):
    """极简 YAML 语法高亮：键 / 字符串 / 注释 / 数字 / 布尔"""

    def __init__(self, doc):
        super().__init__(doc)
        self.fmt_key = QTextCharFormat()
        self.fmt_key.setForeground(QColor("#3daee9"))
        self.fmt_key.setFontWeight(QFont.Weight.DemiBold)

        self.fmt_str = QTextCharFormat()
        self.fmt_str.setForeground(QColor("#27ae60"))

        self.fmt_num = QTextCharFormat()
        self.fmt_num.setForeground(QColor("#e67e22"))

        self.fmt_comment = QTextCharFormat()
        self.fmt_comment.setForeground(QColor("#7f8c8d"))
        self.fmt_comment.setFontItalic(True)

        self.fmt_bool = QTextCharFormat()
        self.fmt_bool.setForeground(QColor("#9b59b6"))

        self.fmt_dash = QTextCharFormat()
        self.fmt_dash.setForeground(QColor("#95a5a6"))

    def highlightBlock(self, text: str) -> None:
        import re

        # 注释
        i = text.find("#")
        if i >= 0:
            # 忽略引号内的 #
            before = text[:i]
            if before.count('"') % 2 == 0 and before.count("'") % 2 == 0:
                self.setFormat(i, len(text) - i, self.fmt_comment)
                text = before

        # 键
        m = re.match(r"^(\s*-?\s*)([\w.-]+)(\s*:)", text)
        if m:
            self.setFormat(m.start(2), len(m.group(2)), self.fmt_key)

        # 列表破折号
        for m in re.finditer(r"^\s*(-)\s", text):
            self.setFormat(m.start(1), 1, self.fmt_dash)

        # 字符串（引号内）
        for m in re.finditer(r'"[^"]*"|\'[^\']*\'', text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_str)

        # 布尔
        for m in re.finditer(r"\b(true|false|yes|no|null|~)\b", text, re.I):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_bool)

        # 数字
        for m in re.finditer(r"\b\d+(\.\d+)?\b", text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_num)

class TreeLineDelegate(QStyledItemDelegate):
    """在树的第一列自绘分支连线（├ / └ / 引导线）

    QSS 已压掉 Breeze 原生的引导线（style.py 的
    ::branch:!has-children 规则），展开箭头仍由原生绘制；
    连线在这里补上，几何完全可控。

    ══ 坐标系（PyQt6 + Breeze 实测）══
    Qt 传给 delegate 的 painter 原点是 **viewport 原点**，
    与 option.rect 同一坐标系 —— 不是 option.rect.topLeft()
    （9fe0022 当时的注释搞错了这一点，线全部画到负坐标上
    不可见，实际显示的是被 QSS padding 推偏的原生线）。

    缩进槽布局（rootIsDecorated=True，viewport 坐标）：
      · 第 k 层槽 = [k*indent, (k+1)*indent)
      · depth 层项的内容从 (depth+1)*indent 开始（顶层也占
        一个箭头槽），即 option.rect.x() = (depth+1)*indent
      · 槽 k 中心 x = k*indent + indent//2
      · 父链槽 = 0..depth-1（引导线），自己的拐角槽 = depth

    线画在 super().paint() **之前**：内容/选中高亮盖在线上，
    不会出现深色分支线压在蓝色选中条上（与 QTreeView
    drawRow 先画分支后画 item 的顺序一致）。
    """

    def paint(self, painter, option, index) -> None:
        tree = self.parent()
        if (
            tree is not None
            and index.column() == 0
            and tree.indentation() > 0
            and index.parent().isValid()
        ):
            self._draw_branches(painter, option, index, tree)
        super().paint(painter, option, index)

    def _draw_branches(self, painter, option, index, tree) -> None:
        from PyQt6.QtGui import QColor, QPainter, QPen

        indent = tree.indentation()
        model = tree.model()

        depth = 0
        it = index.parent()
        while it.isValid():
            depth += 1
            it = it.parent()

        rect = option.rect
        mid_y = rect.center().y()

        col = QColor(option.palette.color(
            option.palette.ColorRole.WindowText
        ))
        col.setAlpha(70)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(col)
        pen.setWidth(1)
        painter.setPen(pen)

        def row_has_next(i) -> bool:
            return i.row() < model.rowCount(i.parent()) - 1

        # 父链槽 0..depth-1：引导线。
        # 延续条件与 QCommonStyle 的 State_Sibling 一致：
        #   · 直接父层槽（depth-1）：本项后面还有兄弟 → 贯穿整行
        #   · 更外层槽 k：第 k 层祖先还有兄弟 → 贯穿，否则止于本行中部
        for k in range(depth):
            x = k * indent + indent // 2
            if k < depth - 1:
                ancestor = index
                for _ in range(depth - 1 - k):
                    ancestor = ancestor.parent()
                cont = row_has_next(ancestor)
            else:
                cont = row_has_next(index)
            if cont:
                painter.drawLine(x, rect.top(), x, rect.bottom() + 1)
            else:
                painter.drawLine(x, rect.top(), x, mid_y)

        # 自己的拐角槽（depth）：└ 或 ├ + 横线接到内容起点
        x = depth * indent + indent // 2
        if row_has_next(index):
            painter.drawLine(x, rect.top(), x, rect.bottom() + 1)
        else:
            painter.drawLine(x, rect.top(), x, mid_y)
        painter.drawLine(x, mid_y, rect.x(), mid_y)

        painter.restore()


class ComposePage(QWidget):
    """Compose 项目管理"""

    status_message = pyqtSignal(str)
    request_logs = pyqtSignal(str)  # 项目名，由主窗口处理

    def __init__(self, client: PodmanClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self.root = Path(DEFAULT_COMPOSE_ROOT)
        self.projects: list[cm.ComposeProject] = []
        self._current: cm.ComposeProject | None = None
        self._dirty = False
        self._loading = False
        self._tasks: list = []

        self._build_ui()
        self._load_root_setting()
        QTimer.singleShot(100, self.refresh)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def refresh_icons(self) -> None:
        """主题切换后重建图标（QIconEngine 需重新绑定）"""
        if hasattr(self, 'act_new'):
            self.act_new.setIcon(icon("add"))
        if hasattr(self, "act_delete"):
            self.act_delete.setIcon(icon("delete"))
        if hasattr(self, 'act_up'):
            self.act_up.setIcon(icon("play"))
        if hasattr(self, 'act_down'):
            self.act_down.setIcon(icon("stop"))
        if hasattr(self, 'act_restart'):
            self.act_restart.setIcon(icon("restart"))
        if hasattr(self, 'act_pull'):
            self.act_pull.setIcon(icon("down"))
        if hasattr(self, 'act_logs'):
            self.act_logs.setIcon(icon("log"))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 工具栏 ──
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        self.act_new = QAction(icon("add"), "新建", self)

        self.act_new.setProperty("iconName", "add")
        self.act_new.triggered.connect(self._new_project)
        tb.addAction(self.act_new)

        self.act_delete = QAction(icon("delete"), "删除", self)
        self.act_delete.setProperty("iconName", "delete")
        self.act_delete.triggered.connect(self._delete_project)
        tb.addAction(self.act_delete)

        self.act_up = QAction(icon("play"), "启动", self)

        self.act_up.setProperty("iconName", "play")
        self.act_up.triggered.connect(lambda: self._run("up"))
        tb.addAction(self.act_up)

        self.act_down = QAction(icon("stop"), "停止", self)

        self.act_down.setProperty("iconName", "stop")
        self.act_down.triggered.connect(lambda: self._run("down"))
        tb.addAction(self.act_down)

        self.act_restart = QAction(icon("restart"), "重启", self)

        self.act_restart.setProperty("iconName", "restart")
        self.act_restart.triggered.connect(lambda: self._run("restart"))
        tb.addAction(self.act_restart)

        self.act_pull = QAction(icon("down"), "拉取", self)

        self.act_pull.setProperty("iconName", "down")
        self.act_pull.triggered.connect(lambda: self._run("pull"))
        tb.addAction(self.act_pull)

        self.act_logs = QAction(icon("log"), "日志", self)

        self.act_logs.setProperty("iconName", "log")
        self.act_logs.triggered.connect(self._show_logs)
        tb.addAction(self.act_logs)

        # 「保存 / 刷新 / 根目录 / 浏览」已移除：
        #   · 保存：YAML 编辑器已改为自动保存（切走/失焦时落盘）
        #   · 刷新：项目列表本来就定时刷新，页面切换也会刷
        #   · 根目录：几乎不会改，改也是去设置里一次搞定
        # 工具栏只留真正会反复用的操作，一行放下不挤。

        layout.addWidget(tb)

        # ── 主分割区 ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 左侧：项目树
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(8, 8, 4, 8)
        lv.setSpacing(6)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["项目 / 服务", "状态"])
        self.tree.setRootIsDecorated(True)
        # 交替行底色会让树的分支连线看起来断成一截一截
        self.tree.setAlternatingRowColors(False)
        self.tree.setIndentation(18)
        self.tree.setUniformRowHeights(True)
        # 自己画分支线：QSS 的 ::branch 规则在不同 Breeze 版本下
        # 表现不一致（要么断线要么丢箭头），自绘最稳
        self.tree.setItemDelegate(TreeLineDelegate(self.tree))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.tree.header().setFixedHeight(26)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 100)
        self.tree.currentItemChanged.connect(self._on_tree_select)
        self.tree.itemDoubleClicked.connect(self._on_tree_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_context_menu)
        # 与其他列表统一：圆角白底卡片
        lv.addWidget(make_list_card(self.tree), 1)

        self.lbl_stats = QLabel("")
        self.lbl_stats.setObjectName("hintLabel")
        self.lbl_stats.setContentsMargins(8, 4, 8, 4)
        lv.addWidget(self.lbl_stats)

        splitter.addWidget(left)

        # 右侧：详情 / 编辑器
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(), "详情")
        self.tabs.addTab(self._build_yaml_tab(), "YAML")
        rv.addWidget(self.tabs)

        splitter.addWidget(right)
        splitter.setSizes([320, 700])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        layout.addWidget(splitter, 1)

        self._update_actions()

    def _build_overview_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 项目信息：标题在卡片外，卡片只装内容
        t1 = QLabel("项目信息")
        t1.setObjectName("sectionTitle")
        layout.addWidget(t1)

        grp = QFrame()
        grp.setObjectName("card")
        gv = QVBoxLayout(grp)
        gv.setContentsMargins(16, 12, 16, 14)
        gv.setSpacing(0)

        f = QFormLayout()
        f.setContentsMargins(0, 0, 0, 0)
        f.setSpacing(6)
        self.lbl_name = QLabel("—")
        self.lbl_name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        f.addRow("名称:", self.lbl_name)

        # 路径用只读输入框：不截断、可选中复制、可横向滚动
        self.ed_path = QLineEdit()
        self.ed_path.setReadOnly(True)
        self.ed_path.setFrame(False)
        self.ed_path.setStyleSheet(
            "background: transparent; padding: 0;"
        )
        f.addRow("文件:", self.ed_path)

        self.ed_data = QLineEdit()
        self.ed_data.setReadOnly(True)
        self.ed_data.setFrame(False)
        self.ed_data.setStyleSheet("background: transparent; padding: 0;")
        f.addRow("数据目录:", self.ed_data)
        gv.addLayout(f)
        layout.addWidget(grp)

        # 服务：标题在卡片外，卡片只装表格
        t2 = QLabel("服务")
        t2.setObjectName("sectionTitle")
        layout.addWidget(t2)

        grp2 = QFrame()
        # 用 listCard：表格贴边不留缝，表头首尾自带圆角
        grp2.setObjectName("listCard")
        g2 = QVBoxLayout(grp2)
        g2.setContentsMargins(0, 0, 0, 0)
        g2.setSpacing(0)

        self.tbl_svc = QTableWidget(0, 5)
        # 图标与文字视觉居中（Qt 默认会让图标高 2–3px）
        apply_aligned_delegate(self.tbl_svc)
        self.tbl_svc.setHorizontalHeaderLabels(
            ["服务", "状态", "镜像", "端口", "内存"]
        )
        self.tbl_svc.verticalHeader().setVisible(False)
        # 行号列已隐藏，左上角那个方块要去掉，否则卡片里多一块白
        self.tbl_svc.setCornerButtonEnabled(False)
        self.tbl_svc.verticalHeader().setDefaultSectionSize(26)
        self.tbl_svc.setShowGrid(False)
        self.tbl_svc.setFrameShape(QTableWidget.Shape.NoFrame)
        self.tbl_svc.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl_svc.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.tbl_svc.setAlternatingRowColors(True)
        h = self.tbl_svc.horizontalHeader()
        h.setFixedHeight(26)
        h.setHighlightSections(False)
        # 最后一列拉伸填满，避免内容比卡片宽时冒出横向滚动条
        h.setStretchLastSection(True)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)   # 镜像占剩余空间
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        # 右侧面板不宽，列宽要克制，否则会撑出横向滚动条
        self.tbl_svc.setColumnWidth(0, 78)
        self.tbl_svc.setColumnWidth(1, 92)     # 状态要放得下图标 +「运行中」
        self.tbl_svc.setColumnWidth(3, 92)
        self.tbl_svc.setColumnWidth(4, 116)
        g2.addWidget(self.tbl_svc, 1)
        layout.addWidget(grp2, 1)

        return w

    def _build_yaml_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        # 右侧最小间隙（编辑器自身已有指示器留白）
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        bar = QWidget()
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)

        self.lbl_yaml_hint = QLabel("选择项目后可编辑")
        self.lbl_yaml_hint.setObjectName("hintLabel")
        bl.addWidget(self.lbl_yaml_hint, 1)

        btn_revert = QPushButton("还原")
        btn_revert.clicked.connect(self._load_yaml)
        bl.addWidget(btn_revert)

        btn_validate = QPushButton("校验")
        btn_validate.clicked.connect(self._validate_yaml)
        bl.addWidget(btn_validate)

        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save_yaml)
        bl.addWidget(btn_save)

        layout.addWidget(bar)

        self.ed_yaml = AutoHidePlainTextEdit()
        self.ed_yaml.setObjectName("logView")
        # 自动换行：长行折行显示
        self.ed_yaml.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.ed_yaml.setTabStopDistance(24)
        self._highlighter = YamlHighlighter(self.ed_yaml.document())
        self.ed_yaml.textChanged.connect(self._on_yaml_changed)
        # 失焦即保存：没有「保存」按钮，不能靠用户记得手动存
        self.ed_yaml.focusOutEvent = self._yaml_focus_out(self.ed_yaml)
        layout.addWidget(self.ed_yaml, 1)

        return w

    # ─────────────────────────────────────────
    # 根目录设置
    # ─────────────────────────────────────────
    def _load_root_setting(self) -> None:
        """读 Compose 根目录（存 SQLite）"""
        from core import state

        root = state.get_setting("compose/root", str(DEFAULT_COMPOSE_ROOT))
        if root and Path(str(root)).is_dir():
            self.root = Path(str(root))

    def _save_root_setting(self) -> None:
        """保存 Compose 根目录（存 SQLite）"""
        from core import state

        state.set_setting("compose/root", str(self.root))

    def set_root(self, path: str) -> None:
        """设置 Compose 根目录（由设置页调用，界面上不再有入口）"""
        p = Path((path or "").strip())
        if not p.is_dir():
            QMessageBox.warning(self, "目录不存在", f"{p} 不是一个目录")
            return
        self.root = p
        self._save_root_setting()
        self.refresh()

    # ─────────────────────────────────────────
    # 刷新（异步扫描）
    # ─────────────────────────────────────────
    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.status_message.emit(f"正在扫描 {self.root}…")

        root = self.root

        def load():
            projects = cm.scan_root(root)
            containers = []
            try:
                containers = self.client.list_containers()
            except Exception:
                pass
            statuses = {
                p.name: cm.get_status(p, containers) for p in projects
            }
            return projects, statuses

        def done(result):
            projects, statuses = result
            self.projects = projects
            self._populate_tree(statuses)
            self._loading = False
            pass
            self.status_message.emit(
                f"{root} 下找到 {len(projects)} 个 Compose 项目"
            )

        def error(msg):
            self._loading = False
            pass
            self.status_message.emit(f"扫描失败: {msg}")

        task = run_async(load, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    def refresh_silent(self) -> None:
        self.refresh()

    def _populate_tree(self, statuses: dict[str, dict[str, str]]) -> None:
        # 记住展开和选中
        expanded = set()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.isExpanded():
                expanded.add(it.text(0))
        sel_name = self._current.name if self._current else None

        self.tree.clear()

        # 无项目时显示空状态提示
        if not self.projects:
            hint = QTreeWidgetItem(["（此目录下没有 Compose 项目）", ""])
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            hint.setForeground(0, QColor("#95a5a6"))
            self.tree.addTopLevelItem(hint)
            self.lbl_stats.setText(
                f"0 个项目 · 请检查根目录 {self.root} 或点「新建」创建"
            )
            self._current = None
            self._update_actions()
            return

        total_svc = 0
        total_running = 0

        for proj in self.projects:
            st = statuses.get(proj.name, {})
            running = sum(1 for v in st.values() if v == "running")
            total_svc += proj.service_count
            total_running += running

            item = QTreeWidgetItem([
                proj.name,
                f"{running}/{proj.service_count}",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, proj.name)
            item.setToolTip(0, str(proj.path))

            # 图标
            item.setIcon(0, icon("compose"))

            # 状态着色 + 图标
            if running == proj.service_count and proj.service_count > 0:
                item.setForeground(1, QColor("#27ae60"))
                _pstate = "running"
            elif running > 0:
                item.setForeground(1, QColor("#f39c12"))
                _pstate = "paused"
            else:
                item.setForeground(1, QColor("#95a5a6"))
                _pstate = "exited"
            item.setData(1, Qt.ItemDataRole.UserRole + 20, _pstate)
            item.setIcon(1, state_icon(_pstate, 14))

            # 解析错误提示
            if proj.error:
                item.setForeground(0, QColor("#e74c3c"))
                item.setToolTip(0, f"{proj.path}\n⚠️ {proj.error}")

            # 子项：服务
            for svc in proj.services:
                state = st.get(svc.name, "missing")
                child = QTreeWidgetItem([
                    svc.name,
                    STATE_TEXT.get(state, state),
                ])
                _istate = {"running": "running", "exited": "exited",
                           "created": "created", "paused": "paused",
                           "missing": "exited"}.get(state, "exited")
                child.setData(1, Qt.ItemDataRole.UserRole + 20, _istate)
                child.setIcon(1, state_icon(_istate, 14))
                child.setData(0, Qt.ItemDataRole.UserRole, None)
                child.setData(0, Qt.ItemDataRole.UserRole + 1, proj.name)
                child.setData(0, Qt.ItemDataRole.UserRole + 2, svc.name)
                child.setIcon(0, icon("container"))
                color = STATE_COLORS.get(state, "#95a5a6")
                child.setForeground(1, QColor(color))
                child.setToolTip(0, f"{svc.image}\n端口: {svc.port_summary}")
                item.addChild(child)

            self.tree.addTopLevelItem(item)
            if proj.name in expanded or not expanded:
                item.setExpanded(True)

            if sel_name and proj.name == sel_name:
                self.tree.setCurrentItem(item)

        self.lbl_stats.setText(
            f"{len(self.projects)} 个项目 · {total_svc} 个服务 · "
            f"{total_running} 个运行中"
        )

        if not self.projects:
            # 清空右侧详情，避免残留旧数据
            self._current = None
            self.lbl_name.setText("—")
            self.ed_path.setText("")
            self.ed_data.setText("")
            self.tbl_svc.setRowCount(0)
            self.ed_yaml.setPlainText(
                "# 还没有选择 Compose 项目\n"
                "#\n"
                "# 请从左侧选择项目，或点工具栏「新建」创建。\n"
                "#\n"
                "# 新建的项目会生成这样的目录结构：\n"
                "#   <根目录>/<项目名>/compose/docker-compose.yml\n"
                "#   <根目录>/<项目名>/data/\n"
                "#   <根目录>/<项目名>/config/"
            )
            self.lbl_yaml_hint.setText("尚未选择项目")
            self._update_actions()
            self.status_message.emit(
                f"{self.root} 下未找到 Compose 项目"
            )
            return

        # 未选中任何项目时，自动选第一个（避免右侧空白）
        if self._current is None and self.tree.topLevelItemCount() > 0:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))

    # ─────────────────────────────────────────
    # 选择
    # ─────────────────────────────────────────
    def _project_by_name(self, name: str) -> cm.ComposeProject | None:
        return next((p for p in self.projects if p.name == name), None)

    def _on_tree_select(self, cur: QTreeWidgetItem | None, _prev) -> None:
        # 切到别的项目前先把未保存的 YAML 落盘
        # （工具栏的「保存」已移除，不能指望用户手动点）
        self._autosave_yaml()

        if cur is None:
            self._current = None
            self._update_actions()
            return

        # 可能是子项（服务）→ 取父项目
        proj_name = cur.data(0, Qt.ItemDataRole.UserRole)
        if proj_name is None:
            proj_name = cur.data(0, Qt.ItemDataRole.UserRole + 1)

        proj = self._project_by_name(proj_name) if proj_name else None
        if proj is None:
            self._current = None
            self._update_actions()
            return

        changed = self._current is not proj
        self._current = proj

        if changed:
            self._show_overview(proj)
            self._load_yaml()

        self._update_actions()

    def _on_tree_double_click(self, item: QTreeWidgetItem, _col: int) -> None:
        svc_name = item.data(0, Qt.ItemDataRole.UserRole + 2)
        proj_name = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if svc_name and proj_name:
            self.request_logs.emit(f"{proj_name}/{svc_name}")
        elif item.data(0, Qt.ItemDataRole.UserRole):
            self.tabs.setCurrentIndex(1)  # 双击项目 → 切到 YAML

    def _show_overview(self, proj: cm.ComposeProject) -> None:
        self.lbl_name.setText(proj.name)
        self.ed_path.setText(str(proj.path))
        self.ed_path.setCursorPosition(0)
        self.ed_path.setToolTip(str(proj.path))
        self.ed_data.setText(str(proj.data_dir))
        self.ed_data.setCursorPosition(0)
        self.ed_data.setToolTip(str(proj.data_dir))

        # 服务表格
        statuses = cm.get_status(proj)
        self.tbl_svc.setRowCount(0)

        # 内存用量：一次算好整张表要用的数据，避免每行都去查
        mem = {}
        try:
            from core import podman_config as pcfg

            for c in pcfg.compute_usage(self.client).get("containers", []):
                mem[c["name"]] = c
        except Exception:
            pass

        for svc in proj.services:
            row = self.tbl_svc.rowCount()
            self.tbl_svc.insertRow(row)

            state = statuses.get(svc.name, "missing")

            # 当前占用 / 已配置上限
            info = mem.get(svc.container_name or "") or {}
            actual = info.get("actual_gb", 0.0)
            limit = info.get("limit_gb", 0.0)
            if state == "running":
                mem_text = (
                    f"{actual:.2f} / {limit:.2f} GB" if limit > 0
                    else f"{actual:.2f} GB"
                )
            else:
                mem_text = "—"

            cells = [
                svc.name,
                STATE_TEXT.get(state, state),
                svc.image,
                svc.port_summary,
                mem_text,
            ]
            for col, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setToolTip(text)
                if col == 1:
                    it.setForeground(QColor(STATE_COLORS.get(state, "#95a5a6")))
                    it.setIcon(state_icon(
                        {"running": "running", "exited": "exited",
                         "created": "created", "paused": "paused",
                         "missing": "exited"}.get(state, "exited"),
                        14,
                    ))
                self.tbl_svc.setItem(row, col, it)

    def _update_actions(self) -> None:
        has = self._current is not None
        for act in (
            self.act_up, self.act_down, self.act_restart,
            self.act_pull, self.act_logs, self.act_delete,
        ):
            act.setEnabled(has)

    # ─────────────────────────────────────────
    # YAML
    # ─────────────────────────────────────────
    def _load_yaml(self) -> None:
        self._dirty = False
        if self._current is None:
            self.ed_yaml.setPlainText(
                "# 还没有选择 Compose 项目\n"
                "#\n"
                "# 请从左侧选择项目，或点工具栏「新建」创建。\n"
                "#\n"
                "# 新建的项目会生成这样的目录结构：\n"
                "#   <根目录>/<项目名>/compose/docker-compose.yml\n"
                "#   <根目录>/<项目名>/data/\n"
                "#   <根目录>/<项目名>/config/"
            )
            self.lbl_yaml_hint.setText("尚未选择项目")
            return
        try:
            text = self._current.path.read_text(encoding="utf-8")
        except OSError as e:
            self.ed_yaml.setPlainText(f"# 读取失败: {e}")
            return
        self.ed_yaml.setPlainText(text)
        self._dirty = False
        self.lbl_yaml_hint.setText(str(self._current.path))

    def _yaml_focus_out(self, editor):
        """给 QPlainTextEdit 挂「失焦自动保存」"""
        original = editor.focusOutEvent

        def handler(event):
            original(event)
            self._autosave_yaml()

        return handler

    def _autosave_yaml(self) -> None:
        """有未保存修改就静默落盘（不弹窗、不打断操作）"""
        if not self._dirty or self._current is None:
            return
        try:
            self._current.path.write_text(
                self.ed_yaml.toPlainText(), encoding="utf-8"
            )
            self._dirty = False
            self.lbl_yaml_hint.setText(str(self._current.path))
            self.status_message.emit("已自动保存 YAML")
        except OSError as e:
            self.status_message.emit(f"自动保存失败: {e}")

    def _on_yaml_changed(self) -> None:
        if not self._dirty:
            self._dirty = True
            self.lbl_yaml_hint.setText(
                f"{self._current.path if self._current else ''}  ● 已修改"
            )

    def _validate_yaml(self) -> bool:
        text = self.ed_yaml.toPlainText()
        ok, err, _data = cm.validate_yaml(text)
        if ok:
            QMessageBox.information(self, "校验通过", "YAML 格式正确。")
            return True
        QMessageBox.warning(self, "校验失败", err)
        return False

    def _save_yaml(self) -> None:
        if self._current is None:
            return
        text = self.ed_yaml.toPlainText()

        ok, err, _ = cm.validate_yaml(text)
        if not ok:
            r = QMessageBox.question(
                self, "格式有问题",
                f"{err}\n\n仍要保存吗？（可能导致无法启动）",
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        try:
            # 备份
            bak = self._current.path.with_suffix(".yml.bak")
            if self._current.path.exists():
                bak.write_text(
                    self._current.path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            self._current.path.write_text(text, encoding="utf-8")
        except OSError as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return

        self._dirty = False
        self.lbl_yaml_hint.setText(str(self._current.path))
        self.status_message.emit(f"已保存 {self._current.path.name}")
        notifier().info("Compose 已保存", str(self._current.path))

        # 重新解析并刷新
        cm._parse(self._current)
        self.refresh()

    # ─────────────────────────────────────────
    # 执行操作
    # ─────────────────────────────────────────
    def _run(self, action: str) -> None:
        if self._current is None:
            return
        proj = self._current

        if action == "down":
            r = QMessageBox.question(
                self, "停止项目",
                f"停止并移除「{proj.name}」的所有容器？\n\n"
                f"数据卷不会删除（位于 {proj.data_dir}）",
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        names = {
            "up": "启动", "down": "停止", "restart": "重启",
            "pull": "拉取", "ps": "查询",
        }
        label = names.get(action, action)
        self.status_message.emit(f"{label} {proj.name}…")

        def work():
            return cm.run_compose(proj, action, timeout=600)

        def done(result):
            ok, output = result
            if ok:
                self.status_message.emit(f"{proj.name} {label}成功")
                if action == "up":
                    notifier().send(
                        "Compose 已启动", proj.name, icon_name="play"
                    )
                elif action == "down":
                    notifier().send(
                        "Compose 已停止", proj.name, icon_name="stop"
                    )
                elif action == "restart":
                    notifier().send(
                        "Compose 已重启", proj.name, icon_name="restart"
                    )
                elif action == "pull":
                    notifier().image_pulled(proj.name)
            else:
                self.status_message.emit(f"{proj.name} {label}失败")
                notifier().op_failed(label, proj.name, output[:200])
                QMessageBox.warning(
                    self, f"{label}失败",
                    f"{proj.name}\n\n{output[:1500]}",
                )
            self.refresh()

        def error(msg):
            self.status_message.emit(f"{label}失败: {msg}")
            QMessageBox.critical(self, f"{label}失败", msg)

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    def on_disconnected(self) -> None:
        """Podman 断开：所有服务状态标记为未知"""
        # 项目列表保留（来自文件系统），但状态清空
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setText(1, "—")
            item.setForeground(1, QColor("#95a5a6"))
            for j in range(item.childCount()):
                child = item.child(j)
                child.setText(1, "未知")
                child.setForeground(1, QColor("#95a5a6"))
                child.setIcon(1, state_icon("exited", 14))

        self.lbl_stats.setText(
            f"{len(self.projects)} 个项目 · Podman 服务已断开"
        )
        self._update_actions()

    def on_reconnected(self) -> None:
        """重连后重新扫描状态"""
        self.refresh()

    def _tree_context_menu(self, pos) -> None:
        """项目树右键菜单"""
        item = self.tree.itemAt(pos)
        if item is None:
            return

        self.tree.setCurrentItem(item)
        proj = self._current
        if proj is None:
            return

        from PyQt6.QtWidgets import QMenu

        menu = QMenu(self)

        act_up = menu.addAction(icon("play"), "启动")
        act_up.triggered.connect(lambda: self._run("up"))

        act_down = menu.addAction(icon("stop"), "停止")
        act_down.triggered.connect(lambda: self._run("down"))

        act_restart = menu.addAction(icon("restart"), "重启")
        act_restart.triggered.connect(lambda: self._run("restart"))

        menu.addSeparator()

        act_logs = menu.addAction(icon("log"), "查看日志")
        act_logs.triggered.connect(self._show_logs)

        act_yaml = menu.addAction(icon("edit"), "编辑 YAML")
        act_yaml.triggered.connect(lambda: self.tabs.setCurrentIndex(1))

        menu.addSeparator()

        act_delete = menu.addAction(icon("delete"), "删除项目…")
        act_delete.triggered.connect(self._delete_project)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _delete_project(self) -> None:
        """删除 Compose 项目（先停止容器，再删目录）"""
        proj = self._current
        if proj is None:
            self.status_message.emit("请先选择一个项目")
            return

        # 检查运行中的服务
        statuses = cm.get_status(proj)
        running = [svc for svc, st in statuses.items() if st == "running"]

        detail = f"<b>项目：</b>{proj.name}<br>"
        detail += f"<b>目录：</b>{proj.root}<br>"
        detail += f"<b>服务：</b>{proj.service_count} 个<br>"

        if running:
            detail += (
                f"<br><b style='color:#e67e22'>⚠ {len(running)} 个服务正在运行：</b><br>"
                f"&nbsp;&nbsp;&nbsp;&nbsp;{'、'.join(running)}<br>"
                f"<br>删除前会自动停止这些容器。"
            )

        detail += "<br><br><b>将删除以下内容（不可恢复）：</b><br>"
        for sub in ("compose", "data", "config", "logs", "init"):
            d = proj.root / sub
            if d.exists():
                detail += f"&nbsp;&nbsp;• {d}<br>"

        r = QMessageBox.warning(
            self, f"删除项目「{proj.name}」",
            detail + "<br>确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        # 二次确认
        r2 = QMessageBox.critical(
            self, "再次确认",
            f"即将永久删除：<br><br><b>{proj.root}</b><br><br>"
            f"包括所有数据卷、配置和日志。<br>"
            f"<b style='color:#e74c3c'>此操作不可撤销。</b><br><br>确定删除？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r2 != QMessageBox.StandardButton.Yes:
            return

        self._do_delete(proj, running)

    def _do_delete(self, proj: cm.ComposeProject, running: list[str]) -> None:
        """后台执行删除：先 down 停止，再删目录"""
        import shutil

        self.status_message.emit(f"正在删除 {proj.name}…")
        if hasattr(self, "act_delete"):
            self.act_delete.setEnabled(False)

        target = proj.root
        had_running = bool(running)

        def work():
            # 1. 先停止（总是执行，确保容器被清理）
            ok_down, out_down = cm.run_compose(proj, "down", timeout=180)

            # 2. 删除项目目录
            if not target.exists():
                return True, "目录已不存在", out_down

            try:
                shutil.rmtree(target)
            except OSError as e:
                return False, f"删除目录失败: {e}", out_down

            return True, f"已删除 {target}", out_down

        def done(result):
            ok, msg, down_out = result
            if hasattr(self, "act_delete"):
                self.act_delete.setEnabled(True)

            if ok:
                self.status_message.emit(f"{proj.name} 删除成功")
                notifier().send(
                    "Compose 项目已删除",
                    f"{proj.name}\n{target}",
                    icon_name="delete",
                )
                self._current = None
                self.refresh()
            else:
                self.status_message.emit(f"{proj.name} 删除失败: {msg}")
                notifier().op_failed("删除", proj.name, msg)
                QMessageBox.critical(
                    self, "删除失败",
                    f"{msg}<br><br><b>停止输出：</b><br>"
                    f"<pre>{down_out[:800]}</pre>",
                )

        def error(msg):
            if hasattr(self, "act_delete"):
                self.act_delete.setEnabled(True)
            self.status_message.emit(f"删除失败: {msg}")
            QMessageBox.critical(self, "删除失败", msg)

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    def _show_logs(self) -> None:
        """显示项目日志（自动隐藏滚动条 + 过滤 + 复制）"""
        if self._current is None:
            return
        proj = self._current

        self.status_message.emit(f"正在读取 {proj.name} 日志…")

        def work():
            return cm.run_compose(proj, "logs", timeout=60)

        def done(result):
            ok, output = result
            self._open_log_dialog(proj, output or "(无输出)")

        def error(msg):
            QMessageBox.warning(self, "读取日志失败", msg)

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    def _open_log_dialog(self, proj, text: str) -> None:
        """日志窗口：工具栏 + 过滤 + 自动隐藏滚动条"""
        # 保持引用，防止被 Python GC 回收导致窗口闪退
        if not hasattr(self, "_log_dialogs"):
            self._log_dialogs: list = []

        dlg = QDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.setWindowTitle(f"日志 · {proj.name}")
        dlg.resize(920, 620)
        dlg.setMinimumSize(560, 360)

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 日志区（先创建，供工具栏 lambda 引用）──
        view = AutoHidePlainTextEdit()
        view.setObjectName("logView")
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        view.setFrameShape(QFrame.Shape.NoFrame)

        from PyQt6.QtGui import QFont as _QFont
        _font = _QFont("Maple Mono NF CN")
        _font.setStyleHint(_QFont.StyleHint.Monospace)
        _font.setPointSize(QApplication.font().pointSize() or 10)
        view.setFont(_font)

        all_lines = text.splitlines()
        view.setPlainText(text)

        # ── 工具栏 ──
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        act_refresh = QAction(icon("refresh"), "刷新", dlg)
        act_refresh.triggered.connect(
            lambda: self._reload_log(dlg, proj)
        )
        tb.addAction(act_refresh)

        act_copy = QAction(icon("save"), "复制", dlg)
        act_copy.triggered.connect(
            lambda: QApplication.clipboard().setText(view.toPlainText())
        )
        tb.addAction(act_copy)

        act_clear = QAction(icon("clean"), "清屏", dlg)
        act_clear.triggered.connect(view.clear)
        tb.addAction(act_clear)

        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().Policy.Expanding,
            spacer.sizePolicy().Policy.Preferred,
        )
        tb.addWidget(spacer)

        layout.addWidget(tb)

        # ── 过滤栏 ──
        filter_bar = QFrame()
        filter_bar.setObjectName("filterBar")
        fb = QHBoxLayout(filter_bar)
        fb.setContentsMargins(10, 6, 10, 6)
        fb.setSpacing(8)

        search = QLineEdit()
        search.setObjectName("searchBox")
        search.setPlaceholderText("过滤关键词（留空显示全部）")
        search.setClearButtonEnabled(True)
        search.setFixedHeight(26)
        search.addAction(
            icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        fb.addWidget(search, 1)

        lbl_match = QLabel("")
        lbl_match.setObjectName("hintLabel")
        lbl_match.setMinimumWidth(70)
        lbl_match.setAlignment(Qt.AlignmentFlag.AlignRight)
        fb.addWidget(lbl_match)

        layout.addWidget(filter_bar)

        # ── 日志区加入布局 ──
        layout.addWidget(view, 1)

        # ── 状态栏 ──
        status = QFrame()
        status.setObjectName("logStatusBar")
        sb = QHBoxLayout(status)
        sb.setContentsMargins(10, 4, 10, 4)

        lbl_status = QLabel(f"{len(all_lines)} 行")
        lbl_status.setObjectName("hintLabel")
        sb.addWidget(lbl_status, 1)

        from datetime import datetime
        lbl_time = QLabel(datetime.now().strftime("%H:%M:%S"))
        lbl_time.setObjectName("hintLabel")
        sb.addWidget(lbl_time)

        layout.addWidget(status)

        # ── 过滤逻辑 ──
        def apply_filter(kw: str) -> None:
            kw = kw.strip()
            if not kw:
                view.setPlainText(text)
                lbl_match.setText("")
                return
            low = kw.lower()
            hits = [ln for ln in all_lines if low in ln.lower()]
            view.setPlainText(
                "\n".join(hits) if hits else f"（无匹配「{kw}」的行）"
            )
            lbl_match.setText(f"{len(hits)}/{len(all_lines)}")

        search.textChanged.connect(apply_filter)

        self._log_dialogs.append(dlg)
        dlg.destroyed.connect(
            lambda _=None, d=dlg: (
                self._log_dialogs.remove(d)
                if d in self._log_dialogs else None
            )
        )

        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _reload_log(self, dlg, proj) -> None:
        """重新拉取日志（关掉旧窗口开新的）"""
        dlg.close()
        self._show_logs()

    # ─────────────────────────────────────────
    # 新建项目
    # ─────────────────────────────────────────
    def _new_project(self) -> None:
        name, ok = ask_text(
            self, "新建 Compose 项目",
            f"项目名称（将在 {self.root} 下创建）：",
            hint=f"将创建目录结构：\n"
                 f"  {self.root}/<名称>/compose/docker-compose.yml\n"
                 f"  {self.root}/<名称>/data/\n"
                 f"  {self.root}/<名称>/config/",
            width=680, height=250,
        )
        if not ok or not name:
            return
        if not name.replace("-", "").replace("_", "").isalnum():
            QMessageBox.warning(
                self, "名称无效",
                "项目名只能包含字母、数字、连字符和下划线。",
            )
            return

        ok, msg, path = cm.create_project(self.root, name)
        if not ok:
            QMessageBox.critical(self, "创建失败", msg)
            return

        self.status_message.emit(msg)
        notifier().info("Compose 项目已创建", f"{name}\n{path}")
        self.refresh()

        QTimer.singleShot(600, lambda: self._select_project(name))

    def _select_project(self, name: str) -> None:
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.text(0) == name:
                self.tree.setCurrentItem(it)
                self.tabs.setCurrentIndex(1)  # 直接看 YAML
                return
