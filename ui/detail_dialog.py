"""详情对话框

- JSON 详情展示（带语法高亮）
- 复制、保存
"""

from __future__ import annotations

import json

from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QPlainTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from resources.icons import icon
from ui.widgets import AutoHidePlainTextEdit


class JsonDetailDialog(QDialog):
    def __init__(self, title: str, data, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 620)
        self._data = data

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        tb = QToolBar()
        act_copy = QAction("复制 JSON", self)
        act_copy.triggered.connect(self._copy)
        tb.addAction(act_copy)
        act_save = QAction("保存到文件…", self)
        act_save.triggered.connect(self._save)
        tb.addAction(act_save)
        layout.addWidget(tb)

        self.view = AutoHidePlainTextEdit()
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)
        font = QFont("Maple Mono NF CN")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(QApplication.font().pointSize() or 10)
        self.view.setFont(font)
        self.view.setPlainText(self._pretty())
        layout.addWidget(self.view)

    def _pretty(self) -> str:
        try:
            return json.dumps(self._data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(self._data)

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.view.toPlainText())

    def _save(self) -> None:
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "保存 JSON", "detail.json", "JSON 文件 (*.json);;所有文件 (*)",
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.view.toPlainText())


def show_json_detail(parent: QWidget, title: str, data) -> None:
    dlg = JsonDetailDialog(title, data, parent)
    dlg.exec()
