"""设置页

三个标签页：
1. 镜像加速 — registry mirror + 代理
2. 存储 — graphRoot 切换
3. 关于 — 版本信息
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor
from PyQt6.QtWidgets import (
    QGridLayout,
    QDoubleSpinBox,
    QSlider,
    QSizePolicy,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QFrame,
)

from core import podman_config as cfg
from core.podman_client import PodmanClient, PodmanError
from core.vm import manager
from resources.icons import ICON_SIZE_TOOLBAR, icon
from ui.style import semantic_colors


# ─────────────────────────────────────────
# KDE 风格布局构件
#
# KDE HIG 的做法是「加粗小节标题 + 留白 + 细线」分隔，
# 而不是给每块内容套一个带边框的 GroupBox ——
# 满屏方框会显得很重，而且边框和 Breeze 的细线质感冲突。
# ─────────────────────────────────────────
PAGE_MARGIN = 8         # 页面四周留白（贴近 tab 与窗口边）
SECTION_GAP = 10        # 卡片之间的间距
ROW_GAP = 8             # 行间距


def _page() -> tuple[QScrollArea, QVBoxLayout]:
    """标准设置页容器：滚动区 + 统一边距"""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)

    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
    v.setSpacing(SECTION_GAP)

    scroll.setWidget(w)
    return scroll, v


def _section(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    """一个卡片分组：浅底 + 细边框 + 圆角，标题在卡片内

    参考 KDE 系统设置的分组样式。刻意不加阴影 ——
    平铺的设置页里加阴影只会显得脏。
    """
    card = QFrame()
    card.setObjectName("card")
    v = QVBoxLayout(card)
    v.setContentsMargins(14, 12, 14, 12)
    v.setSpacing(8)

    if title:
        head = QHBoxLayout()
        head.setSpacing(6)
        lbl = QLabel(title)
        lbl.setObjectName("cardTitle")
        head.addWidget(lbl)
        head.addStretch()
        v.addLayout(head)
        card._head = head          # 允许调用方往标题行右侧加东西

    return card, v


def _info_box(text: str) -> QFrame:
    """浅底提示框（用于较长说明）"""
    box = QFrame()
    box.setObjectName("infoBox")
    v = QVBoxLayout(box)
    v.setContentsMargins(12, 8, 12, 8)
    lbl = QLabel(text)
    lbl.setObjectName("infoText")
    lbl.setWordWrap(True)
    v.addWidget(lbl)
    return box


def _form() -> QFormLayout:
    """标准表单：标签右对齐（KDE 惯例）"""
    f = QFormLayout()
    f.setContentsMargins(0, 0, 0, 0)
    f.setHorizontalSpacing(14)
    f.setVerticalSpacing(ROW_GAP)
    f.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    f.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
    )
    return f


def _help(text: str) -> QLabel:
    """控件下方的灰色说明"""
    lbl = QLabel(text)
    lbl.setObjectName("hintLabel")
    lbl.setWordWrap(True)
    return lbl


def _kv(label: str, mono: bool = False) -> tuple[QWidget, QLabel]:
    """只读「标签 + 值」一行（值可选中复制）"""
    row = QWidget()
    h = QHBoxLayout(row)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(14)

    k = QLabel(label)
    k.setObjectName("formLabel")
    k.setFixedWidth(96)
    k.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
    h.addWidget(k)

    v = QLabel("—")
    v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    v.setWordWrap(True)
    v.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    if mono:
        v.setObjectName("monoValue")
    h.addWidget(v, 1)
    return row, v


class SettingsPage(QWidget):
    status_message = pyqtSignal(str)

    def __init__(self, client: PodmanClient, parent: QWidget | None = None):
        super().__init__(parent)
        self.client = client
        self._tasks: list = []   # 后台任务句柄，防止被 GC
        self._build_ui()
        self.load_config()

        # 内存占用每秒刷新（仅当停留在服务标签页时）
        self._mem_timer = QTimer(self)
        self._mem_timer.setInterval(1000)
        self._mem_timer.timeout.connect(self._tick_memory)
        self._mem_timer.start()

        # 切换标签时启停刷新
        self.tabs.currentChanged.connect(self._on_tab_changed)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        # 尽量少的边距：设置页内容要贴近 tab 栏和窗口左边缘
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        # 不要圆角/边框的 pane，tab 栏本身由 Breeze 绘制
        self.tabs.setDocumentMode(False)
        # 顺序按「用户多久用一次」排：
        #   服务（出问题才来）→ 资源（偶尔调）→ 镜像加速（装完就忘）
        #   → 存储（基本不动）→ 关于（纯查看）
        self.tabs.addTab(self._build_service_tab(), "服务")
        self.tabs.addTab(self._build_resources_tab(), "资源")
        self.tabs.addTab(self._build_mirror_tab(), "镜像")
        self.tabs.addTab(self._build_storage_tab(), "存储")
        self.tabs.addTab(self._build_about_tab(), "关于")
        layout.addWidget(self.tabs)

    # ── 镜像加速 ──
    def _build_mirror_tab(self) -> QWidget:
        """镜像加速：改完即生效，不需要点保存

        地址写在 registries.conf，每次拉镜像都会重新读，
        所以改了就落盘。
        """
        scroll, v = _page()

        # ══ 状态 ══
        self.lbl_mirror_status = QLabel("")
        self.lbl_mirror_status.setObjectName("mirrorStatus")
        v.addWidget(self.lbl_mirror_status)

        # ══ 加速地址 ══
        sec, sv = _section("加速地址")

        body = QHBoxLayout()
        body.setSpacing(6)

        self.lst_mirrors = QListWidget()
        self.lst_mirrors.setMinimumHeight(132)
        self.lst_mirrors.setMaximumHeight(190)
        self.lst_mirrors.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        self.lst_mirrors.itemSelectionChanged.connect(
            self._update_mirror_buttons
        )
        body.addWidget(self.lst_mirrors, 1)

        btncol = QVBoxLayout()
        btncol.setSpacing(4)

        def icon_btn(ic_name, tip, slot):
            b = QPushButton()
            b.setIcon(icon(ic_name))
            b.setFixedSize(32, 32)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            btncol.addWidget(b)
            return b

        self.btn_mirror_up = icon_btn(
            "up", "上移（优先级更高）", lambda: self._move_mirror(-1)
        )
        self.btn_mirror_down = icon_btn(
            "down", "下移", lambda: self._move_mirror(1)
        )
        btncol.addSpacing(6)
        self.btn_mirror_del = icon_btn(
            "delete", "移除选中的地址", self._del_mirror
        )
        btncol.addStretch()

        body.addLayout(btncol)
        sv.addLayout(body)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItem("从常用地址选择…", "")
        for name, url in cfg.MIRROR_PRESETS:
            self.cmb_preset.addItem(name, url)
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_selected)
        add_row.addWidget(self.cmb_preset, 1)

        btn_add = QPushButton(icon("add"), "添加")
        btn_add.setToolTip("添加自定义地址")
        btn_add.clicked.connect(self._add_mirror)
        add_row.addWidget(btn_add)

        sv.addLayout(add_row)
        sv.addWidget(_help(
            "按从上到下的顺序尝试，前一个失败自动降级到下一个。"
            "未配置时直连官方仓库。"
        ))

        v.addWidget(sec)

        # ══ 代理（默认折叠）══
        self.btn_mirror_adv = QPushButton("显示代理设置")
        self.btn_mirror_adv.setCheckable(True)
        self.btn_mirror_adv.setFlat(True)
        self.btn_mirror_adv.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mirror_adv.toggled.connect(self._toggle_mirror_advanced)
        adv_bar = QHBoxLayout()
        adv_bar.setContentsMargins(0, 0, 0, 0)
        adv_bar.addWidget(self.btn_mirror_adv)
        adv_bar.addStretch()
        v.addLayout(adv_bar)

        self.box_proxy, pv = _section("HTTP 代理")

        self.chk_proxy = QCheckBox("启用代理")
        self.chk_proxy.toggled.connect(self._on_proxy_toggled)
        pv.addWidget(self.chk_proxy)

        f = _form()

        self.cmb_proxy_preset = QComboBox()
        self.cmb_proxy_preset.addItem("从常用地址选择…", "")
        for name, url in cfg.PROXY_PRESETS:
            self.cmb_proxy_preset.addItem(name, url)
        self.cmb_proxy_preset.currentIndexChanged.connect(
            self._on_proxy_preset_selected
        )
        f.addRow("常用地址", self.cmb_proxy_preset)

        self.ed_proxy = QLineEdit()
        self.ed_proxy.setPlaceholderText("http://127.0.0.1:7890")
        self.ed_proxy.editingFinished.connect(self._autosave_mirrors)
        f.addRow("代理地址", self.ed_proxy)

        self.ed_noproxy = QLineEdit()
        self.ed_noproxy.setPlaceholderText("localhost,127.0.0.1,10.88.0.0/16")
        self.ed_noproxy.editingFinished.connect(self._autosave_mirrors)
        f.addRow("排除地址", self.ed_noproxy)

        pv.addLayout(f)
        pv.addWidget(_help("访问境外仓库时使用，国内加速地址一般不需要。"))

        self.box_proxy.hide()
        v.addWidget(self.box_proxy)

        # ══ 清除 ══
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addStretch()
        btn_clear = QPushButton("清除全部加速配置")
        btn_clear.clicked.connect(self._clear_all)
        bar.addWidget(btn_clear)
        v.addLayout(bar)

        v.addStretch()
        return scroll

    def _toggle_mirror_advanced(self, on: bool) -> None:
        self.box_proxy.setVisible(on)
        self.btn_mirror_adv.setText(
            "隐藏代理设置" if on else "显示代理设置"
        )

    def _autosave_mirrors(self) -> None:
        """把当前界面状态写入 registries.conf（静默，失败才提示）"""
        mirrors = []
        for i in range(self.lst_mirrors.count()):
            it = self.lst_mirrors.item(i)
            if not (it.flags() & Qt.ItemFlag.ItemIsEnabled):
                continue
            mirrors.append(it.text())

        try:
            if mirrors:
                ok, msg = cfg.safe_write_config(
                    "registries", {"docker.io": mirrors}
                )
                if not ok:
                    QMessageBox.warning(self, "保存失败", msg)
                    return
            else:
                cfg.clear_registries()

            if self.chk_proxy.isChecked() and self.ed_proxy.text().strip():
                p = self.ed_proxy.text().strip()
                ok, msg = cfg.safe_write_config(
                    "proxy", p, p, self.ed_noproxy.text().strip()
                )
                if not ok:
                    QMessageBox.warning(self, "保存失败", msg)
                    return
            else:
                cfg.clear_proxy()
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"写入配置出错：\n{e}")
            return

        self._update_mirror_status(len(mirrors))
        self.status_message.emit("镜像加速配置已保存")

    def _update_mirror_status(self, count: int | None = None) -> None:
        if count is None:
            count = 0
            for i in range(self.lst_mirrors.count()):
                it = self.lst_mirrors.item(i)
                if it.flags() & Qt.ItemFlag.ItemIsEnabled:
                    count += 1
        if count:
            self.lbl_mirror_status.setText(f"已配置 {count} 个加速地址 · 改动即时生效")
        else:
            self.lbl_mirror_status.setText("当前直连官方仓库（未使用加速）")

    # ── 存储 ──
    def _build_storage_tab(self) -> QWidget:
        """存储：位置 + 占用 + 危险操作

        只保留「换盘」和「重置」两个真实需求；
        驱动/运行时属于诊断信息，放进关于页即可。
        """
        scroll, v = _page()

        # ══ 当前位置 ══
        sec, sv = _section("位置")

        row_root, self.st_root = _kv("数据目录", mono=True)
        sv.addWidget(row_root)

        row_disk, self.lbl_disk = _kv("磁盘")
        sv.addWidget(row_disk)

        row_used, self.st_used = _kv("已用")
        sv.addWidget(row_used)


        f = _form()
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        self.ed_graphroot = QLineEdit()
        self.ed_graphroot.setPlaceholderText(
            "留空使用默认（~/.local/share/containers/storage）"
        )
        dir_row.addWidget(self.ed_graphroot, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_graphroot)
        dir_row.addWidget(btn_browse)
        f.addRow("新目录", dir_row)
        sv.addLayout(f)

        sv.addWidget(_help(
            "更改目录后需要迁移已有数据，期间所有容器必须停止。"
            "数据量大时可能耗时较久。"
        ))

        act = QHBoxLayout()
        act.setSpacing(6)
        act.addStretch()

        btn_migrate = QPushButton("迁移")
        btn_migrate.setToolTip("把现有数据搬到新目录（podman system migrate）")
        btn_migrate.clicked.connect(self._run_migrate)
        act.addWidget(btn_migrate)

        btn_save = QPushButton("应用")
        btn_save.clicked.connect(self._save_storage_config)
        act.addWidget(btn_save)

        sv.addLayout(act)
        v.addWidget(sec)

        # ══ 虚拟机目录 ══
        sec3, s3 = _section("虚拟机目录")

        row_vm, self.st_vm_root = _kv("目录", mono=True)
        s3.addWidget(row_vm)

        row_vm_disk, self.lbl_vm_disk = _kv("磁盘")
        s3.addWidget(row_vm_disk)

        row_vm_used, self.lbl_vm_used = _kv("占用")
        s3.addWidget(row_vm_used)


        f2 = _form()
        vm_row = QHBoxLayout()
        vm_row.setSpacing(6)
        self.ed_vm_root = QLineEdit()
        self.ed_vm_root.setPlaceholderText(
            str(manager.DEFAULT_VM_ROOT)
        )
        vm_row.addWidget(self.ed_vm_root, 1)
        btn_vm_browse = QPushButton("浏览…")
        btn_vm_browse.clicked.connect(self._browse_vm_root)
        vm_row.addWidget(btn_vm_browse)
        f2.addRow("新目录", vm_row)
        s3.addLayout(f2)

        s3.addWidget(_help(
            "虚拟机磁盘、内存快照都放在这里，占空间较大。"
            "更改后现有虚拟机会一并搬过去（需先停止所有虚拟机）。"
        ))

        vm_act = QHBoxLayout()
        vm_act.setSpacing(6)
        vm_act.addStretch()

        btn_vm_reset = QPushButton("恢复默认")
        btn_vm_reset.clicked.connect(self._reset_vm_root)
        vm_act.addWidget(btn_vm_reset)

        btn_vm_apply = QPushButton("应用")
        btn_vm_apply.clicked.connect(self._apply_vm_root)
        vm_act.addWidget(btn_vm_apply)

        s3.addLayout(vm_act)
        v.addWidget(sec3)

        # ══ 危险区 ══
        sec2, sv2 = _section("重置")

        sv2.addWidget(_help(
            "恢复到首次安装状态：删除全部容器、镜像、卷、网络，"
            "并清除代理与加速配置。宿主机目录（如 /data）里的数据不受影响。"
        ))

        self.lbl_reset_preview = QLabel("")
        self.lbl_reset_preview.setObjectName("hintLabel")
        self.lbl_reset_preview.setWordWrap(True)
        sv2.addWidget(self.lbl_reset_preview)

        reset_bar = QHBoxLayout()
        reset_bar.setSpacing(6)
        reset_bar.addStretch()
        self.btn_reset = QPushButton("重置全部数据…")
        self.btn_reset.setObjectName("dangerButton")
        self.btn_reset.clicked.connect(self._run_full_reset)
        reset_bar.addWidget(self.btn_reset)
        sv2.addLayout(reset_bar)

        v.addWidget(sec2)

        v.addStretch()

        QTimer.singleShot(120, self._refresh_storage_status)
        return scroll

    def _refresh_vm_root_info(self) -> None:
        """刷新虚拟机目录的路径 / 磁盘 / 占用"""
        root = manager.vm_root()
        self.st_vm_root.setText(str(root))

        free, total = cfg.disk_free(str(root)) if root.exists() else (0, 0)
        self.lbl_vm_disk.setText(
            f"{free / 1024**3:.1f} GB 可用 / {total / 1024**3:.1f} GB 总计"
            if total else "—"
        )

        size = 0
        if root.exists():
            for f in root.rglob("*"):
                try:
                    if f.is_file():
                        size += f.stat().st_size
                except OSError:
                    pass
        self.lbl_vm_used.setText(f"{size / 1024**3:.2f} GB")

    def _browse_vm_root(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择虚拟机目录",
            self.ed_vm_root.text().strip() or str(manager.DEFAULT_VM_ROOT),
            QFileDialog.Option.DontUseNativeDialog,
        )
        if d:
            self.ed_vm_root.setText(d)

    def _apply_vm_root(self) -> None:
        """应用虚拟机目录（含数据搬迁）"""
        target = self.ed_vm_root.text().strip()
        if not target:
            QMessageBox.information(
                self, "请选择目录", "请先选择一个新的虚拟机目录。"
            )
            return

        current = manager.vm_root()
        if str(Path(target).expanduser()) == str(current):
            QMessageBox.information(
                self, "无需更改", "新目录与当前目录相同。"
            )
            return

        running = [v.spec.name for v in manager.load_all() if v.running]
        if running:
            QMessageBox.warning(
                self, "有虚拟机在运行",
                "请先停止这些虚拟机再更改目录：\n  "
                + "\n  ".join(running),
            )
            return

        has_data = any(
            (current / sub).is_dir() and any((current / sub).iterdir())
            for sub in ("machines", "shared")
        )
        msg = f"将虚拟机目录更改为：\n{target}\n\n"
        msg += ("现有虚拟机会被移动到新目录。\n\n" if has_data
                else "")
        msg += "确定继续？"
        if QMessageBox.question(
            self, "更改虚拟机目录", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        ok, msg2 = manager.set_vm_root(target, move=True)
        if ok:
            self.status_message.emit(f"虚拟机目录已更改为 {msg2}")
            self.ed_vm_root.clear()
            self._refresh_storage_status()
        else:
            QMessageBox.critical(self, "更改失败", msg2)

    def _reset_vm_root(self) -> None:
        """恢复默认目录"""
        current = manager.vm_root()
        if current == manager.DEFAULT_VM_ROOT:
            QMessageBox.information(self, "已是默认", "当前就在默认目录。")
            return
        self.ed_vm_root.setText(str(manager.DEFAULT_VM_ROOT))
        self._apply_vm_root()

    def _refresh_storage_status(self) -> None:
        """刷新存储状态（只读展示 + 重置预览）"""
        try:
            info = self.client.system_info()
            store = info.get("store") or {}
            root = store.get("graphRoot", "—")
            self.st_root.setText(root)

            free, total = cfg.disk_free(root) if root != "—" else (0, 0)
            if total:
                self.lbl_disk.setText(
                    f"{free / 1024**3:.1f} GB 可用 / {total / 1024**3:.1f} GB 总计"
                )
            else:
                self.lbl_disk.setText("—")

            df = self.client.system_df()
            imgs = df.get("Images") or []
            ctrs = df.get("Containers") or []
            vols = df.get("Volumes") or []
            used = (
                sum(x.get("Size", 0) or 0 for x in imgs)
                + sum(x.get("SizeRw", 0) or 0 for x in ctrs)
                + sum((x.get("UsageData") or {}).get("Size", 0) or 0 for x in vols)
            )
            self.st_used.setText(f"{used / 1024**3:.2f} GB")

            self._reset_stats = (len(ctrs), len(imgs), len(vols), used)
            self.lbl_reset_preview.setText(
                f"当前将删除：{len(ctrs)} 个容器 · {len(imgs)} 个镜像 · "
                f"{len(vols)} 个卷 · 约 {used / 1024**3:.2f} GB"
            )
        except Exception as e:
            self.st_root.setText(f"读取失败: {e}")

        # 虚拟机目录与 podman 连接无关，独立刷新
        self._refresh_vm_root_info()

    def _run_full_reset(self) -> None:
        """完全重置：三重确认"""
        r1 = QMessageBox.warning(
            self, "重置 Podman",
            "<b>这会把 Podman 恢复到首次安装的状态。</b><br><br>"
            "将被删除：<br>"
            "&nbsp;&nbsp;· 所有容器（含运行中的）<br>"
            "&nbsp;&nbsp;· 所有镜像<br>"
            "&nbsp;&nbsp;· 所有卷<br>"
            "&nbsp;&nbsp;· 所有自定义网络<br>"
            "&nbsp;&nbsp;· 存储驱动和目录配置<br>"
            "&nbsp;&nbsp;· 本程序写入的代理和加速配置<br><br>"
            "<b style='color:#e74c3c'>此操作不可恢复。</b><br><br>"
            "确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r1 != QMessageBox.StandardButton.Yes:
            return

        from PyQt6.QtWidgets import QInputDialog, QLineEdit

        text, ok = QInputDialog.getText(
            self, "再次确认",
            "请输入 RESET（全大写）以确认执行：",
            QLineEdit.EchoMode.Normal, "",
        )
        if not ok or text.strip() != "RESET":
            self.status_message.emit("已取消重置")
            return

        r3 = QMessageBox.critical(
            self, "最后确认",
            "这是最后一次确认。\n\n"
            "执行后所有容器、镜像、卷将被永久删除，\n"
            "Podman 恢复到初始状态。\n\n"
            "真的要继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r3 != QMessageBox.StandardButton.Yes:
            self.status_message.emit("已取消重置")
            return

        self._do_full_reset()

    def _do_full_reset(self) -> None:
        """后台执行 podman system reset"""
        import subprocess

        self.btn_reset.setEnabled(False)
        self.status_message.emit("正在重置 Podman…（可能需要几分钟）")

        def work():
            r = subprocess.run(
                ["podman", "system", "reset", "--force"],
                capture_output=True, text=True, timeout=600,
            )
            reset_ok = r.returncode == 0
            reset_out = (r.stdout or "") + (r.stderr or "")

            try:
                cfg.clear_registries()
                cfg.clear_proxy()
                cfg.clear_storage()
                cleared = True
            except Exception:
                cleared = False

            ok_restart, msg_restart = cfg.restart_and_verify(timeout=25)

            return {
                "reset_ok": reset_ok,
                "reset_out": reset_out.strip(),
                "cleared": cleared,
                "restart_ok": ok_restart,
                "restart_msg": msg_restart,
            }

        def done(res):
            self.btn_reset.setEnabled(True)

            if not res["reset_ok"]:
                QMessageBox.critical(
                    self, "重置失败",
                    f"podman system reset 执行失败：\n\n"
                    f"{res['reset_out'][:1000]}",
                )
                self.status_message.emit("重置失败")
                return

            lines = ["Podman 已重置到初始状态。", ""]
            lines.append("✓ 所有容器、镜像、卷、网络已删除")
            if res["cleared"]:
                lines.append("✓ 代理、镜像加速、存储配置已清除")
            if res["restart_ok"]:
                lines.append("✓ 服务已重启")
            else:
                lines.append(f"⚠ 服务重启失败：{res['restart_msg']}")

            QMessageBox.information(self, "重置完成", "\n".join(lines))

            from core.notify import notifier
            notifier().send(
                "Podman 已重置",
                "所有容器、镜像、卷已删除，配置已恢复默认",
                icon_name="clean",
            )

            self.status_message.emit("Podman 已重置")
            self.load_config()
            self._refresh_storage_status()
            self._refresh_service()

        def error(msg):
            self.btn_reset.setEnabled(True)
            QMessageBox.critical(self, "重置失败", msg)
            self.status_message.emit(f"重置失败: {msg}")

        from core.async_task import run_async
        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks = [t for t in getattr(self, "_tasks", []) if t.alive]
        self._tasks.append(task)

    # ── 关于 ──
    def _build_about_tab(self) -> QWidget:
        """关于：应用信息 + Podman 运行环境（原「概览」并入）"""
        scroll, v = _page()

        from core.config import APP_NAME, APP_VERSION

        # ══ 头部 ══
        head = QHBoxLayout()
        head.setSpacing(14)

        from resources.icons import app_icon

        ic = QLabel()
        ic.setPixmap(app_icon().pixmap(56, 56))
        head.addWidget(ic)

        txt = QVBoxLayout()
        txt.setSpacing(2)
        name = QLabel(APP_NAME)
        name.setObjectName("aboutTitle")
        txt.addWidget(name)
        sub = QLabel(f"版本 {APP_VERSION} · KDE Plasma 原生 Podman 管理工具")
        sub.setObjectName("hintLabel")
        txt.addWidget(sub)
        head.addLayout(txt)
        head.addStretch()

        v.addLayout(head)

        # ══ 运行环境 ══
        sec1, s1 = _section("运行环境")
        r, self._ov_version = _kv("版本")
        s1.addWidget(r)
        r, self._ov_api = _kv("API 版本")
        s1.addWidget(r)
        r, self._ov_rootless = _kv("Rootless")
        s1.addWidget(r)
        r, self._ov_cgroup = _kv("Cgroup")
        s1.addWidget(r)
        r, self._ov_kernel = _kv("内核")
        s1.addWidget(r)
        r, self._ov_platform = _kv("平台")
        s1.addWidget(r)
        v.addWidget(sec1)

        # ══ 存储 ══
        sec2, s2 = _section("存储")
        r, self._ov_driver = _kv("驱动")
        s2.addWidget(r)
        r, self._ov_root = _kv("数据目录", mono=True)
        s2.addWidget(r)
        r, self._ov_socket = _kv("Socket", mono=True)
        s2.addWidget(r)
        v.addWidget(sec2)

        # ══ 占用 ══
        sec3, s3 = _section("占用统计")
        r, self._ov_images = _kv("镜像")
        s3.addWidget(r)
        r, self._ov_containers = _kv("容器")
        s3.addWidget(r)
        r, self._ov_volumes = _kv("卷")
        s3.addWidget(r)
        v.addWidget(sec3)

        # ══ 配置文件 ══
        sec4, s4 = _section("配置文件")
        for fname in ("registries.conf", "containers.conf", "storage.conf"):
            r, val = _kv(fname, mono=True)
            val.setText(str(cfg.CONFIG_DIR / fname))
            s4.addWidget(r)
        v.addWidget(sec4)

        # ══ 底部动作 ══
        bar = QHBoxLayout()
        bar.setSpacing(6)

        btn_open = QPushButton(icon("folder"), "打开配置目录")
        btn_open.clicked.connect(self._open_config_dir)
        bar.addWidget(btn_open)

        btn_copy = QPushButton(icon("copy"), "复制诊断信息")
        btn_copy.setToolTip("复制版本与运行环境到剪贴板，方便反馈问题")
        btn_copy.clicked.connect(self._copy_diagnostics)
        bar.addWidget(btn_copy)

        bar.addStretch()
        v.addLayout(bar)
        v.addStretch()

        QTimer.singleShot(120, self._load_about)
        return scroll

    def _open_config_dir(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        cfg._ensure_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(cfg.CONFIG_DIR)))

    def _copy_diagnostics(self) -> None:
        from PyQt6.QtGui import QGuiApplication

        lines = [
            f"Podman: {self._ov_version.text()}",
            f"API: {self._ov_api.text()}",
            f"Rootless: {self._ov_rootless.text()}",
            f"Cgroup: {self._ov_cgroup.text()}",
            f"内核: {self._ov_kernel.text()}",
            f"平台: {self._ov_platform.text()}",
            f"驱动: {self._ov_driver.text()}",
            f"数据目录: {self._ov_root.text()}",
            f"Socket: {self._ov_socket.text()}",
        ]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self.status_message.emit("诊断信息已复制到剪贴板")

    def _load_about(self) -> None:
        """异步加载 Podman 运行环境信息"""
        from core.async_task import run_async
        from ui.utils import fmt_size

        def work():
            out = {}
            try:
                out["version"] = self.client.version()
            except Exception as e:
                out["version_error"] = str(e)
            try:
                out["info"] = self.client.system_info()
            except Exception as e:
                out["info_error"] = str(e)
            try:
                out["df"] = self.client.system_df()
            except Exception as e:
                out["df_error"] = str(e)
            return out

        def done(d):
            v = d.get("version")
            if v:
                comps = v.get("Components", [])
                eng = next(
                    (x for x in comps if x.get("Name") == "Podman Engine"), {}
                )
                self._ov_version.setText(eng.get("Version", "—"))
                self._ov_api.setText(
                    str(eng.get("Details", {}).get("APIVersion", "—"))
                )
                self._ov_platform.setText(
                    v.get("Platform", {}).get("Name", "—")
                )
            elif "version_error" in d:
                self._ov_version.setText("未连接")

            info = d.get("info")
            if info:
                store = info.get("store") or {}
                host = info.get("host") or {}
                sec = host.get("security") or {}
                self._ov_driver.setText(store.get("graphDriverName", "—"))
                self._ov_root.setText(store.get("graphRoot", "—"))
                self._ov_socket.setText(self.client.socket_path or "—")
                self._ov_rootless.setText(
                    "是" if sec.get("rootless") else "否"
                )
                self._ov_cgroup.setText(host.get("cgroupVersion", "—"))
                self._ov_kernel.setText(host.get("kernel", "—"))
            elif "info_error" in d:
                self._ov_rootless.setText("—")

            df = d.get("df")
            if df:
                imgs = df.get("Images") or []
                ctrs = df.get("Containers") or []
                vols = df.get("Volumes") or []
                self._ov_images.setText(
                    f"{len(imgs)} 个 · "
                    f"{fmt_size(sum(x.get('Size', 0) or 0 for x in imgs))}"
                )
                self._ov_containers.setText(
                    f"{len(ctrs)} 个 · "
                    f"{fmt_size(sum(x.get('SizeRw', 0) or 0 for x in ctrs))}"
                )
                self._ov_volumes.setText(
                    f"{len(vols)} 个 · "
                    f"{fmt_size(sum((x.get('UsageData') or {}).get('Size', 0) or 0 for x in vols))}"
                )

        run_async(work, on_done=done, on_error=lambda m: None, parent=self)

    # ── 配置加载 ──
    def load_config(self) -> None:
        # 镜像加速
        reg = cfg.read_registries()
        self.lst_mirrors.clear()
        count = 0
        for registry, urls in reg["mirrors"].items():
            for url in urls:
                item = QListWidgetItem(icon("network"), url)
                self.lst_mirrors.addItem(item)
                count += 1

        if count == 0:
            ph = QListWidgetItem(
                "尚未配置加速地址 — 从下方选择预设，或点「添加」手动输入"
            )
            ph.setFlags(Qt.ItemFlag.NoItemFlags)
            ph.setForeground(QColor("#95a5a6"))
            self.lst_mirrors.addItem(ph)

        if hasattr(self, "lbl_mirror_status"):
            self._update_mirror_status(count)
        self._update_mirror_buttons()

        # 代理
        proxy = cfg.read_proxy()
        hp = proxy.get("http_proxy", "")
        self.chk_proxy.setChecked(bool(hp))
        self.ed_proxy.setText(hp)
        self.ed_noproxy.setText(
            proxy.get("no_proxy", "localhost,127.0.0.1,10.88.0.0/16")
        )
        self._on_proxy_toggled(bool(hp))

        # 存储（只编辑数据目录，驱动/运行时属于诊断信息，放关于页）
        st = cfg.read_storage()
        self.ed_graphroot.setText(st.get("graphroot", ""))
        self._update_disk_info()

    def _update_disk_info(self) -> None:
        path = self.ed_graphroot.text().strip()
        if not path:
            path = str(Path.home() / ".local" / "share" / "containers")
        if not os.path.isdir(path):
            self.lbl_disk.setText(f"{path}  (目录不存在)")
            return
        free, total = cfg.disk_free(path)
        if total:
            self.lbl_disk.setText(
                f"{free / 1024**3:.1f} GB 可用 / {total / 1024**3:.1f} GB 总计"
            )
        else:
            self.lbl_disk.setText("无法读取")

    # ── 镜像加速操作 ──
    def _update_mirror_buttons(self) -> None:
        if not hasattr(self, "btn_mirror_up"):
            return
        row = self.lst_mirrors.currentRow()
        count = self.lst_mirrors.count()
        has = row >= 0
        self.btn_mirror_up.setEnabled(has and row > 0)
        self.btn_mirror_down.setEnabled(has and row < count - 1)
        self.btn_mirror_del.setEnabled(has)

    def _remove_placeholder(self) -> None:
        for i in range(self.lst_mirrors.count()):
            it = self.lst_mirrors.item(i)
            if not (it.flags() & Qt.ItemFlag.ItemIsEnabled):
                self.lst_mirrors.takeItem(i)
                return

    def _ensure_placeholder(self) -> None:
        if self.lst_mirrors.count() == 0:
            ph = QListWidgetItem(
                "尚未配置加速地址 — 从下方选择预设，或点「添加」手动输入"
            )
            ph.setFlags(Qt.ItemFlag.NoItemFlags)
            ph.setForeground(QColor("#95a5a6"))
            self.lst_mirrors.addItem(ph)

    def _refresh_mirror_status(self) -> None:
        if not hasattr(self, "lbl_mirror_status"):
            return
        count = sum(
            1 for i in range(self.lst_mirrors.count())
            if self.lst_mirrors.item(i).flags() & Qt.ItemFlag.ItemIsEnabled
        )
        self.lbl_mirror_status.setText(
            f"已配置 {count} 个加速地址" if count
            else "当前直连官方仓库（未使用加速）"
        )

    def _on_preset_selected(self, idx: int) -> None:
        url = self.cmb_preset.itemData(idx)
        if not url:
            return
        if "<你的ID>" in url:
            QMessageBox.information(
                self, "需要替换 ID",
                "阿里云加速地址需要你自己的专属 ID。\n\n"
                "请到 https://cr.console.aliyun.com/ 获取后手动填写。",
            )
            self.cmb_preset.setCurrentIndex(0)
            return
        for i in range(self.lst_mirrors.count()):
            if self.lst_mirrors.item(i).text() == url:
                self.status_message.emit("该地址已在列表中")
                self.lst_mirrors.setCurrentRow(i)
                self.cmb_preset.setCurrentIndex(0)
                return
        self._remove_placeholder()
        item = QListWidgetItem(icon("network"), url)
        self.lst_mirrors.addItem(item)
        self.lst_mirrors.setCurrentItem(item)
        self.cmb_preset.setCurrentIndex(0)
        self._refresh_mirror_status()
        self.status_message.emit(f"已添加 {url}")
        self._autosave_mirrors()


    def _add_mirror(self) -> None:
        from ui.widgets import ask_text

        url, ok = ask_text(
            self, "添加加速地址",
            "镜像加速地址（需包含 https:// 前缀）:",
            default="https://",
            hint="示例：https://docker.m.daocloud.io",
            width=680, height=190,
        )
        if not ok or not url:
            return
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(
                self, "格式错误",
                f"地址必须以 http:// 或 https:// 开头\n\n当前输入：{url}",
            )
            return
        if url in ("https://", "http://"):
            QMessageBox.warning(self, "地址不完整", "请输入完整的加速地址。")
            return

        for i in range(self.lst_mirrors.count()):
            if self.lst_mirrors.item(i).text() == url:
                self.status_message.emit("该地址已在列表中")
                self.lst_mirrors.setCurrentRow(i)
                return

        self._remove_placeholder()
        item = QListWidgetItem(icon("network"), url)
        self.lst_mirrors.addItem(item)
        self.lst_mirrors.setCurrentItem(item)
        self._refresh_mirror_status()
        self.status_message.emit(f"已添加 {url}")
        self._autosave_mirrors()


    def _del_mirror(self) -> None:
        row = self.lst_mirrors.currentRow()
        if row < 0:
            return
        item = self.lst_mirrors.item(row)
        if not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        text = item.text()
        self.lst_mirrors.takeItem(row)
        self._ensure_placeholder()
        self._refresh_mirror_status()
        self._update_mirror_buttons()
        self.status_message.emit(f"已移除 {text}")
        self._autosave_mirrors()


    def _move_mirror(self, delta: int) -> None:
        row = self.lst_mirrors.currentRow()
        new = row + delta
        if row < 0 or new < 0 or new >= self.lst_mirrors.count():
            return
        item = self.lst_mirrors.takeItem(row)
        self.lst_mirrors.insertItem(new, item)
        self.lst_mirrors.setCurrentRow(new)
        self._update_mirror_buttons()
        self._autosave_mirrors()


    def _on_proxy_toggled(self, on: bool) -> None:
        self.ed_proxy.setEnabled(on)
        self.ed_noproxy.setEnabled(on)
        self.cmb_proxy_preset.setEnabled(on)
        self._autosave_mirrors()


    def _on_proxy_preset_selected(self, idx: int) -> None:
        url = self.cmb_proxy_preset.itemData(idx)
        if url:
            self.ed_proxy.setText(url)
        self._autosave_mirrors()


    def _clear_all(self) -> None:
        r = QMessageBox.question(
            self, "清除配置",
            "将删除本应用生成的 registries.conf 和 containers.conf。\n"
            "（会先备份为 .bak）\n\n确定继续？",
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        cfg.clear_registries()
        cfg.clear_proxy()
        self.load_config()
        self._update_mirror_status(0)
        self.status_message.emit("配置已清除")

    # ── 存储操作 ──
    def _browse_graphroot(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择存储目录")
        if d:
            self.ed_graphroot.setText(d)
            self._update_disk_info()

    def _save_storage_config(self) -> None:
        root = self.ed_graphroot.text().strip()
        if root:
            try:
                Path(root).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(
                    self, "目录不可用", f"无法创建/访问 {root}：\n{e}"
                )
                return
            if not os.access(root, os.W_OK):
                QMessageBox.critical(
                    self, "无写权限", f"{root} 不可写，请检查权限。"
                )
                return

        try:
            if root:
                # 驱动和运行时保持用户原有设置，本页不再暴露
                st = cfg.read_storage()
                ok, msg = cfg.safe_write_config(
                    "storage",
                    st.get("driver") or "overlay",
                    root,
                    st.get("runroot", ""),
                )
                if not ok:
                    QMessageBox.critical(self, "保存失败", msg)
                    return
            else:
                cfg.clear_storage()
        except OSError as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return

        self._ask_restart("存储配置已保存")

    def _run_migrate(self) -> None:
        r = QMessageBox.question(
            self, "执行迁移",
            "将执行 podman system migrate 把现有数据搬到新位置。\n"
            "请先停止所有容器。\n\n确定继续？",
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        import subprocess

        try:
            res = subprocess.run(
                ["podman", "system", "migrate"],
                capture_output=True, text=True, timeout=300,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            QMessageBox.critical(self, "迁移失败", str(e))
            return

        if res.returncode == 0:
            QMessageBox.information(self, "迁移完成", "数据已迁移到新位置。")
            self.status_message.emit("迁移完成")
        else:
            QMessageBox.critical(
                self, "迁移失败", res.stderr.strip() or "未知错误"
            )

    def _ask_restart(self, title: str) -> None:
        r = QMessageBox.question(
            self, title,
            f"{title}。\n\n需要重启 Podman 服务才能生效，现在重启吗？",
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self.status_message.emit("正在重启 Podman 服务…")
        ok, msg = cfg.restart_and_verify(timeout=15)
        if ok:
            self.status_message.emit(msg)
            from core.notify import notifier
            notifier().info("Podman 服务已重启", "连接正常")
        else:
            self.status_message.emit("服务启动失败")
            QMessageBox.critical(
                self, "服务启动失败",
                msg + "\n\n可在「设置 → 服务」中检查，或回滚配置（*.bak）。",
            )
        QTimer.singleShot(500, self._refresh_service)

    # ── 服务 ──
    def _build_service_tab(self) -> QWidget:
        """Podman 服务：状态 + 三个操作 + 自启

        KDE 设置页的习惯是一屏看完全部、不需要滚动找按钮，
        所以这里只放真正会用的东西，rootful 收进高级。
        """
        scroll, v = _page()

        # ══ 状态 ══
        sec, sv = _section("Podman 服务")

        head = QHBoxLayout()
        head.setSpacing(10)

        self.lbl_user_state = QLabel("—")
        self.lbl_user_state.setObjectName("serviceStateBig")
        head.addWidget(self.lbl_user_state)

        head.addStretch()

        self.lbl_service_version = QLabel("")
        self.lbl_service_version.setObjectName("hintLabel")
        head.addWidget(self.lbl_service_version)

        sv.addLayout(head)

        self.lbl_service_hint = _help("")
        sv.addWidget(self.lbl_service_hint)

        # 操作（右对齐，KDE 的动作按钮习惯放右侧）
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        btn_row.addStretch()

        self.btn_svc_start = QPushButton("启动")
        self.btn_svc_start.clicked.connect(lambda: self._service_action("start"))
        btn_row.addWidget(self.btn_svc_start)

        self.btn_svc_stop = QPushButton("停止")
        self.btn_svc_stop.clicked.connect(lambda: self._service_action("stop"))
        btn_row.addWidget(self.btn_svc_stop)

        self.btn_svc_restart = QPushButton("重启")
        self.btn_svc_restart.clicked.connect(
            lambda: self._service_action("restart")
        )
        btn_row.addWidget(self.btn_svc_restart)

        sv.addLayout(btn_row)

        # ══ 开机自启 ══

        self.chk_autostart = QCheckBox("登录系统时自动启动")
        self.chk_autostart.setToolTip(
            "登录时自动启动 podman.socket（用户级，无需提权）"
        )
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)
        sv.addWidget(self.chk_autostart)

        sv.addWidget(_help(
            "本应用启动时也会自动确保服务在运行，通常不需要手动操作。"
        ))

        v.addWidget(sec)

        # ══ 未安装引导 ══
        self.box_not_installed, nv = _section("安装 Podman")
        self.lbl_install_hint = _help(
            "系统中没有找到 Podman，无法管理容器。"
            "点下方按钮自动安装（会弹出系统授权框）。"
        )
        nv.addWidget(self.lbl_install_hint)

        inst_row = QHBoxLayout()
        inst_row.setSpacing(6)
        inst_row.addStretch()
        btn_recheck = QPushButton("重新检测")
        btn_recheck.clicked.connect(self._refresh_service)
        inst_row.addWidget(btn_recheck)
        self.btn_install = QPushButton(icon("download"), "安装 Podman")
        self.btn_install.clicked.connect(self._install_podman)
        inst_row.addWidget(self.btn_install)
        nv.addLayout(inst_row)

        self.box_not_installed.hide()
        v.addWidget(self.box_not_installed)

        # ══ 高级 ══
        self.btn_advanced = QPushButton("显示高级选项")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setFlat(True)
        self.btn_advanced.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_advanced.toggled.connect(self._toggle_advanced)
        adv_bar = QHBoxLayout()
        adv_bar.setContentsMargins(0, 0, 0, 0)
        adv_bar.addWidget(self.btn_advanced)
        adv_bar.addStretch()
        v.addLayout(adv_bar)

        self.box_advanced, av = _section("系统级服务（rootful）")

        sys_row = QHBoxLayout()
        sys_row.setSpacing(10)

        self.chk_sys_autostart = QCheckBox("开机自启")
        self.chk_sys_autostart.toggled.connect(self._on_sys_autostart_toggled)
        sys_row.addWidget(self.chk_sys_autostart)

        self.lbl_sys_state = QLabel("—")
        self.lbl_sys_state.setObjectName("serviceState")
        sys_row.addWidget(self.lbl_sys_state)

        sys_row.addStretch()

        for text, slot in [
            ("启动", lambda: self._sys_service_action("start")),
            ("停止", lambda: self._sys_service_action("stop")),
            ("重启", lambda: self._sys_service_action("restart")),
        ]:
            b = QPushButton(text)
            b.setToolTip("需要管理员权限，会弹出授权框")
            b.clicked.connect(slot)
            sys_row.addWidget(b)

        av.addLayout(sys_row)
        av.addWidget(_help(
            "以 root 运行，容器与用户级互不可见。绝大多数场景不需要开启。"
        ))

        self.box_advanced.hide()
        v.addWidget(self.box_advanced)

        # ══ 虚拟机网络 ══
        sec_net, nv2 = _section("虚拟机网络")

        self.lbl_net_mode = QLabel("")
        self.lbl_net_mode.setWordWrap(True)
        nv2.addWidget(self.lbl_net_mode)

        net_row = QHBoxLayout()
        net_row.setSpacing(6)
        net_row.addStretch()
        self.btn_net_setup = QPushButton("启用共享网络…")
        self.btn_net_setup.setToolTip(
            "创建共享网桥（需要一次系统授权）。同时配置轻机器的"
            "网络权限。启用后机器之间互通，宿主机直连内部 IP。"
        )
        self.btn_net_setup.clicked.connect(self._setup_bridge)
        net_row.addWidget(self.btn_net_setup)
        nv2.addLayout(net_row)

        nv2.addWidget(_info_box(
            "轻机器通过共享网桥获得独立 IP（10.99.x.x）：机器之间"
            "互通、宿主机直连、外部 SSH 客户端用 <名字>.vm.local。"
            "本操作同时配置 /etc/lxc/lxc-usernet 配额与家目录 ACL"
            "（非特权容器的必要前置），全部幂等，可放心重复执行。"
        ))

        v.addWidget(sec_net)

        v.addStretch()
        QTimer.singleShot(80, self._refresh_service)
        return scroll

    def _refresh_net_mode(self) -> None:
        from core.vm import bridge

        if bridge.ready():
            self.lbl_net_mode.setText(
                f"共享网络已启用 · 网桥 {bridge.BRIDGE} · "
                f"宿主机地址 {bridge.HOST_IP}"
            )
            self.btn_net_setup.setText("重新初始化…")
        else:
            self.lbl_net_mode.setText(
                "共享网络未启用 · 机器无法获得 IP，先启用再创建"
            )
            self.btn_net_setup.setText("启用共享网络…")

    def _setup_bridge(self) -> None:
        from core.async_task import run_async
        from core.vm import bridge

        ret = QMessageBox.question(
            self, "启用共享网络",
            "将创建网桥并配置 DHCP/NAT，需要一次系统授权。\n\n"
            "启用后：\n"
            "· 虚拟机之间可以互相访问\n"
            "· 宿主机可以直接用内部 IP 连接\n"
            "· 需要重启虚拟机才生效\n\n继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self.btn_net_setup.setEnabled(False)
        self.lbl_net_mode.setText("正在初始化…（请在授权框中确认）")

        def work():
            return bridge.setup()

        def done(res):
            ok, msg = res
            self.btn_net_setup.setEnabled(True)
            self._refresh_net_mode()
            if ok:
                self.status_message.emit("共享网络已启用，重启虚拟机后生效")
                QMessageBox.information(
                    self, "已启用",
                    "共享网络已启用。\n\n"
                    "请重启虚拟机（列表里点「重启」），"
                    "之后就可以直接用内部 IP 访问，"
                    "虚拟机之间也能互通。",
                )
            else:
                QMessageBox.warning(self, "初始化失败", msg)

        def error(msg):
            self.btn_net_setup.setEnabled(True)
            self._refresh_net_mode()
            QMessageBox.critical(self, "初始化失败", str(msg))

        run_async(work, on_done=done, on_error=error, parent=self)

    def _toggle_advanced(self, on: bool) -> None:
        self.box_advanced.setVisible(on)
        self.btn_advanced.setText("隐藏高级选项" if on else "显示高级选项")

    def _install_podman(self) -> None:
        """引导安装 podman"""
        from core.async_task import run_async

        self.btn_install.setEnabled(False)
        self.lbl_install_hint.setText("正在安装…（请在授权框中确认）")

        def work():
            return cfg.install_podman()

        def done(res):
            ok, msg = res
            self.btn_install.setEnabled(True)
            if ok:
                self.lbl_install_hint.setText("安装完成，正在启动服务…")
                cfg.ensure_user_service()
                self.client.check_connection()
                QTimer.singleShot(600, self._refresh_service)
            else:
                self.lbl_install_hint.setText(f"安装失败：{msg}")
                QMessageBox.warning(
                    self, "安装失败",
                    f"{msg}\n\n可手动安装：\n"
                    "Arch: sudo pacman -S podman\n"
                    "Debian/Ubuntu: sudo apt install podman\n"
                    "Fedora: sudo dnf install podman",
                )

        def error(msg):
            self.btn_install.setEnabled(True)
            self.lbl_install_hint.setText(f"安装失败：{msg}")

        run_async(work, on_done=done, on_error=error, parent=self)

    # ── 资源 ──
    def _build_resources_tab(self) -> QWidget:
        """资源：内存上限 + 当前占用"""
        from ui.charts import ContainerRows, MemorySummary

        scroll, v = _page()

        # ══ 内存上限 ══
        sec, sv = _section("内存上限")

        # 大号数值 + 说明同行
        val_row = QHBoxLayout()
        val_row.setSpacing(12)

        self.lbl_mem_value = QLabel("不限制")
        self.lbl_mem_value.setObjectName("bigValue")
        val_row.addWidget(self.lbl_mem_value)

        self.lbl_mem_hint = QLabel("")
        self.lbl_mem_hint.setObjectName("hintLabel")
        self.lbl_mem_hint.setWordWrap(True)
        val_row.addWidget(self.lbl_mem_hint, 1)

        sv.addLayout(val_row)

        # 滑块（松手才落盘）
        self.sld_mem = QSlider(Qt.Orientation.Horizontal)
        self.sld_mem.setRange(0, 100)
        self.sld_mem.setSingleStep(1)
        self.sld_mem.setPageStep(5)
        self.sld_mem.valueChanged.connect(self._on_mem_slider)
        self.sld_mem.sliderReleased.connect(self._schedule_mem_apply)
        sv.addWidget(self.sld_mem)

        # 两端刻度
        scale = QHBoxLayout()
        scale.setSpacing(0)
        self.lbl_scale_min = QLabel("不限制")
        self.lbl_scale_min.setObjectName("hintLabel")
        scale.addWidget(self.lbl_scale_min)
        scale.addStretch()
        self.lbl_scale_max = QLabel("")
        self.lbl_scale_max.setObjectName("hintLabel")
        scale.addWidget(self.lbl_scale_max)
        sv.addLayout(scale)

        # 快捷档位
        seg = QHBoxLayout()
        seg.setSpacing(6)
        self._preset_buttons: list[QPushButton] = []
        self._preset_values: list[int] = []
        self._seg_layout = seg
        sv.addLayout(seg)

        sv.addWidget(_info_box(
            "上限只做启动前检查：新容器会让已分配内存超过上限时先提醒，"
            "避免把系统内存吃满。已在运行的容器不受影响。"
        ))

        v.addWidget(sec)

        # ══ 当前占用 ══
        sec2, sv2 = _section()

        head2 = QHBoxLayout()
        head2.setSpacing(10)

        t2 = QLabel("当前占用")
        t2.setObjectName("cardTitle")
        head2.addWidget(t2)

        self.lbl_usage_total = QLabel("—")
        self.lbl_usage_total.setObjectName("cardTitle")
        head2.addWidget(self.lbl_usage_total)

        head2.addStretch()

        self.lbl_usage_badge = QLabel("")
        self.lbl_usage_badge.setObjectName("badgeOk")
        head2.addWidget(self.lbl_usage_badge)

        sv2.addLayout(head2)

        self.mem_chart = MemorySummary()
        sv2.addWidget(self.mem_chart)

        self.mem_bars = ContainerRows()
        sv2.addWidget(self.mem_bars)

        v.addWidget(sec2)
        v.addStretch()

        self._presets_built = False
        self._mem_host_gb = 0.0
        self._mem_dirty = False

        QTimer.singleShot(80, self._refresh_memory)
        return scroll

    def _on_mem_slider(self, value: int) -> None:
        """拖动滑块：同步大号数字（松手才落盘）"""
        self._mem_dirty = True
        self.lbl_mem_value.setText("不限制" if value <= 0 else f"{value} GB")
        self._sync_preset_highlight(value)

    def _schedule_mem_apply(self) -> None:
        """防抖：连续微调时只写一次配置"""
        if not hasattr(self, "_mem_timer"):
            self._mem_timer = QTimer(self)
            self._mem_timer.setSingleShot(True)
            self._mem_timer.setInterval(350)
            self._mem_timer.timeout.connect(self._apply_mem_limit)
        self._mem_timer.start()

    def _sync_preset_highlight(self, gb: int) -> None:
        for b in self._preset_buttons:
            b.setChecked(b.property("segGb") == gb)


    def _rebuild_presets(self, host_gb: float) -> None:
        """按物理内存生成档位与滑块范围

        规则：
          · 只出现「整齐」的档位（1/2/4/8/12/16/24/32/48/64…GB）
          · 全部 ≤ 物理内存
          · 最多 6 档，避免一排按钮把界面撑爆
        """
        if self._presets_built or host_gb <= 0:
            return

        host = max(2, int(host_gb))
        self._mem_host_gb = float(host)

        LADDER = [1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256]
        usable = [v for v in LADDER if v <= host] or [1]
        picked = usable[-6:] if len(usable) > 6 else usable

        # 滑块 / 数值框：0 = 不限制，其余直接是 GB
        self.sld_mem.blockSignals(True)
        self.sld_mem.setRange(0, host)
        self.sld_mem.blockSignals(False)
        self.sld_mem.setPageStep(max(1, host // 20))
        self.lbl_scale_max.setText(f"{host} GB（全部）")

        # 清空旧档位按钮
        while self._seg_layout.count():
            item = self._seg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._preset_buttons.clear()

        self._preset_values = [0] + picked
        for gb in self._preset_values:
            text = "不限制" if gb == 0 else f"{gb}G"
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFixedHeight(26)
            b.setMinimumWidth(48)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("segGb", gb)
            b.clicked.connect(lambda _=False, g=gb: self._select_preset(g))
            self._seg_layout.addWidget(b)
            self._preset_buttons.append(b)

        self._seg_layout.addStretch()
        self._presets_built = True

    def _select_preset(self, gb: int) -> None:
        """点档位按钮：同步两个控件并立即应用"""
        self.sld_mem.blockSignals(True)
        self.sld_mem.setValue(gb)
        self.sld_mem.blockSignals(False)
        self.lbl_mem_value.setText("不限制" if gb <= 0 else f"{gb} GB")
        self._sync_preset_highlight(gb)
        self._mem_dirty = True
        self._apply_mem_limit()

    def _current_preset_gb(self) -> int:
        return int(self.sld_mem.value())

    def _apply_mem_limit(self) -> None:
        """把滑块当前值写入配置"""
        value = float(self._current_preset_gb())
        current = float(cfg.get_memory_limit_gb())
        if abs(value - current) < 0.01 and not self._mem_dirty:
            return

        cfg.set_memory_limit_gb(value)
        self._mem_dirty = False

        if value <= 0:
            self.status_message.emit("已取消内存上限")
        else:
            self.status_message.emit(f"内存上限已设为 {value:.0f} GB")
        self._refresh_memory()

    def _refresh_memory(self) -> None:
        """刷新内存图表与上限显示"""
        from core import podman_config as pcfg

        try:
            u = pcfg.compute_usage(self.client)
        except Exception:
            return

        self.mem_chart.set_data(
            u["host_gb"], u["limit_gb"], u["declared_gb"], u["actual_gb"]
        )
        self.mem_bars.set_items(
            u["containers"],
            max(u["limit_gb"], u["host_gb"], 1.0),
            u["host_gb"],
            vms=u.get("vms"),
        )

        if not self._presets_built and u["host_gb"] > 0:
            self._rebuild_presets(u["host_gb"])

        # 用户正在拖动时不要抢他的值
        if not self._mem_dirty and self._presets_built:
            cur = int(round(u["limit_gb"]))
            self.sld_mem.blockSignals(True)
            self.sld_mem.setValue(cur)
            self.sld_mem.blockSignals(False)
            self._sync_preset_highlight(cur)

        # 卡片标题行：大号数值 + 当前占用 + 状态徽标
        cur = int(round(u["limit_gb"]))
        self.lbl_mem_value.setText(
            "不限制" if cur <= 0 else f"{cur} GB"
        )
        if cur > 0:
            self.lbl_usage_total.setText(
                f"{u['actual_gb']:.2f} GB / {cur:.2f} GB"
            )
        else:
            self.lbl_usage_total.setText(
                f"{u['actual_gb']:.2f} GB / 整机 {u['host_gb']:.1f} GB"
            )

        if cur <= 0:
            badge, kind = "未限制", "badgeOk"
        elif u["declared_gb"] > cur:
            badge, kind = "已超限", "badgeWarn"
        elif u["declared_gb"] > cur * 0.8:
            badge, kind = "接近上限", "badgeWarn"
        else:
            badge, kind = "正常", "badgeOk"

        self.lbl_usage_badge.setText(badge)
        if self.lbl_usage_badge.objectName() != kind:
            self.lbl_usage_badge.setObjectName(kind)
            self.lbl_usage_badge.style().unpolish(self.lbl_usage_badge)
            self.lbl_usage_badge.style().polish(self.lbl_usage_badge)

        # 提示
        if u["limit_gb"] <= 0:
            self.lbl_mem_hint.setText(
                f"已分配 {u['declared_gb']:.1f} GB"
            )
            self.lbl_mem_hint.setStyleSheet("")
        elif u["declared_gb"] > u["limit_gb"]:
            self.lbl_mem_hint.setText(
                f"已超出 {u['declared_gb'] - u['limit_gb']:.1f} GB"
            )
            self.lbl_mem_hint.setStyleSheet(
                f"color: {semantic_colors()['danger']};"
            )
        else:
            remain = u["limit_gb"] - u["declared_gb"]
            self.lbl_mem_hint.setText(
                f"剩余 {remain:.1f} GB · 已分配 {u['declared_gb']:.1f} GB"
            )
            self.lbl_mem_hint.setStyleSheet("")

    def _do_verified_restart(self) -> None:
        """重启服务并验证"""
        self.status_message.emit("正在重启 Podman 服务…")

        ok, msg = cfg.restart_and_verify(timeout=20)
        if ok:
            self.status_message.emit(msg)
            from core.notify import notifier
            notifier().info("Podman 服务已重启", "连接正常")
        else:
            self.status_message.emit("服务启动失败")
            QMessageBox.critical(
                self, "服务启动失败",
                msg + "\n\n可在下方手动重试，或检查配置备份（*.bak）。",
            )
        QTimer.singleShot(400, self._refresh_service)

    # tab 索引：0 服务 / 1 资源 / 2 镜像加速 / 3 存储 / 4 关于
    TAB_RESOURCES = 1

    def _on_tab_changed(self, idx: int) -> None:
        if idx == self.TAB_RESOURCES:
            self._refresh_memory()

    def _tick_memory(self) -> None:
        """每秒刷新（仅资源页可见时）"""
        if not self.isVisible():
            return
        if self.tabs.currentIndex() != self.TAB_RESOURCES:
            return
        self._refresh_memory()

    def _set_state_label(self, label, text: str, color: str, state_key: str) -> None:
        """给状态标签设置图标 + 文字

        用 QHBoxLayout 容器替换单个 QLabel 的思路行不通（label 已在布局中），
        改为：label 内嵌 base64 PNG 的富文本，图标与文字天然对齐。
        """
        import base64

        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
        from resources.icons import state_icon

        pix = state_icon(state_key, 14).pixmap(14, 14)

        buf = QByteArray()
        qbuf = QBuffer(buf)
        qbuf.open(QIODevice.OpenModeFlag.WriteOnly)
        pix.save(qbuf, "PNG")
        qbuf.close()

        b64 = base64.b64encode(bytes(buf)).decode()

        label.setText(
            f'<img src="data:image/png;base64,{b64}" '
            f'width="14" height="14" style="vertical-align:middle;"> '
            f'<span style="color:{color};">{text}</span>'
        )
        label.setTextFormat(Qt.TextFormat.RichText)

    def _refresh_service(self) -> None:
        """刷新服务状态（含「未安装」分支）"""
        installed = cfg.podman_installed()

        # 未安装：整块状态卡片换成安装引导
        self.box_not_installed.setVisible(not installed)
        if not installed:
            for wdg in (self.lbl_user_state, self.lbl_service_version,
                        self.btn_svc_start, self.btn_svc_stop,
                        self.btn_svc_restart, self.chk_autostart,
                        self.lbl_service_hint):
                wdg.setVisible(False)
            return

        for wdg in (self.lbl_user_state, self.lbl_service_version,
                    self.btn_svc_start, self.btn_svc_stop,
                    self.btn_svc_restart, self.chk_autostart,
                    self.lbl_service_hint):
            wdg.setVisible(True)

        st = cfg.autostart_status()

        blocked = self.chk_autostart.blockSignals(True)
        self.chk_autostart.setChecked(st["enabled"])
        self.chk_autostart.blockSignals(blocked)

        text, color, ikey = self._active_text(st["active_raw"])
        self._set_state_label(self.lbl_user_state, text, color, ikey)

        ver = cfg.podman_version()
        self.lbl_service_version.setText(f"Podman {ver}" if ver else "")

        # 按钮按状态启停，避免点了没反应
        running = st["active_raw"] == "active"
        self.btn_svc_start.setEnabled(not running)
        self.btn_svc_stop.setEnabled(running)
        self.btn_svc_restart.setEnabled(running)

        sst = cfg.system_service_status()
        blocked = self.chk_sys_autostart.blockSignals(True)
        self.chk_sys_autostart.setChecked(sst["enabled"])
        self.chk_sys_autostart.blockSignals(blocked)

        text2, color2, ikey2 = self._active_text(sst["active_raw"])
        self._set_state_label(self.lbl_sys_state, text2, color2, ikey2)

        if running:
            self.lbl_service_hint.setText(
                f"已连接 · {self.client.socket_path}"
            )
        else:
            self.lbl_service_hint.setText(
                "服务未运行，点「启动」即可。"
                "（命令行等价于 systemctl --user start podman.socket）"
            )

        if hasattr(self, "lbl_net_mode"):
            self._refresh_net_mode()

        if hasattr(self, "mem_chart"):
            self._refresh_memory()

    @staticmethod
    def _active_text(raw: str) -> tuple[str, str, str]:
        """返回 (文字, 颜色, 状态key)"""
        return {
            "active": ("运行中", "#27ae60", "running"),
            "inactive": ("已停止", "#95a5a6", "exited"),
            "failed": ("启动失败", "#e74c3c", "dead"),
            "activating": ("启动中", "#f39c12", "restarting"),
            "deactivating": ("停止中", "#f39c12", "paused"),
        }.get(raw, (raw, "#95a5a6", "exited"))

    def _on_autostart_toggled(self, checked: bool) -> None:
        ok, msg = cfg.set_autostart(checked)
        if ok:
            self.status_message.emit(msg)
            from core.notify import notifier
            notifier().info(
                "已开启开机自启" if checked else "已关闭开机自启",
                "用户级 podman.socket",
            )
        else:
            QMessageBox.warning(
                self, "操作失败",
                f"{'开启' if checked else '关闭'}失败：\n\n{msg}",
            )
            blocked = self.chk_autostart.blockSignals(True)
            self.chk_autostart.setChecked(not checked)
            self.chk_autostart.blockSignals(blocked)
        QTimer.singleShot(400, self._refresh_service)

    def _on_sys_autostart_toggled(self, checked: bool) -> None:
        self.status_message.emit("等待管理员授权…")
        ok, msg = cfg.set_system_autostart(checked)
        if ok:
            self.status_message.emit(msg)
            from core.notify import notifier
            notifier().info(
                "已开启系统级自启" if checked else "已关闭系统级自启",
                "podman.socket (rootful)",
            )
        else:
            self.status_message.emit(f"操作失败: {msg}")
            if "取消" not in msg:
                QMessageBox.warning(
                    self, "操作失败",
                    f"{'开启' if checked else '关闭'}系统级自启失败：\n\n{msg}",
                )
            blocked = self.chk_sys_autostart.blockSignals(True)
            self.chk_sys_autostart.setChecked(not checked)
            self.chk_sys_autostart.blockSignals(blocked)
        QTimer.singleShot(600, self._refresh_service)

    def _service_action(self, action: str) -> None:
        import subprocess

        label = {"start": "启动", "stop": "停止", "restart": "重启"}.get(action, action)

        if action == "stop":
            r = QMessageBox.question(
                self, "停止服务",
                "停止后本程序将无法管理容器。\n\n确定继续？",
            )
            if r != QMessageBox.StandardButton.Yes:
                return

        try:
            res = subprocess.run(
                ["systemctl", "--user", action, "podman.socket"],
                capture_output=True, text=True, timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            QMessageBox.critical(self, f"{label}失败", str(e))
            return

        if res.returncode == 0:
            self.status_message.emit(f"用户级服务{label}成功")

            # 主动更新连接状态：不等定时器轮询
            if action == "stop":
                self.client._set_connected(False)
            elif action in ("start", "restart"):
                # 等服务起来再探测
                QTimer.singleShot(800, self.client.check_connection)
        else:
            QMessageBox.warning(
                self, f"{label}失败",
                (res.stderr or res.stdout).strip() or "未知错误",
            )
        QTimer.singleShot(600, self._refresh_service)

    def _sys_service_action(self, action: str) -> None:
        label = {"start": "启动", "stop": "停止", "restart": "重启"}.get(action, action)

        r = QMessageBox.question(
            self, f"{label}系统级服务",
            f"将{'停止' if action == 'stop' else label}系统级 podman.socket。\n"
            f"需要管理员权限，会弹出授权框。\n\n确定继续？",
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        self.status_message.emit("等待管理员授权…")
        ok, msg = cfg.system_service_action(action)
        if ok:
            self.status_message.emit(msg)
        else:
            self.status_message.emit(f"{label}失败: {msg}")
            if "取消" not in msg:
                QMessageBox.warning(self, f"{label}失败", msg)
        QTimer.singleShot(600, self._refresh_service)

