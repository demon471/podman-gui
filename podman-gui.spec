# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置

生成单文件可执行：dist/podman-gui
"""

import sys
from pathlib import Path

PROJECT = Path(SPECPATH).resolve()

# ── 数据文件 ──
datas = [
    # 桌面文件随包分发（可选，安装时用）
    (str(PROJECT / "resources" / "podman-gui.desktop"), "resources"),
    # 应用图标（多尺寸 PNG，运行时用于窗口/任务栏图标）
    (str(PROJECT / "resources" / "appicon"), "resources/appicon"),
]

# ── 需要收集的 PyQt6 模块 ──
hiddenimports = [
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "PyQt6.QtSvg",
    # 终端依赖
    "pexpect",
    "ptyprocess",
    "pyte",
    "wcwidth",
    # VM 文件浏览（SFTP）：paramiko 在 core/vm/files.py 里于 try 内
    # 延迟导入，显式列出避免静态分析漏掉
    "paramiko",
    "paramiko.sftp",
    "paramiko.sftp_client",
    "paramiko.transport",
    "paramiko.channel",
    "bcrypt",
    "nacl",
    "cryptography",
]

# 排除不需要的大模块，减小体积
excludes = [
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQuick",
    "PyQt6.QtQml",
    "PyQt6.Qt3DCore",
    "PyQt6.QtMultimedia",
    "PyQt6.QtBluetooth",
    "PyQt6.QtDesigner",
    "PyQt6.QtHelp",
    "PyQt6.QtTest",
    "PyQt6.QtOpenGL",
    "PyQt6.QtPdf",
    "PyQt6.QtSql",
    "tkinter",
    "pdb",
    "lib2to3",
    "test",
]


a = Analysis(
    [str(PROJECT / "main.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={
        "qt": {
            # 只要必要的 Qt 插件
            "plugins": [
                "platforms/libqxcb.so",
                "platforms/libqwayland-generic.so",
                "platforms/libqwayland-egl.so",

                "imageformats/libqsvg.so",
                "imageformats/libqico.so",
                "imageformats/libqjpeg.so",
                "iconengines/libqsvgicon.so",
                "styles/libqcleanlooksstyle.so",
            ],
        }
    },
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# ── 二进制瘦身 ──
# excludes 只能挡 Python import，挡不住 ldd 依赖链和 Qt 插件
# 拖进来的原生库。以下都是 GUI 应用用不到的：
#   · 视频编解码（x265/aom/SVT-AV1/x264/dav1d/rav1e/heif/jxl）
#     —— Qt 图像插件链拖进来的，我们只需要 jpeg/png/svg/ico
#   · GTK 链（gtk-3/glycin/cairo/gio…）—— libqgtk3 平台主题
#     的依赖，KDE 下走 xcb/wayland + kde 主题，不用 GTK
#   · KF6BreezeIcons / KF6WidgetsAddons —— KDE 图标包
#   · QtQuick/Qml/Pdf —— excludes 排了 Python 端，但 .so 仍被
#     依赖扫描拖进来
import re as _re

_BIN_EXCLUDE = _re.compile(
    r"lib(x265|aom|SvtAv1Enc|x264|dav1d|rav1e|heif|jxl|gtk-3|glycin"
    r"|cairo|gio-2|glib-2|gobject-2|gmodule-2|gthread-2|pango"
    r"|harfbuzz|KF6[A-Z]\w+"
    r"|openh264|xml2|pixman|ldap|lber|krb5|k5crypto|gssapi"
    r"|Qt6Quick|Qt6Qml|Qt6Pdf|Qt6Designer|Qt6Help|Qt6Test"
    r"|Qt6WebEngine|Qt6Multimedia|Qt6Bluetooth|Qt6Nfc|Qt6SerialPort"
    r"|Qt6Sensors|Qt6Positioning|Qt6Location|Qt6WebChannel)"
)

# Qt 翻译文件（.qm）与 locale 数据：界面只有中文/英文，
# Qt 自带的几十种翻译用不上
a.binaries = [
    b for b in a.binaries
    if "/translations/" not in b[0] and not b[0].endswith(".qm")
]

a.binaries = [
    b for b in a.binaries if not _BIN_EXCLUDE.search(b[0])
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="podman-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # GUI 程序，不显示终端
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
