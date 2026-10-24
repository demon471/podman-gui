"""KDE 主题集成

让 PyQt6 应用完全融入 KDE Plasma：
- 使用 Breeze 控件风格
- 跟随系统配色方案（亮色/暗色）
- 使用 KDE 图标主题
- 监听系统主题变化并实时更新
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPalette
from PyQt6.QtWidgets import QApplication


def _kreadconfig(key: str, group: str = "General", file: str = "kdeglobals") -> str:
    """读取 KDE 配置"""
    try:
        r = subprocess.run(
            ["kreadconfig6", "--file", file, "--group", group, "--key", key],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def is_dark_mode() -> bool:
    """检测当前是否为暗色模式

    优先用实际生效的 QPalette（最可靠，反映真实渲染颜色），
    没有 QApplication 时才回退到读 KDE 配置文件。
    """
    # 1. 应用调色板（首选）：直接反映窗口实际背景色
    app = QApplication.instance()
    if app is not None:
        bg = app.palette().color(QPalette.ColorRole.Window)
        # 感知亮度（BT.601）
        lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
        return lum < 128

    # 2. 回退：KDE ColorScheme 名称
    scheme = _kreadconfig("ColorScheme")
    if scheme:
        return "dark" in scheme.lower()

    # 3. 回退：kdeglobals 里的窗口背景色
    bg_str = _kreadconfig("BackgroundNormal", group="Colors:Window")
    if bg_str:
        try:
            r, g, b = (int(x) for x in bg_str.split(",")[:3])
            return (0.299 * r + 0.587 * g + 0.114 * b) < 128
        except (ValueError, IndexError):
            pass

    return False


def apply_kde_theme(app: QApplication) -> None:
    """应用 KDE 原生主题

    关键：设置 QT_QPA_PLATFORMTHEME=kde 后，Qt 会自动：
    - 读取 KDE 的调色板（配色方案）
    - 读取 KDE 字体设置
    - 读取 KDE 图标主题
    - 使用 Breeze 控件风格
    """
    # 让 Qt 使用 KDE 平台主题（必须在 QApplication 创建前设置才完全生效，
    # 但运行中设置也能让 Qt 重新读取部分配置）
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")

    # 明确使用 Breeze 控件风格（KDE 默认）
    if hasattr(app.style(), "objectName"):
        app.setStyle("Breeze")

    # 应用 KDE 图标主题
    icon_theme = _kreadconfig("Theme", group="Icons")
    if icon_theme:
        QIcon.setThemeName(icon_theme)

    # 应用 KDE 字体
    font_str = _kreadconfig("font")
    if font_str:
        # 格式: "Sarasa UI SC,10,-1,5,50,0,0,0,0,0"
        parts = font_str.split(",")
        if parts:
            family = parts[0].strip()
            size = 10
            if len(parts) > 1:
                try:
                    size = int(parts[1])
                except ValueError:
                    pass
            if size < 0:  # 负值表示像素大小
                size = abs(size)
            font = QFont(family, size)
            app.setFont(font)


class ThemeWatcher(QObject):
    """监听 KDE 主题变化"""

    theme_changed = pyqtSignal(bool)  # 参数：是否为暗色

    def __init__(self, interval_ms: int = 2000, parent: QObject | None = None):
        super().__init__(parent)
        self._last_dark = is_dark_mode()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._check)
        self._timer.start(interval_ms)

    def _check(self) -> None:
        current = is_dark_mode()
        if current != self._last_dark:
            self._last_dark = current
            # 重新应用图标主题（KDE 会切换 breeze / breeze-dark）
            icon_theme = _kreadconfig("Theme", group="Icons")
            if icon_theme:
                QIcon.setThemeName(icon_theme)
            self.theme_changed.emit(current)

    @property
    def is_dark(self) -> bool:
        return self._last_dark


def get_icon(name: str, fallback: str = "application-x-executable") -> QIcon:
    """获取图标，带 KDE 主题回退"""
    icon = QIcon.fromTheme(name)
    if icon.isNull() and fallback:
        icon = QIcon.fromTheme(fallback)
    return icon
