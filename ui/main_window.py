"""主窗口

布局：
┌──────────────────────────────────────┐
│ 侧边栏 │        页面内容            │
│ 容器   │  ┌────────────────────┐    │
│ 镜像   │  │    工具栏          │    │
│ 卷     │  ├────────────────────┤    │
│ 网络   │  │                    │    │
│ Pod    │  │    表格/内容        │    │
│ 系统   │  │                    │    │
│        │  └────────────────────┘    │
├──────────────────────────────────────┤
│ 状态栏                                │
└──────────────────────────────────────┘
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from core.config import APP_NAME, APP_VERSION
from core.kde_theme import ThemeWatcher
from core.podman_client import PodmanClient, PodmanError, find_socket
from resources.icons import ICON_SIZE_TOOLBAR, icon
from ui.compose_page import ComposePage
from ui.container_page import ContainerPage
from ui.image_page import ImagePage
from ui.log_viewer import open_log_viewer
from ui.terminal import TerminalDialog
from ui.network_page import NetworkPage
from ui.pod_page import PodPage
from ui.settings_page import SettingsPage
from ui.tray import TrayIcon
from ui.sidebar import NAV_LABELS, Sidebar
from ui.style import build_stylesheet
from ui.volume_page import VolumePage


def _window_icon() -> QIcon:
    """窗口图标：优先用 PNG（应用图标），回退到内置 SVG"""
    from pathlib import Path as _P

    from core.config import RESOURCES

    png = RESOURCES / "appicon" / "podman-gui-256.png"
    if png.exists():
        return QIcon(str(png))
    return icon("seal", 256)


class MainWindow(QMainWindow):
    def __init__(self, client: PodmanClient):
        super().__init__()
        self.client = client
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 760)
        self.setWindowIcon(_window_icon())

        self._build_ui()
        self._setup_theme_watcher()
        self._check_connection()
        self._setup_tray()

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        # 不显示菜单栏（本应用所有操作都在侧边栏/工具栏）
        self.menuBar().setVisible(False)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 侧边栏
        self.sidebar = Sidebar()
        self.sidebar.item_selected.connect(self._on_nav)
        layout.addWidget(self.sidebar)

        # 页面栈
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        # 各页面
        self.page_containers = ContainerPage(self.client)
        self.page_images = ImagePage(self.client)
        self.page_volumes = VolumePage(self.client)
        self.page_networks = NetworkPage(self.client)
        self.page_pods = PodPage(self.client)
        self.page_compose = ComposePage(self.client)
        self.page_settings = SettingsPage(self.client)

        self._pages = {
            "containers": self.page_containers,
            "images": self.page_images,
            "volumes": self.page_volumes,
            "networks": self.page_networks,
            "pods": self.page_pods,
            "compose": self.page_compose,
            "settings": self.page_settings,
        }
        for p in self._pages.values():
            self.stack.addWidget(p)
            if hasattr(p, "status_message"):
                p.status_message.connect(self._set_status)

        # 容器页的信号
        self.page_containers.request_logs.connect(self._open_logs)
        self.page_containers.request_terminal.connect(self._open_terminal)
        self.page_compose.request_logs.connect(self._open_compose_logs)

        # 状态栏
        sb = QStatusBar()
        self.setStatusBar(sb)

        self.lbl_conn = QLabel("")
        self.lbl_conn.mousePressEvent = self._on_conn_clicked
        sb.addPermanentWidget(self.lbl_conn)

        self.lbl_status = QLabel("就绪")
        sb.addWidget(self.lbl_status, 1)

        # 默认选中容器页
        self.sidebar.select("containers")

        # 每 10 秒检测一次连接状态
        self._conn_timer = QTimer(self)
        self._conn_timer.timeout.connect(lambda: self._check_connection(False))
        self._conn_timer.start(10000)

        # 不建菜单栏：所有功能都在侧边栏和工具栏中

    # ─────────────────────────────────────────
    # 系统托盘
    # ─────────────────────────────────────────
    def _setup_tray(self) -> None:
        """创建系统托盘（不可用时静默跳过）"""
        self.tray = None
        try:
            self.tray = TrayIcon(self.client, self)
            self.tray.show_window.connect(self._restore_from_tray)
            self.tray.quit_app.connect(self._quit_from_tray)
            self.tray.open_logs.connect(self._open_logs)
        except RuntimeError:
            self.tray = None

    def _restore_from_tray(self) -> None:
        """从托盘恢复主窗口"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        """托盘菜单退出"""
        if self.tray is not None:
            self.tray.hide()
        QApplication.instance().quit()

    def _setup_theme_watcher(self) -> None:
        self.theme_watcher = ThemeWatcher(2000, self)
        self.theme_watcher.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, is_dark: bool) -> None:
        """KDE 主题切换时重新应用样式和图标"""
        QApplication.instance().setStyleSheet(build_stylesheet())

        # 侧边栏图标
        self.sidebar.refresh_icons()

        # 各页面图标 + 数据
        for page in self._pages.values():
            if hasattr(page, "refresh_icons"):
                page.refresh_icons()
            if hasattr(page, "refresh_silent"):
                page.refresh_silent()

        # 窗口图标
        self.setWindowIcon(_window_icon())

        # 强制全窗口重绘（QIconEngine 会在绘制时用新颜色）
        self.update()
        for w in self.findChildren(QWidget):
            w.update()

        self._set_status(f"已切换到{'暗色' if is_dark else '亮色'}主题")

    # ─────────────────────────────────────────
    # 导航
    # ─────────────────────────────────────────
    def _on_nav(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        # 同步侧边栏高亮（防止程序化切换时不同步）
        self.sidebar.select(key)
        # 窗口标题跟随页面
        label = NAV_LABELS.get(key)
        self.setWindowTitle(f"{label} — {APP_NAME}" if label else APP_NAME)
        # 切页时刷新一次
        if hasattr(page, "refresh_silent"):
            page.refresh_silent()

    def _refresh_current(self) -> None:
        page = self.stack.currentWidget()
        if hasattr(page, "refresh"):
            page.refresh()

    # ─────────────────────────────────────────
    # 状态
    # ─────────────────────────────────────────
    def _set_status(self, msg: str) -> None:
        self.lbl_status.setText(msg)

    def _on_conn_clicked(self, event) -> None:
        """点击状态栏连接指示器 → 打开设置的服务页"""
        self._on_nav("settings")
        self.sidebar.select("settings")
        if hasattr(self.page_settings, "tabs"):
            # 服务标签是第 1 个（概览=0, 服务=1）
            self.page_settings.tabs.setCurrentIndex(1)

    def _check_connection(self, show_error: bool = True) -> None:
        """检测连接状态并更新状态栏指示器

        show_error=False 用于定时轮询，避免重复弹窗。
        """
        sock = find_socket()

        if not sock:
            self._set_conn_state(
                "未连接", "#e74c3c",
                "未找到 Podman socket\n\n"
                "运行以下命令启动：\n"
                "  systemctl --user start podman.socket",
            )
            if show_error:
                # 非阻塞提示：用户可能在无 Podman 的环境（如 CI）启动，
                # 阻塞式弹窗会导致程序挂死
                QTimer.singleShot(200, lambda: self._warn_no_socket())
            return

        if self.client.ping():
            self._set_conn_state(
                "已连接", "#27ae60", f"Socket: {sock}"
            )
        else:
            self._set_conn_state(
                "连接异常", "#f39c12",
                f"Socket 存在但无响应：\n{sock}\n\n"
                f"尝试重启：systemctl --user restart podman.socket",
            )

    def _warn_no_socket(self) -> None:
        """提示未找到 socket（非模态，不阻塞主循环）"""
        from PyQt6.QtWidgets import QMessageBox as _MB

        box = _MB(self)
        box.setIcon(_MB.Icon.Critical)
        box.setWindowTitle("无法连接 Podman")
        box.setText("未找到 Podman socket。")
        box.setInformativeText(
            "请确认 Podman 正在运行：\n"
            "  systemctl --user start podman.socket\n\n"
            "然后重启本程序。\n\n"
            "（此提示不影响程序继续运行）"
        )
        box.setStandardButtons(_MB.StandardButton.Ok)
        # 非模态：不阻塞事件循环
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        box.show()

    def _set_conn_state(self, text: str, color: str, tooltip: str) -> None:
        self.lbl_conn.setText(f"● {text}")
        self.lbl_conn.setStyleSheet(
            f"color: {color}; font-weight: 600; padding: 0 6px;"
        )
        self.lbl_conn.setToolTip(tooltip)
        self.lbl_conn.setCursor(Qt.CursorShape.PointingHandCursor)

    # ─────────────────────────────────────────
    # 日志 / 终端
    # ─────────────────────────────────────────
    def _open_logs(self, cid: str, name: str) -> None:
        open_log_viewer(self.client, cid, name, self)

    def _open_compose_logs(self, service: str) -> None:
        """打开 compose 服务的日志（按容器名查找）"""
        # service 形如 "redis/redis"，取容器名部分
        name = service.split("/")[-1]
        try:
            containers = self.client.list_containers()
        except PodmanError as e:
            QMessageBox.warning(self, "获取容器失败", str(e))
            return

        target = None
        for c in containers:
            names = c.get("Names") or []
            if isinstance(names, str):
                names = [names]
            for n in names:
                n = str(n).lstrip("/")
                if n == name or n.startswith(f"{name}_") or n.startswith(f"{name}-"):
                    target = c
                    break
            if target:
                break

        if not target:
            QMessageBox.information(
                self, "未找到容器",
                f"服务「{name}」当前没有对应的运行容器。\n"
                f"请先启动该 Compose 项目。",
            )
            return

        cid = target.get("Id", "")
        cname = (target.get("Names") or [name])[0]
        open_log_viewer(self.client, cid, cname, self)

    def _open_terminal(self, cid: str, name: str) -> None:
        """在内置终端中打开容器 shell"""
        dlg = TerminalDialog(self.client, cid, name, self)
        dlg.exec()
        self._set_status(f"已关闭 {name} 的终端")

    # ─────────────────────────────────────────
    # 关于
    # ─────────────────────────────────────────
    def _about(self) -> None:
        try:
            v = self.client.version()
            comps = v.get("Components", [])
            engine = next((x for x in comps if x.get("Name") == "Podman Engine"), {})
            podman_ver = engine.get("Version", "未知")
        except PodmanError:
            podman_ver = "未知"

        QMessageBox.about(
            self, f"关于 {APP_NAME}",
            f"<h3>{APP_NAME} {APP_VERSION}</h3>"
            f"<p>KDE 原生 Podman 容器管理工具</p>"
            f"<p>基于 PyQt6 + Podman REST API</p>"
            f"<hr>"
            f"<p><b>Podman 版本:</b> {podman_ver}</p>"
            f"<p><b>Socket:</b> {self.client.socket_path or '未找到'}</p>",
        )

    # ─────────────────────────────────────────
    # 关闭
    # ─────────────────────────────────────────
    def closeEvent(self, event) -> None:
        """关闭时最小化到托盘（若托盘可用），否则退出"""
        if self.tray is not None and self.tray.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.show_message(
                "Podman 管理器仍在运行",
                "已最小化到系统托盘。点击托盘图标可恢复窗口。",
                3000,
            )
            return

        try:
            from core.async_task import shutdown_all
            shutdown_all(3000)
        except Exception:
            pass
        super().closeEvent(event)
