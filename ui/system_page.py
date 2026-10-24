"""系统信息页

显示 Podman 版本、存储配置、资源占用、运行环境。

性能注意：version() 和 system_info() 各约 900ms，
必须异步加载，否则切换页面会卡 1.8 秒。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.podman_client import PodmanClient, PodmanError
from resources.icons import ICON_SIZE_TOOLBAR, icon
from ui.utils import fmt_size


class InfoRow(QWidget):
    """一行「标签 + 值」，值可选等宽字体"""

    def __init__(self, label: str, mono: bool = False, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.lbl_key = QLabel(label)
        self.lbl_key.setObjectName("formLabel")
        self.lbl_key.setFixedWidth(90)
        layout.addWidget(self.lbl_key)

        self.lbl_val = QLabel("—")
        self.lbl_val.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lbl_val.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.lbl_val.setWordWrap(False)
        if mono:
            self.lbl_val.setStyleSheet("font-family: 'JetBrains Mono', 'Maple Mono NF CN', monospace;")
        layout.addWidget(self.lbl_val, 1)

    def set(self, text: str) -> None:
        self.lbl_val.setText(text or "—")
        self.lbl_val.setToolTip(text or "")


class SystemPage(QWidget):
    status_message = pyqtSignal(str)

    def __init__(self, client: PodmanClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self._loading = False
        self._tasks: list = []
        self._build_ui()

        # 首次加载延后到事件循环启动后，避免阻塞窗口显示
        QTimer.singleShot(50, self.refresh_silent)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def refresh_icons(self) -> None:
        """主题切换后重建图标（QIconEngine 需重新绑定）"""
        if hasattr(self, 'act_refresh'):
            self.act_refresh.setIcon(icon("refresh"))

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 0, 12, 12)
        outer.setSpacing(8)

        # 工具栏
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)
        self.act_refresh = QAction(icon("refresh"), "刷新", self)

        self.act_refresh.setProperty("iconName", "refresh")
        self.act_refresh.triggered.connect(self.refresh)
        tb.addAction(self.act_refresh)
        outer.addWidget(tb)

        # 滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)

        # ── 版本信息 ──
        grp1 = QGroupBox("版本信息")
        f1 = QFormLayout(grp1)
        f1.setContentsMargins(12, 8, 12, 8)
        f1.setSpacing(6)
        self.row_version = InfoRow("Podman")
        self.row_api = InfoRow("API 版本")
        self.row_platform = InfoRow("平台")
        for r in (self.row_version, self.row_api, self.row_platform):
            f1.addRow(r)
        grid.addWidget(grp1, 0, 0)

        # ── 运行环境 ──
        grp2 = QGroupBox("运行环境")
        f2 = QFormLayout(grp2)
        f2.setContentsMargins(12, 8, 12, 8)
        f2.setSpacing(6)
        self.row_rootless = InfoRow("Rootless")
        self.row_cgroup = InfoRow("Cgroup")
        self.row_os = InfoRow("操作系统")
        self.row_kernel = InfoRow("内核")
        for r in (self.row_rootless, self.row_cgroup, self.row_os, self.row_kernel):
            f2.addRow(r)
        grid.addWidget(grp2, 0, 1)

        # ── 存储 ──
        grp3 = QGroupBox("存储")
        f3 = QFormLayout(grp3)
        f3.setContentsMargins(12, 8, 12, 8)
        f3.setSpacing(6)
        self.row_driver = InfoRow("驱动")
        self.row_root = InfoRow("数据目录", mono=True)
        self.row_runroot = InfoRow("运行时", mono=True)
        self.row_socket = InfoRow("Socket", mono=True)
        for r in (self.row_driver, self.row_root, self.row_runroot, self.row_socket):
            f3.addRow(r)
        grid.addWidget(grp3, 1, 0, 1, 2)

        # ── 资源占用 ──
        grp4 = QGroupBox("资源占用")
        g4 = QGridLayout(grp4)
        g4.setContentsMargins(12, 8, 12, 8)
        g4.setSpacing(6)
        self.row_images = InfoRow("镜像")
        self.row_containers = InfoRow("容器")
        self.row_volumes = InfoRow("卷")
        g4.addWidget(self.row_images, 0, 0)
        g4.addWidget(self.row_containers, 1, 0)
        g4.addWidget(self.row_volumes, 2, 0)
        grid.addWidget(grp4, 2, 0, 1, 2)

        grid.setRowStretch(3, 1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

    # ─────────────────────────────────────────
    # 刷新（异步）
    # ─────────────────────────────────────────
    def refresh(self) -> None:
        self.status_message.emit("正在刷新系统信息…")
        self.refresh_silent(notify=True)

    def refresh_silent(self, notify: bool = False) -> None:
        """后台加载，不阻塞 UI"""
        if self._loading:
            return
        self._loading = True
        self.act_refresh.setEnabled(False)

        def load():
            """在后台线程执行全部请求"""
            out: dict = {}
            try:
                out["version"] = self.client.version()
            except PodmanError as e:
                out["version_error"] = str(e)
            try:
                out["info"] = self.client.system_info()
            except PodmanError as e:
                out["info_error"] = str(e)
            try:
                out["df"] = self.client.system_df()
            except PodmanError as e:
                out["df_error"] = str(e)
            return out

        def done(data: dict) -> None:
            self._apply(data)
            self._loading = False
            self.act_refresh.setEnabled(True)
            if notify:
                self.status_message.emit("系统信息已更新")

        def error(msg: str) -> None:
            self._loading = False
            self.act_refresh.setEnabled(True)
            self.status_message.emit(f"加载失败: {msg}")

        task = run_async(load, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t._thread is not None]
        self._tasks.append(task)

    def _apply(self, d: dict) -> None:
        # 版本
        v = d.get("version")
        if v:
            comps = v.get("Components", [])
            engine = next((x for x in comps if x.get("Name") == "Podman Engine"), {})
            self.row_version.set(engine.get("Version", "—"))
            self.row_api.set(str(engine.get("Details", {}).get("APIVersion", "—")))
            self.row_platform.set(v.get("Platform", {}).get("Name", "—"))
        elif "version_error" in d:
            self.row_version.set(f"获取失败: {d['version_error']}")

        # 系统信息
        info = d.get("info")
        if info:
            store = info.get("store") or {}
            host = info.get("host") or {}
            sec = host.get("security") or {}

            self.row_driver.set(store.get("graphDriverName", "—"))
            self.row_root.set(store.get("graphRoot", "—"))
            self.row_runroot.set(store.get("runRoot", "—"))
            self.row_socket.set(self.client.socket_path or "—")

            self.row_rootless.set("是" if sec.get("rootless") else "否")
            self.row_cgroup.set(host.get("cgroupVersion", "—"))
            os_name = host.get("os", "—")
            self.row_os.set(os_name[:48] if isinstance(os_name, str) else str(os_name))
            self.row_kernel.set(host.get("kernel", "—"))
        elif "info_error" in d:
            self.row_driver.set(f"获取失败: {d['info_error']}")

        # 资源占用
        df = d.get("df")
        if df:
            imgs = df.get("Images") or []
            ctrs = df.get("Containers") or []
            vols = df.get("Volumes") or []

            img_size = sum(x.get("Size", 0) or 0 for x in imgs)
            ctr_size = sum(x.get("SizeRw", 0) or 0 for x in ctrs)
            vol_size = sum(
                (x.get("UsageData") or {}).get("Size", 0) or 0 for x in vols
            )

            self.row_images.set(f"{len(imgs)} 个 · {fmt_size(img_size)}")
            self.row_containers.set(f"{len(ctrs)} 个 · {fmt_size(ctr_size)}")
            self.row_volumes.set(f"{len(vols)} 个 · {fmt_size(vol_size)}")
