"""全局配置与常量"""

from pathlib import Path

APP_NAME = "Podman 管理器"
APP_ID = "podman-gui"
APP_VERSION = "1.0.0"

import sys

# 项目根目录 / 资源目录
# PyInstaller 单文件模式下，资源被解压到 sys._MEIPASS 临时目录
if getattr(sys, "frozen", False):
    ROOT = Path(sys._MEIPASS)
else:
    ROOT = Path(__file__).resolve().parent.parent

RESOURCES = ROOT / "resources"

# Podman socket 探测优先级
SOCKET_CANDIDATES = [
    "/run/user/{uid}/podman/podman.sock",   # Linux rootless（最常用）
    "/run/podman/podman.sock",              # Linux rootful
    "/var/run/docker.sock",                 # Docker 兼容
]

# 自动刷新间隔（毫秒）
REFRESH_INTERVAL_MS = 5000

# 日志最多保留行数
LOG_MAX_LINES = 5000
