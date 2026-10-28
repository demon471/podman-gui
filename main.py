#!/usr/bin/env python3
"""Podman 管理器 — KDE 原生容器管理工具

入口文件。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 必须在 QApplication 创建前设置，才能让 Qt 加载 KDE 平台主题
os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")
# Wayland 下启用高分屏支持
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

# 让模块可以被导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 守护进程模式：不创建任何 Qt 对象，纯后台运行。
# 必须在导入 Qt 之前处理 —— 单文件打包时 Qt 初始化很重，
# 且守护进程不需要图形环境（systemd 用户服务里可能没有 DISPLAY）。
if "--daemon" in sys.argv:
    sys.argv.remove("--daemon")
    _root = None
    if "--root" in sys.argv:
        _i = sys.argv.index("--root")
        if _i + 1 < len(sys.argv):
            _root = sys.argv[_i + 1]
            del sys.argv[_i:_i + 2]

    # 把剩下的参数（--install / --status / -v 等）原样传给 daemon，
    # 否则 `--daemon --install` 会被当成「直接运行」而卡住。
    _args = list(sys.argv[1:])
    if _root:
        _args = ["--root", _root] + _args

    from core.vm.daemon import main as _daemon_main

    sys.exit(_daemon_main(_args))

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from core.config import APP_ID, APP_NAME, APP_VERSION
from core.kde_theme import apply_kde_theme, is_dark_mode
from core.podman_client import PodmanClient
from resources.icons import icon
from ui.main_window import MainWindow
from ui.style import build_stylesheet


def _app_icon() -> QIcon:
    """应用图标：优先用 PNG 文件，回退到内置 SVG"""
    from core.config import RESOURCES

    png = RESOURCES / "appicon" / "podman-gui-256.png"
    if png.exists():
        return QIcon(str(png))
    return icon("seal", 256)


def _load_translations(app: QApplication) -> None:
    """加载 Qt 中文翻译（让 Yes/No 等标准按钮显示中文）

    Qt 内置按钮文案由 qtbase_*.qm 提供。不加载时，
    即使系统是中文环境，QMessageBox 仍显示英文 Yes/No。
    """
    from PyQt6.QtCore import QLibraryInfo, QLocale, QTranslator

    translator = QTranslator(app)

    # 搜索路径：PyQt6 自带 + 系统 Qt6
    candidates = [
        QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath),
        "/usr/share/qt6/translations",
        "/usr/share/qt/translations",
    ]

    for path in candidates:
        if not path:
            continue
        # 优先中文，其次用系统 locale
        for locale in ("zh_CN", QLocale.system().name()):
            if translator.load(f"qtbase_{locale}", path):
                app.installTranslator(translator)
                app._qt_translator = translator   # 保持引用，防止被回收
                return


def _install_signal_handlers(app, win) -> None:
    """让 SIGTERM/SIGINT 走正常退出流程（休眠虚拟机）

    Qt 的事件循环不处理 Unix 信号，需要借助 QTimer 把
    信号回调转成主线程操作。这样 `kill <pid>`、桌面注销、
    systemd 停止服务都能优雅地把 VM 转入内存快照，
    而不是留下没人管的 QEMU 进程。
    """
    import signal

    from PyQt6.QtCore import QTimer

    def handler(signum, frame):
        print(f"[main] 收到信号 {signum}，开始保存虚拟机状态…", flush=True)
        try:
            # 触发与「托盘退出」相同的休眠流程，
            # 完成后由 _final_quit 调用 app.quit()
            win._suspend_vms_then_quit()
        except Exception as e:
            print(f"[main] 信号处理失败: {e}", flush=True)
            app.quit()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            # 非主线程或平台不支持时忽略
            pass


def _run_autotest(win) -> None:
    """打包版自动化冒烟测试（仅 PODMAN_GUI_AUTOTEST=1 时启用）"""
    from PyQt6.QtCore import QTimer

    def step1():
        print("[autotest] 切到虚拟机页", flush=True)
        win._on_nav("vms")
        QTimer.singleShot(1500, step2)

    def step2():
        from core.vm import images, manager

        # 清理可能残留的测试 VM
        old = manager.load("_autotest")
        if old is not None:
            manager.delete_vm(old, remove_disk=True)

        from ui.vm_wizard import VMWizard

        print("[autotest] 打开向导", flush=True)
        wiz = VMWizard(win)

        def fill():
            specs = images.list_images()
            t = next((s for s in specs if s.family == "alpine"), specs[0])
            row = next(
                (i for i, s in enumerate(specs) if s.key == t.key), 0
            )
            wiz.img_list.setCurrentRow(row)
            wiz.ed_name.setText("_autotest")
            print("[autotest] 触发创建", flush=True)
            wiz._do_create()

        QTimer.singleShot(1200, fill)
        rc = wiz.exec()
        print("[autotest] 向导返回 %d created=%s" % (
            rc, wiz.created_vm.spec.name if wiz.created_vm else None
        ), flush=True)

        if wiz.created_vm:
            win.page_vms.refresh_silent()
            win.page_vms._rebuild()
            print("[autotest] 列表已刷新", flush=True)

            # 关键：继续走「启动」路径 —— 用户报的闪退是在
            # 创建完成后（很可能紧接着点了启动）。
            QTimer.singleShot(1500, step_start)
            return

        QTimer.singleShot(5000, step3)

    def step_start():
        print("[autotest] 选中新 VM 并启动", flush=True)
        page = win.page_vms
        for r in range(page.model.rowCount()):
            if page.model.item(r, 0).text() == "_autotest":
                page.table.selectRow(r)
                break
        page._start_selected()
        # 等启动探测完成（最多 90 秒）
        QTimer.singleShot(90000, step_check)

    def step_check():
        print("[autotest] ✅ 启动流程走完，仍存活", flush=True)
        from core.vm import manager

        vm = manager.load("_autotest")
        if vm is not None:
            if vm.running:
                manager.stop_vm(vm)
            manager.delete_vm(vm, remove_disk=True)
            print("[autotest] 已清理", flush=True)
        QApplication.instance().quit()

    def step3():
        print("[autotest] ✅ 全流程通过，5s 内无崩溃", flush=True)
        from core.vm import manager

        vm = manager.load("_autotest")
        if vm is not None:
            manager.delete_vm(vm, remove_disk=True)
            print("[autotest] 已清理", flush=True)
        QApplication.instance().quit()

    QTimer.singleShot(3000, step1)


def _restore_vms(win) -> None:
    """后台恢复上次希望运行的虚拟机（不阻塞界面）"""
    from core.async_task import run_async

    def work():
        from core.vm import manager as vm_manager

        return vm_manager.restore_desired_vms()

    def done(results):
        if not results:
            return
        ok_n = sum(1 for _, ok, _ in results if ok)
        print(f"[main] 已恢复 {ok_n}/{len(results)} 台虚拟机")

        # 通知虚拟机页：这些 VM 现在在跑了，重新开始保活计时
        try:
            page = getattr(win, "page_vms", None)
            if page is not None:
                page._sync_keepalive()
                page.refresh_silent()
        except Exception:
            pass

    def error(msg):
        print(f"[main] 恢复虚拟机失败: {msg}")

    run_async(work, on_done=done, on_error=error, parent=win)


def main() -> int:
    app = QApplication(sys.argv)
    _load_translations(app)
    app.setApplicationName(APP_ID)
    # 不设 DisplayName：Qt 会把它拼到窗口标题后面，造成重复
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName(APP_ID)  # Wayland 下关联桌面文件
    app.setWindowIcon(_app_icon())

    # 应用 KDE 主题（字体、图标、Breeze 风格）
    apply_kde_theme(app)

    # 应用自定义 QSS（颜色全部取自调色板）
    app.setStyleSheet(build_stylesheet())

    # 状态存储迁移（旧 QSettings / vm.json → SQLite）
    # 幂等，只在数据库为空时搬一次；原文件保留不删。
    try:
        from core import state

        state.migrate_from_legacy()
    except Exception as e:
        print(f"[main] 状态迁移失败: {e}")

    # 清理历史创建失败留下的空壳 VM 目录
    # （无 vm.json 的目录会导致同名再创建永远报「名称已被占用」）
    try:
        from core.vm import manager as vm_manager

        n = vm_manager.cleanup_orphan_dirs()
        if n:
            print(f"[main] 已清理 {n} 个残留 VM 目录")
    except Exception as e:
        print(f"[main] 清理残留目录失败: {e}")

    client = PodmanClient()
    win = MainWindow(client)
    win.show()

    # 自动化测试钩子：PODMAN_GUI_AUTOTEST=1 时自动走一遍
    # 「打开向导 → 创建 VM → 关闭弹窗」流程，用于验证打包版
    # 是否在该路径上崩溃（PyInstaller 环境与源码环境有差异）。
    if os.environ.get("PODMAN_GUI_AUTOTEST") == "1":
        _run_autotest(win)

    # 处理 SIGTERM / SIGINT（kill、注销、systemd 停止 GUI）
    # 这些信号不会触发 Qt 的 closeEvent，如果不拦截，
    # 虚拟机就会在无人管理的情况下继续运行。
    _install_signal_handlers(app, win)

    # 恢复上次希望运行的虚拟机
    # QEMU 用 -daemonize 独立运行，正常退出应用不会停 VM，
    # 所以通常这里是空操作；只有应用被强杀或系统重启后才需要拉起。
    _restore_vms(win)

    rc = app.exec()

    # 退出前等待后台任务，防止 QThread 被强制终止
    try:
        from core.async_task import shutdown_all
        shutdown_all(3000)
    except Exception:
        pass

    return rc


if __name__ == "__main__":
    sys.exit(main())
