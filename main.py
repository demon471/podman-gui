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


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    # 不设 DisplayName：Qt 会把它拼到窗口标题后面，造成重复
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName(APP_ID)  # Wayland 下关联桌面文件
    app.setWindowIcon(_app_icon())

    # 应用 KDE 主题（字体、图标、Breeze 风格）
    apply_kde_theme(app)

    # 应用自定义 QSS（颜色全部取自调色板）
    app.setStyleSheet(build_stylesheet())

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
