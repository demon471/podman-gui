"""系统托盘

功能：
  · 托盘图标（左键点击显示/隐藏主窗口）
  · 右键菜单：按状态分组的容器列表，直接停止/重启
  · 托盘气泡通知（复用 core.notify）
  · 容器列表定期刷新（10 秒）

菜单结构：
  ┌─────────────────────────────┐
  │ Podman 管理器  ● 已连接      │
  ├─────────────────────────────┤
  │ 显示主窗口                    │
  ├─────────────────────────────┤
  │ 运行中 (2)                    │
  │   ● redis        [停止][重启] │
  │   ● postgres     [停止][重启] │
  │ 已停止 (1)                    │
  │   ○ nacos        [启动]       │
  ├─────────────────────────────┤
  │ 全部启动 / 全部停止           │
  ├─────────────────────────────┤
  │ 退出                          │
  └─────────────────────────────┘
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from core.async_task import run_async
from core.notify import notifier
from core.podman_client import PodmanClient, PodmanError
from resources.icons import icon, state_icon

# 容器状态分组
RUNNING_STATES = ("running", "paused")
REFRESH_MS = 10_000


class TrayIcon(QObject):
    """系统托盘图标"""

    show_window = pyqtSignal()
    quit_app = pyqtSignal()
    open_logs = pyqtSignal(str, str)      # (cid, name)

    def __init__(self, client: PodmanClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._containers: list[dict] = []
        self._busy = False
        self._tasks: list = []

        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("当前环境不支持系统托盘")

        # parent=self：这两个对象必须挂在 TrayIcon 下面。
        # 不指定 parent 时它们是无主 QObject，Python GC 可能在
        # 任意时刻回收，而 QSystemTrayIcon 的底层实现仍持有指针
        # → Qt fatal → abort（进程直接闪退，无 traceback）。
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("Podman 管理器")
        self._update_icon()

        # 菜单（每次右键前重建，保证状态最新）
        # 注意：QMenu 的 parent 必须是 QWidget，TrayIcon 是 QObject，
        # 不能传 self —— 用 setParent(None) + 强引用保证不被 GC。
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._rebuild_menu)
        self.tray.setContextMenu(self.menu)

        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        # 定时刷新容器列表
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

        QTimer.singleShot(100, self.refresh)

    def stop(self) -> None:
        """停止定时刷新（应用退出前调用）

        不停止的话，退出过程中定时器可能再次触发 refresh，
        创建新的后台线程 —— 而此时 Qt 正在销毁对象，
        新线程 emit 会命中已释放对象导致 abort。
        """
        try:
            self._timer.stop()
        except Exception:
            pass
        self._busy = True       # 阻止后续 refresh 再起任务

    # ─────────────────────────────────────────
    # 图标
    # ─────────────────────────────────────────
    def _update_icon(self) -> None:
        """按连接状态切换托盘图标"""
        try:
            ok = self.client.ping()
        except Exception:
            ok = False

        # 托盘图标用海豹（随主题自动黑白变色）
        # 托盘用彩色应用图标：16–24px 下描边图形几乎糊成一团，
        # 实心彩色在任务栏里辨识度高得多
        from resources.icons import app_icon

        self.tray.setIcon(app_icon())

    # ─────────────────────────────────────────
    # 数据刷新
    # ─────────────────────────────────────────
    def refresh(self) -> None:
        """后台拉取容器列表"""
        if self._busy:
            return

        def work():
            return self.client.list_containers(all=True)

        def done(result):
            self._containers = result or []
            running = sum(
                1 for c in self._containers
                if str(c.get("State", "")).lower() in RUNNING_STATES
            )
            total = len(self._containers)
            self.tray.setToolTip(
                f"Podman 管理器 · {running}/{total} 运行中"
            )
            self._update_icon()

        def error(_msg):
            self.tray.setToolTip("Podman 管理器 · 连接失败")

        self._busy = True

        def wrap():
            try:
                return work()
            finally:
                self._busy = False

        task = run_async(wrap, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    # ─────────────────────────────────────────
    # 菜单构建
    # ─────────────────────────────────────────
    def _rebuild_menu(self) -> None:
        self.menu.clear()

        running = [
            c for c in self._containers
            if str(c.get("State", "")).lower() in RUNNING_STATES
        ]
        stopped = [
            c for c in self._containers
            if str(c.get("State", "")).lower() not in RUNNING_STATES
        ]

        # ── 显示主窗口 ──
        act_show = self.menu.addAction(icon("seal"), "显示主窗口")
        act_show.triggered.connect(self.show_window.emit)

        # ── 容器列表（运行中在前，无分组标题）──
        if self._containers:
            self.menu.addSeparator()

            ordered = running + stopped
            for c in ordered[:16]:
                is_running = str(c.get("State", "")).lower() in RUNNING_STATES
                self._add_container_entry(c, is_running=is_running)

            if len(ordered) > 16:
                more = self.menu.addAction(f"… 还有 {len(ordered) - 16} 个容器")
                more.setEnabled(False)

        # ── 退出 ──
        self.menu.addSeparator()
        act_quit = self.menu.addAction("退出")
        act_quit.triggered.connect(self.quit_app.emit)

    def _add_container_entry(self, c: dict, is_running: bool) -> None:
        """单个容器：子菜单里放操作"""
        name = (c.get("Names") or ["?"])[0]
        cid = c.get("Id", "")
        state = str(c.get("State", "")).lower()

        # 状态图标
        sub = self.menu.addMenu(state_icon(state, 16), name)

        if is_running:
            act_stop = sub.addAction(icon("stop"), "停止")
            act_stop.triggered.connect(
                lambda _=False, i=cid, n=name: self._do_action("stop", i, n)
            )

            act_restart = sub.addAction(icon("restart"), "重启")
            act_restart.triggered.connect(
                lambda _=False, i=cid, n=name: self._do_action("restart", i, n)
            )
        else:
            act_start = sub.addAction(icon("play"), "启动")
            act_start.triggered.connect(
                lambda _=False, i=cid, n=name: self._do_action("start", i, n)
            )

        # 局域网访问（有 TCP 端口时显示，只显示「访问」不显示地址）
        urls = self._container_urls(c)
        if urls and is_running:
            if len(urls) == 1:
                act_open = sub.addAction(icon("network"), "访问")
                act_open.setToolTip(urls[0][1])
                act_open.triggered.connect(
                    lambda _=False, u=urls[0][1]: self._open_url(u)
                )
            else:
                # 多端口：子菜单里列端口号
                open_menu = sub.addMenu(icon("network"), "访问")
                for label, url in urls:
                    act = open_menu.addAction(label)
                    act.triggered.connect(
                        lambda _=False, u=url: self._open_url(u)
                    )

        act_logs = sub.addAction(icon("log"), "查看日志")
        act_logs.triggered.connect(
            lambda _=False, i=cid, n=name: self.open_logs.emit(i, n)
        )

    # ─────────────────────────────────────────
    # 端口访问
    # ─────────────────────────────────────────
    def _container_urls(self, c: dict) -> list[tuple[str, str]]:
        """从容器信息提取局域网访问地址"""
        from ui.utils import host_ip

        ports = c.get("Ports") or []
        if not ports:
            return []

        lan_ip = host_ip()
        out = []
        for p in ports:
            hp = p.get("host_port", "")
            if not hp or p.get("protocol", "tcp") != "tcp":
                continue

            bind_ip = p.get("host_ip", "") or ""
            if bind_ip and bind_ip not in ("0.0.0.0", "::", "[::]"):
                host = bind_ip
            else:
                host = lan_ip

            label = f"{host}:{hp}"
            out.append((label, f"http://{label}"))

        return out

    def on_disconnected(self) -> None:
        """Podman 断开：清空容器列表"""
        self._containers = []
        self.tray.setToolTip("Podman 管理器 · 连接已断开")

    def _open_url(self, url: str) -> None:
        """用默认浏览器打开"""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        if QDesktopServices.openUrl(QUrl(url)):
            self.tray.showMessage(
                "已在浏览器打开", url,
                QSystemTrayIcon.MessageIcon.Information, 2500,
            )
        else:
            self.tray.showMessage(
                "打开失败", f"无法打开 {url}",
                QSystemTrayIcon.MessageIcon.Warning, 4000,
            )

    # ─────────────────────────────────────────
    # 操作
    # ─────────────────────────────────────────
    def _do_action(self, action: str, cid: str, name: str) -> None:
        """执行单个容器操作"""
        labels = {"start": "启动", "stop": "停止", "restart": "重启"}
        label = labels.get(action, action)

        fns = {
            "start": self.client.start_container,
            "stop": self.client.stop_container,
            "restart": self.client.restart_container,
        }
        fn = fns.get(action)
        if not fn:
            return

        def work():
            fn(cid)
            return True

        def done(_r):
            n = notifier()
            if action == "start":
                n.container_started(name)
            elif action == "stop":
                n.container_stopped(name)
            elif action == "restart":
                n.container_restarted(name)
            self.refresh()

        def error(msg):
            notifier().op_failed(label, name, msg)
            self.tray.showMessage(
                f"{label}失败",
                f"{name}：{msg}",
                QSystemTrayIcon.MessageIcon.Warning,
                4000,
            )

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    def _batch_action(self, action: str, containers: list[dict]) -> None:
        """批量操作"""
        if not containers:
            return

        labels = {"start": "启动", "stop": "停止", "restart": "重启"}
        label = labels.get(action, action)

        # 停止多个容器时确认
        if action == "stop" and len(containers) > 1:
            r = QMessageBox.question(
                None, "全部停止",
                f"将停止 {len(containers)} 个容器，确定继续？",
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        fns = {
            "start": self.client.start_container,
            "stop": self.client.stop_container,
            "restart": self.client.restart_container,
        }
        fn = fns.get(action)

        def work():
            ok, failed = 0, []
            for c in containers:
                name = (c.get("Names") or ["?"])[0]
                try:
                    fn(c.get("Id"))
                    ok += 1
                except Exception as e:
                    failed.append((name, str(e)[:60]))
            return ok, failed

        def done(result):
            ok, failed = result
            if failed:
                detail = "\\n".join(f"  {n}: {e}" for n, e in failed[:5])
                self.tray.showMessage(
                    f"{label}完成（部分失败）",
                    f"成功 {ok}，失败 {len(failed)}\\n{detail}",
                    QSystemTrayIcon.MessageIcon.Warning,
                    6000,
                )
            else:
                self.tray.showMessage(
                    f"已{label} {ok} 个容器",
                    "",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            self.refresh()

        def error(msg):
            self.tray.showMessage(
                f"{label}失败", msg,
                QSystemTrayIcon.MessageIcon.Critical, 5000,
            )

        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in self._tasks if t.alive]
        self._tasks.append(task)

    # ─────────────────────────────────────────
    # 交互
    # ─────────────────────────────────────────
    def _on_activated(self, reason) -> None:
        """左键单击/双击 → 显示主窗口"""
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window.emit()

    def show_message(self, title: str, body: str, ms: int = 3000) -> None:
        """外部调用：显示托盘气泡"""
        self.tray.showMessage(
            title, body,
            QSystemTrayIcon.MessageIcon.Information, ms,
        )

    def hide(self) -> None:
        self.tray.hide()

    def show(self) -> None:
        self.tray.show()
