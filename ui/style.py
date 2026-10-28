"""KDE / Dolphin 风格样式表

设计原则：
- 不硬编码颜色，全部从 QApplication 调色板取 → 自动适配亮/暗主题
- 紧凑尺寸，向 Dolphin 靠拢（行高 26-28px，图标 16px）
- 扁平化，去掉多余边框和阴影，hover 用 AlternateBase 淡染
- 选中态用 Highlight 色，与系统一致
"""

from __future__ import annotations

from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import QApplication


def _c(role: QPalette.ColorRole, group=QPalette.ColorGroup.Active) -> str:
    return QApplication.palette().color(group, role).name()


def _rgba(role: QPalette.ColorRole, alpha: float) -> str:
    """调色板颜色转 rgba（用于半透明滚动条）"""
    c = QApplication.palette().color(role)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _font_sizes() -> dict[str, str]:
    """基于系统字体推导各级字号，避免硬编码导致大小不一

    - base  : 系统默认（如 10pt）
    - small : base - 1pt（提示、统计等次要文字）
    - mono  : base    （日志/终端等宽文字，与正文同大）
    """
    app = QApplication.instance()
    pt = app.font().pointSize() if app else 10
    if pt <= 0:            # 少数环境用像素
        px = app.font().pixelSize() if app else 13
        pt = max(9, round(px * 0.75))
    return {
        "base": f"{pt}pt",
        "small": f"{max(8, pt - 1)}pt",
        "mono": f"{pt}pt",
    }


def semantic_colors() -> dict[str, str]:
    """成功 / 失败语义色

    Breeze 的 positive/negative 在暗色主题下会偏亮，这里按窗口
    亮度分两套，保证任何主题下都有足够对比度。
    代码里需要动态着色（比如按钮闪一下绿色对勾）时直接取这里，
    不要另写一套色值，否则换主题就不同步了。
    """
    pal = QApplication.palette()
    c = pal.color(QPalette.ColorRole.Window)
    is_dark = (c.red() * 299 + c.green() * 587 + c.blue() * 114) / 1000 < 128
    if is_dark:
        return {"success": "#3ddc84", "danger": "#ff6b6b"}
    return {"success": "#1a7f37", "danger": "#c0392b"}


