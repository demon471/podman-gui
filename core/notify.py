"""KDE 桌面通知

使用 notify-send（libnotify）发送原生通知。
KDE Plasma 显示为右上角气泡，并进入通知中心。

选择 notify-send 而非 D-Bus 直连的原因：
- PyQt6 的 QtDBus 对 a{sv} 嵌套类型序列化有 bug（会崩溃）
- notify-send 是标准工具，稳定可靠
- 支持自定义图标、紧急程度、超时、点击动作
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from core.config import APP_ID, APP_NAME

# 图标缓存目录
_ICON_DIR = Path.home() / ".cache" / "podman-gui" / "icons"


class Notifier:
    """桌面通知发送器"""

    LOW = "low"
    NORMAL = "normal"
    CRITICAL = "critical"

    def __init__(self) -> None:
        self._available = shutil.which("notify-send") is not None
        self._icons_ready = False

    @property
    def available(self) -> bool:
        return self._available

    # ─────────────────────────────────────────
    # 图标
    # ─────────────────────────────────────────
    def _ensure_icons(self) -> None:
        """把内置 SVG 渲染成 PNG 供通知使用

        通知气泡背景在 KDE 下通常是深色，因此图标统一用浅色，
        否则深色图标叠在深色背景上会变成一团黑块。
        """
        if self._icons_ready:
            return
        try:
            from PyQt6.QtWidgets import QApplication
            if QApplication.instance() is None:
                return

            from resources.icons import SVG, _render_svg

            _ICON_DIR.mkdir(parents=True, exist_ok=True)
            for name, svg in SVG.items():
                target = _ICON_DIR / f"{name}.png"
                if target.exists():
                    continue
                # 统一浅色（Breeze 通知区前景色）
                pix = _render_svg(svg, "#eff0f1", 96)
                pix.save(str(target), "PNG")
            self._icons_ready = True
        except Exception:
            pass

    def _icon_arg(self, name: str) -> str:
        """返回图标路径或空字符串"""
        self._ensure_icons()
        p = _ICON_DIR / f"{name}.png"
        if p.exists():
            return str(p)
        # 回退到系统图标名
        return {
            "play": "media-playback-start",
            "stop": "media-playback-stop",
            "restart": "view-refresh",
            "delete": "edit-delete",
            "add": "list-add",
            "info": "dialog-information",
            "podman": "utilities-terminal",
            "container": "application-x-executable",
            "image": "image-x-generic",
            "volume": "drive-harddisk",
            "network": "network-workgroup",
            "pod": "folder",
        }.get(name, "dialog-information")

    # ─────────────────────────────────────────
    # 发送
    # ─────────────────────────────────────────
    def send(
        self,
        summary: str,
        body: str = "",
        *,
        icon_name: str = "podman",
        urgency: str = NORMAL,
        timeout_ms: int = 5000,
    ) -> bool:
        """发送通知，成功返回 True"""
        if not self._available:
            return False

        cmd = [
            "notify-send",
            "--app-name", APP_NAME,
            "--urgency", urgency,
            "--expire-time", str(timeout_ms),
            "--icon", self._icon_arg(icon_name),
            "--hint", f"string:desktop-entry:{APP_ID}",
            summary,
        ]
        if body:
            cmd.append(body)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return r.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    # ─────────────────────────────────────────
    # 便捷方法
    # ─────────────────────────────────────────
    def info(self, summary: str, body: str = "") -> bool:
        return self.send(summary, body, icon_name="info")

    def success(self, summary: str, body: str = "") -> bool:
        return self.send(summary, body, icon_name="play")

    def warn(self, summary: str, body: str = "") -> bool:
        return self.send(summary, body, icon_name="info", timeout_ms=8000)

    def error(self, summary: str, body: str = "") -> bool:
        return self.send(summary, body, icon_name="delete",
                         urgency=self.CRITICAL, timeout_ms=10000)

    # ── 容器 ──
    def container_started(self, name: str) -> bool:
        return self.send(f"容器已启动 · {name}", "正在运行", icon_name="play")

    def container_stopped(self, name: str) -> bool:
        return self.send(f"容器已停止 · {name}", "已停止运行", icon_name="stop")

    def container_restarted(self, name: str) -> bool:
        return self.send(f"容器已重启 · {name}", "已重新启动", icon_name="restart")

    def container_deleted(self, name: str) -> bool:
        return self.send(f"容器已删除 · {name}", "已从系统中移除", icon_name="delete")

    def container_created(self, name: str) -> bool:
        return self.send(f"容器已创建 · {name}", "创建成功", icon_name="add")

    # ── 镜像 ──
    def image_pulled(self, ref: str) -> bool:
        return self.send("镜像拉取完成", ref, icon_name="image")

    def image_deleted(self, name: str) -> bool:
        return self.send("镜像已删除", name, icon_name="delete")

    def images_pruned(self, count: int, space: str) -> bool:
        return self.send("镜像清理完成", f"删除 {count} 个 · 回收 {space}",
                         icon_name="clean")

    # ── 卷 / 网络 ──
    def volume_created(self, name: str) -> bool:
        return self.send("卷已创建", name, icon_name="volume")

    def volume_deleted(self, name: str) -> bool:
        return self.send("卷已删除", name, icon_name="delete")

    def network_created(self, name: str) -> bool:
        return self.send("网络已创建", name, icon_name="network")

    def network_deleted(self, name: str) -> bool:
        return self.send("网络已删除", name, icon_name="delete")

    # ── Pod ──
    def pod_action(self, action: str, name: str) -> bool:
        return self.send(f"Pod 已{action}", name, icon_name="pod")

    # ── 失败 ──
    def op_failed(self, action: str, name: str, reason: str) -> bool:
        return self.error(f"{action}失败", f"「{name}」：{reason}")


_notifier: Notifier | None = None


def notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier
