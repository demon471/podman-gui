"""内置终端

用 pyte（VT100 终端模拟器）+ pexpect（PTY 控制）实现，
在 QPlainTextEdit 中渲染容器内的 shell。

架构：
  QPlainTextEdit ←渲染← pyte.Screen  ←喂数据← pexpect 子进程
       ↓ 键盘事件
   编码成 ANSI 序列 → pexpect.send()
"""

from __future__ import annotations

import shutil
import threading

import pexpect
import pyte
from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QTextCursor
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QPlainTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.podman_client import PodmanClient, PodmanError
from resources.icons import ICON_SIZE_TOOLBAR, icon
from ui.widgets import AutoHidePlainTextEdit


class TerminalView(AutoHidePlainTextEdit):
    """终端显示 + 键盘捕获"""

    input_received = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("logView")
        self.setReadOnly(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setUndoRedoEnabled(False)
        self.setCursorWidth(6)

        font = QFont("Maple Mono NF CN")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(QApplication.font().pointSize() or 10)
        self.setFont(font)

        # 禁用富文本，纯文本
        self.setPlainText("")

    def keyPressEvent(self, e: QKeyEvent) -> None:
        """把按键转换成终端序列"""
        key = e.key()
        text = e.text()

        # 特殊键映射
        special = {
            Qt.Key.Key_Return: "\r",
            Qt.Key.Key_Enter: "\r",
            Qt.Key.Key_Backspace: "\x7f",
            Qt.Key.Key_Tab: "\t",
            Qt.Key.Key_Escape: "\x1b",
            Qt.Key.Key_Up: "\x1b[A",
            Qt.Key.Key_Down: "\x1b[B",
            Qt.Key.Key_Right: "\x1b[C",
            Qt.Key.Key_Left: "\x1b[D",
            Qt.Key.Key_Home: "\x1b[H",
            Qt.Key.Key_End: "\x1b[F",
            Qt.Key.Key_Delete: "\x1b[3~",
            Qt.Key.Key_PageUp: "\x1b[5~",
            Qt.Key.Key_PageDown: "\x1b[6~",
            Qt.Key.Key_Insert: "\x1b[2~",
        }

        # Ctrl 组合
        mods = e.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                seq = chr(key - Qt.Key.Key_A + 1)
                self.input_received.emit(seq)
                return
            if key == Qt.Key.Key_C:
                self.input_received.emit("\x03")
                return
            if key == Qt.Key.Key_D:
                self.input_received.emit("\x04")
                return
            if key == Qt.Key.Key_L:
                self.input_received.emit("\x0c")
                return
            if key == Qt.Key.Key_Z:
                self.input_received.emit("\x1a")
                return

        if key in special:
            self.input_received.emit(special[key])
            return

        if text:
            self.input_received.emit(text)
            return

        super().keyPressEvent(e)


class TerminalDialog(QDialog):
    """容器终端对话框"""

    def __init__(
        self,
        client: PodmanClient,
        cid: str,
        name: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.client = client
        self.cid = cid
        self.name = name

        self._child: pexpect.spawn | None = None
        self._screen = pyte.Screen(120, 30)
        self._stream = pyte.ByteStream(self._screen)
        self._pending = bytearray()
        self._lock = threading.Lock()
        self._running = False

        self.setWindowTitle(f"终端 - {name}")
        self.resize(960, 640)
        self._build_ui()
        self._start_shell()

        # 定时刷新渲染（60ms 一次，约 16fps）
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._render)
        self._render_timer.start(60)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 工具栏
        tb = QToolBar()
        tb.setIconSize(ICON_SIZE_TOOLBAR)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.setFloatable(False)

        self.lbl_info = QLabel(f"容器: {self.name}")
        tb.addWidget(self.lbl_info)

        spacer = QWidget()
        spacer.setSizePolicy(
            spacer.sizePolicy().Policy.Expanding,
            spacer.sizePolicy().Policy.Preferred,
        )
        tb.addWidget(spacer)

        tb.addWidget(QLabel("Shell:"))
        self.cmb_shell = QComboBox()
        self.cmb_shell.addItems(["/bin/sh", "/bin/bash", "/bin/ash", "/bin/zsh"])
        self.cmb_shell.setFixedWidth(120)
        tb.addWidget(self.cmb_shell)

        act_restart = QAction(icon("restart"), "重连", self)
        act_restart.triggered.connect(self._restart_shell)
        tb.addAction(act_restart)

        act_clear = QAction("清屏", self)
        act_clear.triggered.connect(self._clear)
        tb.addAction(act_clear)

        layout.addWidget(tb)

        # 终端视图
        self.view = TerminalView()
        self.view.input_received.connect(self._on_input)
        layout.addWidget(self.view)

        # 底部提示
        self.lbl_status = QLabel("正在连接…")
        self.lbl_status.setObjectName("hintLabel")
        layout.addWidget(self.lbl_status)

    # ─────────────────────────────────────────
    # 启动 shell
    # ─────────────────────────────────────────
    def _start_shell(self, shell: str | None = None) -> None:
        shell = shell or self.cmb_shell.currentText()
        self._stop_shell()

        podman = shutil.which("podman") or "/usr/bin/podman"
        args = [
            "exec", "-it",
            self.cid,
            shell,
        ]

        try:
            child = pexpect.spawn(
                podman, args,
                encoding=None,      # 用 bytes
                timeout=None,
                dimensions=(30, 120),
                echo=False,
            )
            self._child = child
            self._running = True
            self.lbl_status.setText(f"已连接 {shell} · {self.name}")
            threading.Thread(target=self._reader_loop, daemon=True).start()
        except (pexpect.ExceptionPexpect, OSError) as e:
            self.lbl_status.setText(f"连接失败: {e}")
            self._append_plain(f"\n[连接失败] {e}\n")

    def _stop_shell(self) -> None:
        self._running = False
        if self._child is not None:
            try:
                self._child.close(force=True)
            except Exception:
                pass
            self._child = None

    def _restart_shell(self) -> None:
        self._screen.reset()
        self.view.clear()
        self._start_shell()

    def _clear(self) -> None:
        self._screen.reset()
        self.view.clear()

    # ─────────────────────────────────────────
    # 读线程
    # ─────────────────────────────────────────
    def _reader_loop(self) -> None:
        """后台读取 PTY 输出，喂给 pyte"""
        while self._running and self._child is not None:
            try:
                data = self._child.read_nonblocking(size=4096, timeout=0.05)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                with self._lock:
                    self._pending.extend("\n[连接已断开]\n".encode("utf-8"))
                self._running = False
                break
            except Exception:
                break

            if data:
                with self._lock:
                    self._pending.extend(data)

    # ─────────────────────────────────────────
    # 渲染
    # ─────────────────────────────────────────
    def _render(self) -> None:
        """把 pyte 屏幕内容画到 QPlainTextEdit"""
        with self._lock:
            if not self._pending:
                return
            chunk = bytes(self._pending)
            self._pending.clear()

        try:
            self._stream.feed(chunk)
        except Exception:
            return

        # 生成纯文本
        lines = []
        for y in range(self._screen.lines):
            row = self._screen.buffer[y]
            line = "".join(row[x].data for x in range(self._screen.columns))
            lines.append(line.rstrip())

        # 去掉尾部空行
        while lines and not lines[-1]:
            lines.pop()

        text = "\n".join(lines)

        # 保持滚动位置
        sb = self.view.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 4

        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.view.setPlainText(text)

        # 光标定位到屏幕光标处
        cy = min(self._screen.cursor.y, len(lines) - 1) if lines else 0
        cx = self._screen.cursor.x
        if cy >= 0 and lines:
            block = self.view.document().findBlockByNumber(cy)
            if block.isValid():
                c = QTextCursor(block)
                c.movePosition(
                    QTextCursor.MoveOperation.Right,
                    QTextCursor.MoveMode.MoveAnchor,
                    min(cx, len(lines[cy])),
                )
                self.view.setTextCursor(c)

        if at_bottom:
            sb.setValue(sb.maximum())

    def _append_plain(self, text: str) -> None:
        self.view.appendPlainText(text)

    # ─────────────────────────────────────────
    # 输入
    # ─────────────────────────────────────────
    def _on_input(self, seq: str) -> None:
        if self._child is None or not self._running:
            return
        try:
            self._child.send(seq.encode("utf-8", errors="ignore"))
        except Exception:
            pass

    # ─────────────────────────────────────────
    # 窗口尺寸变化时同步 PTY
    # ─────────────────────────────────────────
    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._sync_size()

    def _sync_size(self) -> None:
        if self._child is None:
            return
        fm = self.view.fontMetrics()
        cols = max(20, self.view.viewport().width() // max(1, fm.horizontalAdvance("M")))
        rows = max(5, self.view.viewport().height() // max(1, fm.height()))
        if (rows, cols) != (self._screen.lines, self._screen.columns):
            self._screen.resize(rows, cols)
            try:
                self._child.setwinsize(rows, cols)
            except Exception:
                pass

    # ─────────────────────────────────────────
    # 关闭
    # ─────────────────────────────────────────
    def closeEvent(self, e) -> None:
        self._render_timer.stop()
        self._stop_shell()
        super().closeEvent(e)
