"""pytest 公共配置与夹具

所有测试共享的初始化逻辑放这里，避免每个文件重复。

设计原则
────────
· **不依赖真实 VM 运行**：绝大多数测试只验证纯逻辑
  （状态机、算法、字符串处理），秒级完成。
· **需要 VM 的测试用标记隔离**：`@pytest.mark.needs_vm`，
  默认不跑，避免 CI 或日常提交时卡住。
· **Qt 测试用 offscreen**：不需要真实显示服务器。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 测试一律走 offscreen，避免依赖 Wayland/X11
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 不加载 KDE 平台主题（测试环境可能没有）
os.environ.setdefault("QT_QPA_PLATFORMTHEME", "")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "needs_vm: 需要真实虚拟机运行（默认跳过）"
    )
    config.addinivalue_line(
        "markers", "needs_qt: 需要 QApplication"
    )
    config.addinivalue_line(
        "markers", "slow: 耗时超过 5 秒"
    )


@pytest.fixture(scope="session")
def qapp():
    """全局 QApplication（Qt 只允许一个实例）"""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def temp_vm_dir(tmp_path):
    """临时 VM 目录（模拟一台 VM 的目录结构）"""
    d = tmp_path / "testvm"
    d.mkdir()
    return d


@pytest.fixture
def real_vm():
    """真实 VM（仅在 -m needs_vm 时使用）"""
    from core.vm import manager

    vm = manager.load("rocky")
    if vm is None:
        pytest.skip("rocky 虚拟机不存在")
    return vm
