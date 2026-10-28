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
                "platformthemes/libqgtk3.so",
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
