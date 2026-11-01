"""容器内文件浏览对话框（只读）

只做一件事：看看容器里有什么。
刻意不提供上传 / 下载 / 新建 / 重命名 / 删除 ——
容器是临时环境，改动应该走镜像或挂载，而不是手改运行中的容器。

挂载目录会标出来（宿主机路径 / 命名卷），
因为用户最关心的往往就是「这个目录到底存在哪」。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import container_files as cf
from core.async_task import run_async
from resources.icons import ICON_SIZE_BUTTON, icon
from ui.style import semantic_colors
from ui.utils import fmt_size
from ui.widgets import (
    apply_aligned_delegate,
    apply_dialog_geometry,
    make_list_card,
)

COLUMNS = ["名称", "大小", "挂载", "权限", "修改时间"]


class ContainerFilesDialog(QDialog):
    """容器文件浏览（只读）"""

    def __init__(self, client, cid: str, name: str,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self.cid = cid
        self.container_name = name
        self.cwd = "/"
        self._tasks: list = []
        self._mounts: dict[str, str] = {}
        self._seq = 0

        self.setWindowTitle(f"浏览文件 — {name}")
        self._build_ui()
        apply_dialog_geometry(self, width_ratio=0.42, height_ratio=0.48)

        # 挂载信息先拿（用于标记），再列目录
        QTimer.singleShot(0, self._load_mounts)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        # ── 路径栏 ──
        nav = QHBoxLayout()
        nav.setSpacing(4)

        self.btn_up = QPushButton(icon("up"), "")
        self.btn_up.setFixedSize(30, 28)
        self.btn_up.setToolTip("上一级")
        self.btn_up.clicked.connect(self._go_up)
        nav.addWidget(self.btn_up)

        self.btn_home = QPushButton(icon("home"), "")
        self.btn_home.setFixedSize(30, 28)
        self.btn_home.setToolTip("根目录")
        self.btn_home.clicked.connect(lambda: self.navigate("/"))
        nav.addWidget(self.btn_home)

        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("容器内路径，回车跳转")
        self.ed_path.returnPressed.connect(self._go_typed)
        nav.addWidget(self.ed_path, 1)

        self.btn_refresh = QPushButton(icon("refresh"), "")
        self.btn_refresh.setFixedSize(30, 28)
        self.btn_refresh.setToolTip("刷新")
        self.btn_refresh.clicked.connect(lambda: self.navigate(self.cwd))
        nav.addWidget(self.btn_refresh)

        v.addLayout(nav)

        # ── 挂载提示 ──
        self.lbl_mount = QLabel("")
        self.lbl_mount.setObjectName("hintLabel")
        self.lbl_mount.setWordWrap(True)
        v.addWidget(self.lbl_mount)

        # ── 表格 ──
        self.table = QTableWidget(0, len(COLUMNS))
        apply_aligned_delegate(self.table)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setCornerButtonEnabled(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.doubleClicked.connect(self._on_double_click)

        h = self.table.horizontalHeader()
        h.setFixedHeight(26)
        h.setHighlightSections(False)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in (1, 2, 3, 4):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 88)
        self.table.setColumnWidth(2, 150)   # 挂载：显示宿主机来源
        self.table.setColumnWidth(3, 104)   # 权限
        self.table.setColumnWidth(4, 140)

        v.addWidget(make_list_card(self.table), 1)

        # ── 底部 ──
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("hintLabel")
        bar.addWidget(self.lbl_status, 1)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_close)

        v.addLayout(bar)

    # ─────────────────────────────────────────
    # 数据
    # ─────────────────────────────────────────
    def _load_mounts(self) -> None:
        def work():
            return cf.mounts(self.cid)

        def done(res):
            self._mounts = res or {}
            self.navigate(self.cwd)

        self._spawn(work, done, lambda _m: self.navigate(self.cwd))

    def navigate(self, path: str) -> None:
        self._seq += 1
        seq = self._seq
        p = (path or "/").strip() or "/"

        self.lbl_status.setText("加载中…")
        self.btn_refresh.setEnabled(False)

        def work():
            return cf.list_dir(self.cid, p, self._mounts)

        def done(res):
            if seq != self._seq:
                return
            self.btn_refresh.setEnabled(True)
            ok, data = res
            if not ok:
                self.lbl_status.setText(f"读取失败：{data}")
                return
            self.cwd = p
            self.ed_path.setText(p)
            self._fill(data)
            self.lbl_status.setText(f"{len(data)} 项")

        def error(msg):
            if seq != self._seq:
                return
            self.btn_refresh.setEnabled(True)
            self.lbl_status.setText(f"读取失败：{msg}")

        self._spawn(work, done, error)

    def _fill(self, entries: list[cf.Entry]) -> None:
        self.table.setRowCount(0)

        accent = QColor(semantic_colors()["success"])

        for e in entries:
            row = self.table.rowCount()
            self.table.insertRow(row)

            # 名称：目录/文件图标；挂载目录额外标注来源
            label = e.name
            if e.is_link and e.target:
                label = f"{e.name} → {e.target}"
            item = QTableWidgetItem(label)
            item.setIcon(icon(
                "folder" if e.is_dir else "file", 16
            ))
            item.setData(Qt.ItemDataRole.UserRole, e)
            if e.is_mount:
                item.setForeground(accent)
                item.setToolTip(f"挂载自：{self._mount_source(e.name)}")
            else:
                item.setToolTip(e.name)
            self.table.setItem(row, 0, item)

            size_text = "—" if e.is_dir else fmt_size(e.size)
            self.table.setItem(row, 1, QTableWidgetItem(size_text))

            # 挂载列：直接写出宿主机上的来源，一眼看清数据落在哪
            mount_item = QTableWidgetItem("—")
            if e.is_mount:
                mount_item.setText(self._mount_source(e.name))
                mount_item.setForeground(accent)
                mount_item.setToolTip(
                    f"挂载自：{self._mount_source(e.name)}"
                )
            else:
                mount_item.setForeground(
                    QColor(self.palette().color(
                        self.palette().ColorRole.WindowText
                    ))
                )
            self.table.setItem(row, 2, mount_item)

            self.table.setItem(row, 3, QTableWidgetItem(e.perms))
            self.table.setItem(row, 4, QTableWidgetItem(e.mtime))

        # 挂载提示
        if self._mounts:
            here = self._mounts.get(self.cwd.rstrip("/") or "/")
            if here:
                self.lbl_mount.setText(
                    f"当前目录挂载自宿主机：{here}（改动会直接写宿主机）"
                )
            else:
                self.lbl_mount.setText(
                    "本容器有 "
                    f"{len(self._mounts)} 个挂载目录，已用绿色标出。"
                )
        else:
            self.lbl_mount.setText("本容器没有挂载目录。")

    # ─────────────────────────────────────────
    # 导航
    # ─────────────────────────────────────────
    def _mount_source(self, name: str) -> str:
        """某个条目对应的宿主机来源（非挂载项返回空串）"""
        base = self.cwd.rstrip("/")
        full = f"{base}/{name}" if base else f"/{name}"
        return self._mounts.get(full, "") or "挂载目录"

    def _go_up(self) -> None:
        if self.cwd in ("/", ""):
            return
        parent = self.cwd.rstrip("/").rsplit("/", 1)[0]
        self.navigate(parent or "/")

    def _go_typed(self) -> None:
        text = self.ed_path.text().strip()
        if text:
            self.navigate(text)

    def _on_double_click(self, index) -> None:
        item = self.table.item(index.row(), 0)
        if item is None:
            return
        e = item.data(Qt.ItemDataRole.UserRole)
        if e is None or not e.is_dir:
            return
        base = self.cwd.rstrip("/")
        self.navigate(f"{base}/{e.name}" if base else f"/{e.name}")

    # ─────────────────────────────────────────
    # 任务
    # ─────────────────────────────────────────
    def _spawn(self, work, done, error) -> None:
        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)
