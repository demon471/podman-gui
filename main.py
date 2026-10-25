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