def build_stylesheet() -> str:
    pal = QApplication.palette()
    fs = _font_sizes()
    window = pal.color(QPalette.ColorRole.Window).name()
    window_text = pal.color(QPalette.ColorRole.WindowText).name()
    base = pal.color(QPalette.ColorRole.Base).name()
    text = pal.color(QPalette.ColorRole.Text).name()
    highlight = pal.color(QPalette.ColorRole.Highlight).name()
    highlight_text = pal.color(QPalette.ColorRole.HighlightedText).name()
    button = pal.color(QPalette.ColorRole.Button).name()
    button_text = pal.color(QPalette.ColorRole.ButtonText).name()
    mid = pal.color(QPalette.ColorRole.Mid).name()
    alternate = pal.color(QPalette.ColorRole.AlternateBase).name()
    disabled_text = pal.color(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText
    ).name()

    # 边框色：比 Mid 更淡，贴近 Breeze 的 hairline
    border = mid

    # 滚动条滑块：跟随主题的 WindowText，低透明度
    sb_idle = _rgba(QPalette.ColorRole.WindowText, 0.22)
    sb_hover = _rgba(QPalette.ColorRole.WindowText, 0.45)

    # 语义色（成功/失败），供 QSS 和代码里的动态反馈共用
    sem = semantic_colors()
    success = sem["success"]
    danger = sem["danger"]

    return f"""
/* ═══════════ 全局 ═══════════ */
QWidget {{
    color: {window_text};
}}

QMainWindow, QDialog {{
    background-color: {window};
}}

/* ═══════════ 侧边栏 ═══════════ */
QWidget#sidebar {{
    background-color: {window};
    border-right: 1px solid {border};
}}

/* 侧边栏内的子容器也继承 window，防止出现色块 */
QWidget#sidebar > QWidget {{
    background-color: {window};
}}

QListWidget#sidebarList {{
    /* 显式设为 window，避免继承 Base 导致与标题栏色差 */
    background-color: {window};
    border: none;
    outline: none;
    padding: 2px 4px;
}}

QListWidget#sidebarList::item {{
    padding: 0px 10px;
    border-radius: 4px;
    margin: 1px 4px;
    color: {window_text};
}}

/* 悬停：淡背景 */
QListWidget#sidebarList::item:hover:!selected {{
    background-color: {alternate};
}}

/* 选中：高亮背景（不做左侧边框，避免内容位移和空白） */
QListWidget#sidebarList::item:selected {{
    background-color: {highlight};
    color: {highlight_text};
}}

/* 分组标题项（NoItemFlags，不会被选中） */
QListWidget#sidebarList::item:disabled {{
    color: {disabled_text};
    font-size: {fs['small']};
    font-weight: 600;
    padding-top: 8px;
    background: transparent;
}}

/* ═══════════ 工具栏 ═══════════ */
QToolBar {{
    background-color: {window};
    border: none;
    border-bottom: 1px solid {border};
    padding: 4px 6px;
    spacing: 2px;
}}

/* 分隔符：仅 1px 竖线，上下留白，左右不留额外空间 */
QToolBar::separator {{
    background-color: {border};
    width: 1px;
    margin: 6px 4px;
}}

QToolBar QToolButton {{
    padding: 5px 8px;
    border: 1px solid transparent;
    border-radius: 4px;
    color: {button_text};
    background: transparent;
}}

QToolBar QToolButton:hover {{
    background-color: {alternate};
    border-color: {border};
}}

QToolBar QToolButton:pressed {{
    background-color: {highlight};
    color: {highlight_text};
}}

QToolBar QToolButton:checked {{
    background-color: {alternate};
    border-color: {border};
}}

QToolBar QToolButton:disabled {{
    color: {disabled_text};
    background: transparent;
    border-color: transparent;
}}

/* ═══════════ 表格（Dolphin 视图风格）═══════════ */
QTableView, QTableWidget, QTreeView, QListView {{
    background-color: {base};
    alternate-background-color: {alternate};
    color: {text};
    border: none;
    gridline-color: transparent;
    selection-background-color: {highlight};
    selection-color: {highlight_text};
    outline: none;
}}

QTableView::item, QTableWidget::item, QTreeView::item, QListView::item {{
    padding: 4px 8px;
    border: none;
    min-height: 24px;
}}

QTableView::item:selected, QTableWidget::item:selected,
QTreeView::item:selected, QListView::item:selected {{
    background-color: {highlight};
    color: {highlight_text};
}}

QTableView::item:hover:!selected, QTreeView::item:hover:!selected,
QListView::item:hover:!selected {{
    background-color: {alternate};
}}

QHeaderView {{
    background-color: {window};
}}

QHeaderView::section {{
    background-color: {window};
    color: {window_text};
    padding: 5px 8px;
    border: none;
    border-bottom: 1px solid {border};
    border-right: 1px solid {border};
    font-weight: 600;
}}

QHeaderView::section:hover {{
    background-color: {alternate};
}}

QHeaderView::section:last {{
    border-right: none;
}}

QTableCornerButton::section {{
    background-color: {window};
    border: none;
    border-bottom: 1px solid {border};
}}

/* ═══════════ 按钮 ═══════════ */
QPushButton {{
    background-color: {button};
    color: {button_text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 14px;
    min-height: 18px;
}}

QPushButton:hover {{
    background-color: {alternate};
    border-color: {highlight};
}}

QPushButton:pressed {{
    background-color: {highlight};
    color: {highlight_text};
}}

QPushButton:default {{
    border-color: {highlight};
}}

QPushButton:disabled {{
    color: {disabled_text};
    border-color: {border};
}}

/* ═══════════ 输入框 ═══════════ */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {base};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 18px;
    selection-background-color: {highlight};
    selection-color: {highlight_text};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {highlight};
}}

QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled,
QSpinBox:disabled, QComboBox:disabled {{
    color: {disabled_text};
    background-color: {window};
}}

/* 图标由 addAction 提供，不需要额外左 padding */
QLineEdit#searchBox {{
    padding-left: 2px;
    padding-right: 4px;
    background-image: none;
}}

QComboBox::drop-down {{
    border: none;
    width: 18px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {window_text};
    width: 0;
    height: 0;
    margin-right: 6px;
}}

QComboBox QAbstractItemView {{
    background-color: {base};
    color: {text};
    border: 1px solid {border};
    selection-background-color: {highlight};
    selection-color: {highlight_text};
    outline: none;
}}

/* 忙碌遮罩（重启等耗时操作时覆盖整个对话框） */
QLabel#busyOverlay {{
    /* 用足够深的半透明黑，保证白字在任何主题下都清晰可读。
       90 太浅，浅色主题下白字几乎看不见。 */
    background-color: rgba(0, 0, 0, 150);
    color: #ffffff;
    font-size: 15px;
    font-weight: 500;
    padding: 24px;
}}

/* ═══════════ 标签页 ═══════════
 *
 * 这里**故意不写任何 QTabBar::tab 规则**。
 *
 * 原因：本应用跑在 KDE/Breeze 上，QStyle 已经是 Breeze。
 * Breeze 的 tab 有它自己的设计语言 ——
 *   · 选中项：白底 + 顶部 2px 高亮条 + 下边框与内容区相连
 *   · 未选中：灰底、无边框
 *   · hover：轻微提亮
 *
 * 一旦在 QSS 里覆写 background/border，Qt 就会**放弃 Breeze
 * 的原生绘制**，改用 QSS 的简化画法 —— 结果就是丢失顶部高亮条、
 * 灰色块显得突兀，整体和 Dolphin、系统设置等 KDE 应用完全不像。
 *
 * 所以：tab 的视觉交给 Breeze。这里只处理 Breeze 没有覆盖到的
 * 一点 —— 内容区不要额外边框（Breeze 默认给 pane 画了框，
 * 和 tab 的下边框会叠成双线）。
 */
QTabWidget::pane {{
    border: none;
    /* 内容区用 window 色（浅灰），不是 base（纯白）。
       这样内部的 GroupBox / 卡片（base 白）能浮在灰底上，
       层次分明 —— 和系统设置、Dolphin 的观感一致。
       用 base 的话白卡片贴白底，边界完全看不出来，显得「糊」。 */
    background-color: {window};
}}

/* 内容区与 tab 之间保留 Breeze 的那条分界线。
   Breeze 通过 QTabBar 的 bottom 线实现，这里不需要重复。 */

/* ═══════════ 滚动条（极细、低干扰）═══════════
   设计要点：
   - 槽位完全透明，不占视觉空间
   - 滑块默认半透明，hover 时加深
   - 宽度 8px，圆角 4px
*/
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
    border: none;
}}

QScrollBar::handle:vertical {{
    background: {sb_idle};
    min-height: 28px;
    border-radius: 4px;
    margin: 1px 1px;
}}

QScrollBar::handle:vertical:hover {{
    background: {sb_hover};
}}

QScrollBar::handle:vertical:pressed {{
    background: {highlight};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
    border: none;
    background: none;
}}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 0;
    border: none;
}}

QScrollBar::handle:horizontal {{
    background: {sb_idle};
    min-width: 28px;
    border-radius: 4px;
    margin: 1px 1px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {sb_hover};
}}

QScrollBar::handle:horizontal:pressed {{
    background: {highlight};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
    border: none;
    background: none;
}}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

/* ═══════════ 菜单 / 菜单栏 ═══════════ */
QMenuBar {{
    background-color: {window};
    color: {window_text};
    border-bottom: 1px solid {border};
    padding: 1px 2px;
}}

QMenuBar::item {{
    padding: 4px 10px;
    background: transparent;
    border-radius: 3px;
}}

QMenuBar::item:selected {{
    background-color: {alternate};
}}

QMenu {{
    background-color: {base};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px;
}}

QMenu::item {{
    padding: 5px 28px 5px 10px;
    border-radius: 3px;
}}

QMenu::item:selected {{
    background-color: {highlight};
    color: {highlight_text};
}}

QMenu::item:disabled {{
    color: {disabled_text};
}}

QMenu::separator {{
    height: 1px;
    background: {border};
    margin: 4px 8px;
}}

QMenu::icon {{
    padding-left: 6px;
}}

/* ═══════════ 分组框 ═══════════ */
/* margin-top 需 ≥ 标题行高的一半 + 余量，否则标题贴顶 */
QGroupBox {{
    border: 1px solid {border};
    border-radius: 4px;
    margin-top: 20px;
    padding: 8px 8px 8px 8px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 6px;
    top: 0px;
    padding: 0 4px;
    color: {button_text};
    background-color: transparent;
}}

/* ═══════════ 分割线 ═══════════ */
QSplitter::handle {{
    background-color: {border};
}}

QSplitter::handle:horizontal {{
    width: 1px;
}}

QSplitter::handle:vertical {{
    height: 1px;
}}

QFrame[frameShape="4"], QFrame[frameShape="5"] {{
    color: {border};
}}

/* ═══════════ 状态栏 ═══════════ */
QStatusBar {{
    background-color: {window};
    color: {window_text};
    border-top: 1px solid {border};
    padding: 1px 4px;
}}

QStatusBar::item {{
    border: none;
}}

QStatusBar QLabel {{
    padding: 0 4px;
}}

/* ═══════════ 提示框 ═══════════ */
/* 内边距给足，因为悬停提示含多行键值对与分隔线 */
QToolTip {{
    background-color: {base};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 6px 9px;
    margin: 0px;
}}

/* ═══════════ 进度条 ═══════════ */
QProgressBar {{
    background-color: {base};
    border: 1px solid {border};
    border-radius: 4px;
    text-align: center;
    color: {text};
    min-height: 18px;
}}

QProgressBar::chunk {{
    background-color: {highlight};
    border-radius: 3px;
}}

/* ═══════════ 复选框 / 单选框 ═══════════ */
QCheckBox, QRadioButton {{
    spacing: 6px;
    color: {window_text};
    background: transparent;
}}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {button_text};
    border-radius: 3px;
    background-color: {base};
}}

QRadioButton::indicator {{
    border-radius: 7px;
}}

QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {highlight};
}}

QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {highlight};
    border: 1px solid {highlight};
}}

QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
    background-color: {window};
    border-color: {border};
}}

/* ═══════════ 日志 / 终端视图 ═══════════ */
QPlainTextEdit#logView {{
    background-color: {base};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    font-family: "JetBrains Mono", "Maple Mono NF CN", "Noto Sans Mono", monospace;
    font-size: {fs['mono']};
    /* 左右 padding 收窄，让自绘滚动指示器贴近边缘 */
    padding: 6px 0px 6px 6px;
}}

/* ═══════════ 状态标签 ═══════════ */
QLabel#statusRunning {{
    color: #27ae60;
    font-weight: 600;
}}

QLabel#statusStopped {{
    color: {disabled_text};
}}

QLabel#statusError {{
    color: #e74c3c;
    font-weight: 600;
}}

QLabel#hintLabel {{
    color: {button_text};
    font-size: {fs['small']};
}}

/* 状态反馈：成功绿 / 失败红
   颜色取自 KDE 语义色（Breeze 的 positive/negative），
   暗色主题下也能保证对比度。 */
QLabel#okLabel {{
    color: {success};
    font-size: {fs['small']};
}}

QLabel#errLabel {{
    color: {danger};
    font-size: {fs['small']};
    font-weight: 500;
}}

/* 前置警示：红字但不用错误态那么重，读起来是「注意」不是「出错」 */
QLabel#warnLabel {{
    color: {danger};
    font-size: {fs['small']};
}}

/* 等宽：SSH 命令、路径这类要精确读的内容 */
QLabel#monoLabel {{
    font-family: monospace;
    color: {text};
}}

/* ═══════════ 悬停信息卡片 ═══════════ */
/* 直角（用户偏好：不要圆角不要阴影，正常平贴即可）*/
QFrame#vmInfoCard {{
    background-color: {base};
    border: 1px solid {border};
    border-radius: 0px;
}}

/* 卡片内复制按钮（命令后面的复制小图标）*/
QFrame#vmInfoCard QToolButton#cardCopyBtn {{
    background: transparent;
    border: none;
    border-radius: 0px;
    padding: 2px;
}}
QFrame#vmInfoCard QToolButton#cardCopyBtn:hover {{
    background-color: {alternate};
}}

/* 卡片内标题 */
QLabel#cardTitle {{
    font-weight: 600;
    font-size: {fs['base']};
}}

/* 卡片内分组小标签（SSH 连接 / 端口转发）*/
QLabel#cardSection {{
    color: {button_text};
    font-size: {fs['small']};
}}

/* 卡片内键名（系统 / 账户 / 初始密码）*/
QLabel#cardKey {{
    color: {button_text};
    font-size: {fs['small']};
}}

/* 卡片内等宽命令 */
QLabel#cardCmd {{
    font-family: monospace;
    font-size: {fs['small']};
}}

/* 卡片内弱提示（未运行）*/
QLabel#cardHint {{
    color: {button_text};
    font-size: {fs['small']};
    padding: 0 12px 10px 12px;
}}

/* 卡片内分隔线 */
QFrame#cardSep {{
    background-color: {border};
    max-height: 1px;
    border: none;
}}

/* ═══════════ 资源限制小卡片 ═══════════ */
QFrame#limitBox {{
    background-color: {alternate};
    border: 1px solid {border};
    border-radius: 6px;
}}

/* ── 阈值分段按钮（独立按钮，带间距）── */
QFrame#limitBox QPushButton[segGb] {{
    background: {base};
    border: 1px solid {border};
    border-radius: 5px;
    padding: 4px 16px;
    color: {button_text};
    font-size: {fs['small']};
    min-width: 48px;
}}

QFrame#limitBox QPushButton[segGb]:hover {{
    background: {alternate};
    border-color: {highlight};
}}

QFrame#limitBox QPushButton[segGb]:checked {{
    background: {highlight};
    border-color: {highlight};
    color: {highlight_text};
    font-weight: 600;
}}



/* ═══════════ 危险操作按钮（删除）═══════════ */
QToolBar QToolButton[aria-label] {{
    /* 占位，实际用下方属性选择器 */
}}

/* 删除按钮：图标红色（由 icon(danger=True) 渲染），文字保持常规色 */
QToolBar QToolButton#dangerDelete {{
    color: {button_text};
}}

QToolBar QToolButton#dangerDelete:hover {{
    background: rgba(231, 76, 60, 0.10);
    border-color: rgba(231, 76, 60, 0.45);
}}

QToolBar QToolButton#dangerDelete:pressed {{
    background: rgba(231, 76, 60, 0.22);
}}

QToolBar QToolButton#dangerDelete:disabled {{
    color: {disabled_text};
    background: transparent;
    border-color: transparent;
}}

/* 选中计数提示 */
QLabel#selectionHint {{
    color: {highlight};
    font-weight: 600;
    padding: 0 8px;
}}

/* ═══════════ 日志窗口 ═══════════ */
QFrame#filterBar {{
    background: {window};
    border-bottom: 1px solid {border};
}}

QFrame#filterBar QLineEdit#searchBox {{
    background: {base};
    border: 1px solid {border};
    border-radius: 4px;
}}

QFrame#logStatusBar {{
    background: {window};
    border-top: 1px solid {border};
}}

QFrame#logStatusBar QLabel {{
    padding: 2px 0;
}}

QDialog QPlainTextEdit#logView {{
    border: none;
    border-radius: 0;
    padding: 10px 6px 10px 12px;
    background: {base};
}}

/* ═══════════ 危险操作区 ═══════════ */
QGroupBox#dangerGroup {{
    border: 1px solid rgba(231, 76, 60, 0.45);
    border-radius: 4px;
    margin-top: 20px;
    padding: 8px 8px 8px 8px;
}}

QGroupBox#dangerGroup::title {{
    color: #e74c3c;
    font-weight: 600;
}}

QLabel#dangerHint {{
    color: #e74c3c;
    font-size: {fs['small']};
}}

QPushButton#dangerButton {{
    background: rgba(231, 76, 60, 0.10);
    border: 1px solid rgba(231, 76, 60, 0.55);
    color: {button_text};
    padding: 5px 16px;
}}

QPushButton#dangerButton:hover {{
    background: rgba(231, 76, 60, 0.20);
    border-color: #e74c3c;
}}

QPushButton#dangerButton:pressed {{
    background: #e74c3c;
    color: #ffffff;
}}

QPushButton#dangerButton:disabled {{
    background: transparent;
    border-color: {border};
    color: {disabled_text};
}}

/* ═══════════ 空状态 ═══════════ */
QWidget#emptyState {{
    background-color: {base};
}}

QLabel#emptyTitle {{
    font-size: {fs['base']};
    font-weight: 600;
    color: {window_text};
}}

QLabel#emptySubtitle {{
    font-size: {fs['small']};
    color: {button_text};
}}

QLabel#emptyIcon {{
    padding-bottom: 4px;
}}

/* ═══════════ 表单标签 ═══════════ */
QLabel#formLabel {{
    color: {button_text};
}}

/* ═══════════ 分隔器 ═══════════ */
QToolBox::tab {{
    background-color: {window};
    border-bottom: 1px solid {border};
    padding: 6px 10px;
}}
"""
