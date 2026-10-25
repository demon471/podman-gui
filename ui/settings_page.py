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
    QSpinBox,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QFrame,
)

from core import podman_config as cfg
from core.podman_client import PodmanClient, PodmanError
from resources.icons import ICON_SIZE_TOOLBAR, icon


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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview_tab(), "概览")
        self.tabs.addTab(self._build_service_tab(), "服务")
        self.tabs.addTab(self._build_mirror_tab(), "镜像加速")
        self.tabs.addTab(self._build_storage_tab(), "存储")
        self.tabs.addTab(self._build_about_tab(), "关于")
        layout.addWidget(self.tabs)

    # ── 概览 ──
    # ── 镜像加速 ──
    def _build_mirror_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        hint = QLabel(
            "镜像加速用于加快 docker.io 等仓库的拉取速度。"
            "列表按从上到下的顺序尝试，前一个失败自动降级到下一个。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # ── 加速地址 ──
        grp1 = QGroupBox("加速地址")
        v1 = QVBoxLayout(grp1)
        v1.setContentsMargins(12, 12, 12, 10)
        v1.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(8)

        self.lst_mirrors = QListWidget()
        self.lst_mirrors.setMinimumHeight(110)
        self.lst_mirrors.setMaximumHeight(170)
        self.lst_mirrors.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection
        )
        self.lst_mirrors.itemSelectionChanged.connect(self._update_mirror_buttons)
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

        self.btn_mirror_up = icon_btn("up", "上移（提高优先级）",
                                      lambda: self._move_mirror(-1))
        self.btn_mirror_down = icon_btn("down", "下移（降低优先级）",
                                        lambda: self._move_mirror(1))
        btncol.addSpacing(6)
        self.btn_mirror_del = icon_btn("delete", "移除选中的地址",
                                       self._del_mirror)
        btncol.addStretch()

        body.addLayout(btncol)
        v1.addLayout(body)

        add_row = QHBoxLayout()
        add_row.setSpacing(6)

        self.cmb_preset = QComboBox()
        self.cmb_preset.addItem("从预设选择…", "")
        for name, url in cfg.MIRROR_PRESETS:
            self.cmb_preset.addItem(name, url)
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_selected)
        add_row.addWidget(self.cmb_preset, 1)

        btn_add = QPushButton(icon("add"), "添加")
        btn_add.setToolTip("添加自定义地址")
        btn_add.clicked.connect(self._add_mirror)
        add_row.addWidget(btn_add)

        v1.addLayout(add_row)
        layout.addWidget(grp1)

        # ── 代理 ──
        grp2 = QGroupBox("HTTP 代理（访问境外 registry）")
        f2 = QFormLayout(grp2)
        f2.setContentsMargins(12, 12, 12, 10)
        f2.setSpacing(8)

        self.chk_proxy = QCheckBox("启用代理")
        self.chk_proxy.toggled.connect(self._on_proxy_toggled)
        f2.addRow(self.chk_proxy)

        self.cmb_proxy_preset = QComboBox()
        self.cmb_proxy_preset.addItem("从预设选择…", "")
        for name, url in cfg.PROXY_PRESETS:
            self.cmb_proxy_preset.addItem(name, url)
        self.cmb_proxy_preset.currentIndexChanged.connect(
            self._on_proxy_preset_selected
        )
        f2.addRow("常用地址:", self.cmb_proxy_preset)

        self.ed_proxy = QLineEdit()
        self.ed_proxy.setPlaceholderText("http://127.0.0.1:7890")
        f2.addRow("代理地址:", self.ed_proxy)

        self.ed_noproxy = QLineEdit()
        self.ed_noproxy.setPlaceholderText("localhost,127.0.0.1,10.88.0.0/16")
        f2.addRow("排除地址:", self.ed_noproxy)

        layout.addWidget(grp2)

        # ── 底部操作 ──
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.lbl_mirror_status = QLabel("")
        self.lbl_mirror_status.setObjectName("hintLabel")
        bar.addWidget(self.lbl_mirror_status, 1)

        btn_reload = QPushButton("重新读取")
        btn_reload.clicked.connect(self.load_config)
        bar.addWidget(btn_reload)

        btn_clear = QPushButton("清除配置")
        btn_clear.clicked.connect(self._clear_all)
        bar.addWidget(btn_clear)

        btn_save = QPushButton(icon("save"), "保存")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self._save_mirror_config)
        bar.addWidget(btn_save)

        layout.addLayout(bar)
        layout.addStretch()

        scroll.setWidget(w)
        return scroll

    # ── 存储 ──
    def _build_storage_tab(self) -> QWidget:
        """存储：状态 + 配置 + 迁移 + 重置"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        layout.addWidget(self._build_storage_main())
        layout.addWidget(self._build_storage_reset())
        layout.addStretch()

        scroll.setWidget(w)
        return scroll

    # ── 存储主区（状态 + 配置 + 迁移）──
    def _build_storage_main(self) -> QWidget:
        grp = QGroupBox("存储")
        v = QVBoxLayout(grp)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(10)

        # ══ 状态行 ══
        status = QHBoxLayout()
        status.setSpacing(28)

        def stat(label):
            holder = QWidget()
            h = QHBoxLayout(holder)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            k = QLabel(label)
            k.setObjectName("formLabel")
            h.addWidget(k)
            val = QLabel("—")
            h.addWidget(val)
            return holder, val

        w1, self.st_driver = stat("驱动")
        w2, self.st_used = stat("已用")
        status.addWidget(w1)
        status.addWidget(w2)
        status.addStretch()

        btn_refresh = QPushButton(icon("refresh"), "")
        btn_refresh.setFixedSize(30, 30)
        btn_refresh.setToolTip("刷新存储状态")
        btn_refresh.clicked.connect(self._refresh_storage_status)
        status.addWidget(btn_refresh)

        v.addLayout(status)

        # ══ 分隔线 ══
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(line)

        # ══ 配置区 ══
        f = QFormLayout()
        f.setContentsMargins(0, 0, 0, 0)
        f.setSpacing(8)

        self.ed_graphroot = QLineEdit()
        self.ed_graphroot.setPlaceholderText(
            "留空使用默认（~/.local/share/containers/storage）"
        )
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.ed_graphroot, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_graphroot)
        row.addWidget(btn_browse)
        f.addRow("数据目录:", row)

        self.lbl_disk = QLabel("")
        self.lbl_disk.setObjectName("hintLabel")
        f.addRow("", self.lbl_disk)

        # 当前生效的目录（只读，等宽字体）
        self.st_root = QLabel("—")
        self.st_root.setStyleSheet(
            "font-family: 'JetBrains Mono', 'Maple Mono NF CN', monospace;"
        )
        self.st_root.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        f.addRow("当前:", self.st_root)

        self.ed_runroot = QLineEdit()
        self.ed_runroot.setPlaceholderText(
            "留空使用默认（/run/user/UID/containers）"
        )
        f.addRow("运行时:", self.ed_runroot)

        self.cmb_driver = QComboBox()
        self.cmb_driver.addItems(["overlay", "vfs", "btrfs", "zfs"])
        self.cmb_driver.setFixedWidth(140)
        f.addRow("驱动:", self.cmb_driver)

        v.addLayout(f)

        # ══ 操作行 ══
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        lbl_mig = QLabel("改目录后需迁移数据")
        lbl_mig.setObjectName("hintLabel")
        row2.addWidget(lbl_mig)

        btn_migrate = QPushButton("迁移")
        btn_migrate.setToolTip("执行 podman system migrate（需先停止所有容器）")
        btn_migrate.clicked.connect(self._run_migrate)
        row2.addWidget(btn_migrate)

        row2.addStretch()

        btn_reload = QPushButton("重新读取")
        btn_reload.clicked.connect(self.load_config)
        row2.addWidget(btn_reload)

        btn_save = QPushButton(icon("save"), "保存并重启")
        btn_save.clicked.connect(self._save_storage_config)
        row2.addWidget(btn_save)

        v.addLayout(row2)

        QTimer.singleShot(120, self._refresh_storage_status)
        return grp

    def _refresh_storage_status(self) -> None:
        """刷新存储状态行"""
        try:
            info = self.client.system_info()
            store = info.get("store") or {}
            self.st_driver.setText(store.get("graphDriverName", "—"))

            root = store.get("graphRoot", "—")
            self.st_root.setText(root)

            free, total = cfg.disk_free(root) if root != "—" else (0, 0)
            if total:
                self.lbl_disk.setText(
                    f"{free / 1024**3:.1f} GB 可用 / {total / 1024**3:.1f} GB 总计"
                )

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

            # 重置预览同步更新
            self._reset_stats = (len(ctrs), len(imgs), len(vols), used)
            if hasattr(self, "lbl_reset_preview"):
                self._refresh_reset_preview()
        except Exception as e:
            self.st_driver.setText(f"失败: {e}")

    # ── 重置（危险）──
    def _build_storage_reset(self) -> QWidget:
        grp = QGroupBox("重置")
        grp.setObjectName("dangerGroup")
        v = QVBoxLayout(grp)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(8)

        warn = QLabel(
            "恢复到首次安装状态：删除全部容器、镜像、卷、网络，"
            "并清除代理与加速配置。"
            "<br><span style='color:#95a5a6'>"
            "宿主目录（如 /data）中的 bind mount 数据不受影响。</span>"
        )
        warn.setObjectName("dangerHint")
        warn.setWordWrap(True)
        v.addWidget(warn)

        row = QHBoxLayout()
        row.setSpacing(8)

        self.lbl_reset_preview = QLabel("")
        self.lbl_reset_preview.setObjectName("hintLabel")
        row.addWidget(self.lbl_reset_preview, 1)

        self.btn_reset = QPushButton(icon("delete", danger=True), "重置 Podman")
        self.btn_reset.setObjectName("dangerButton")
        self.btn_reset.clicked.connect(self._run_full_reset)
        row.addWidget(self.btn_reset)

        v.addLayout(row)
        return grp

    def _refresh_reset_preview(self) -> None:
        """更新重置预览文字"""
        stats = getattr(self, "_reset_stats", None)
        if not stats:
            self.lbl_reset_preview.setText("")
            self.btn_reset.setEnabled(True)
            return

        ctrs, imgs, vols, used = stats
        if not (ctrs or imgs or vols):
            self.lbl_reset_preview.setText("已无内容可删除")
            self.btn_reset.setEnabled(False)
        else:
            self.lbl_reset_preview.setText(
                f"将删除 {ctrs} 容器 · {imgs} 镜像 · {vols} 卷"
                f"（{used / 1024**3:.2f} GB）"
            )
            self.btn_reset.setEnabled(True)

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
        self._tasks = [t for t in getattr(self, "_tasks", []) if t._thread is not None]
        self._tasks.append(task)

    # ── 关于 ──
    def _build_about_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        from core.config import APP_NAME, APP_VERSION

        lbl = QLabel(f"<h2>{APP_NAME}</h2><p>版本 {APP_VERSION}</p>")
        layout.addWidget(lbl)

        info = QLabel(
            "<p>KDE Plasma 原生 Podman 图形管理工具</p>"
            "<p>基于 PyQt6 + Podman REST API 构建</p>"
            "<hr>"
            "<p><b>配置文件位置</b><br>"
            f"registries.conf: {cfg.REGISTRIES_CONF}<br>"
            f"containers.conf: {cfg.CONTAINERS_CONF}<br>"
            f"storage.conf: {cfg.STORAGE_CONF}</p>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)
        layout.addStretch()
        return w

    def _build_overview_tab(self) -> QWidget:
        """Podman 系统概览（原侧边栏「概览」页内容）"""
        from PyQt6.QtWidgets import QGridLayout, QFormLayout
        from core.async_task import run_async
        from ui.utils import fmt_size

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        grid = QGridLayout(w)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setSpacing(12)

        def info_row(label, mono=False):
            """一行「标签 + 值」"""
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(12)
            k = QLabel(label)
            k.setObjectName("formLabel")
            k.setFixedWidth(88)
            h.addWidget(k)
            v = QLabel("—")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            v.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            if mono:
                v.setStyleSheet(
                    "font-family: 'JetBrains Mono', 'Maple Mono NF CN', monospace;"
                )
            h.addWidget(v, 1)
            return row, v

        # ── 版本信息 ──
        g1 = QGroupBox("版本信息")
        f1 = QFormLayout(g1)
        f1.setContentsMargins(12, 12, 12, 10)
        f1.setSpacing(6)
        r1, self._ov_version = info_row("Podman")
        r2, self._ov_api = info_row("API 版本")
        r3, self._ov_platform = info_row("平台")
        for r in (r1, r2, r3):
            f1.addRow(r)
        grid.addWidget(g1, 0, 0)

        # ── 运行环境 ──
        g2 = QGroupBox("运行环境")
        f2 = QFormLayout(g2)
        f2.setContentsMargins(12, 12, 12, 10)
        f2.setSpacing(6)
        r4, self._ov_rootless = info_row("Rootless")
        r5, self._ov_cgroup = info_row("Cgroup")
        r6, self._ov_os = info_row("操作系统")
        r7, self._ov_kernel = info_row("内核")
        for r in (r4, r5, r6, r7):
            f2.addRow(r)
        grid.addWidget(g2, 0, 1)

        # ── 存储 ──
        g3 = QGroupBox("存储")
        f3 = QFormLayout(g3)
        f3.setContentsMargins(12, 12, 12, 10)
        f3.setSpacing(6)
        r8, self._ov_driver = info_row("驱动")
        r9, self._ov_root = info_row("数据目录", mono=True)
        r10, self._ov_runroot = info_row("运行时", mono=True)
        r11, self._ov_socket = info_row("Socket", mono=True)
        for r in (r8, r9, r10, r11):
            f3.addRow(r)
        grid.addWidget(g3, 1, 0, 1, 2)

        # ── 资源占用 ──
        g4 = QGroupBox("资源占用")
        f4 = QFormLayout(g4)
        f4.setContentsMargins(12, 12, 12, 10)
        f4.setSpacing(6)
        r12, self._ov_images = info_row("镜像")
        r13, self._ov_containers = info_row("容器")
        r14, self._ov_volumes = info_row("卷")
        for r in (r12, r13, r14):
            f4.addRow(r)
        grid.addWidget(g4, 2, 0, 1, 2)

        grid.setRowStretch(3, 1)

        scroll.setWidget(w)
        # 首次加载延后，避免阻塞窗口显示
        QTimer.singleShot(80, self._load_overview)
        return scroll

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
            self.lbl_mirror_status.setText(
                f"已配置 {count} 个加速地址" if count
                else "当前直连官方仓库（未使用加速）"
            )
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

        # 存储
        st = cfg.read_storage()
        self.ed_graphroot.setText(st.get("graphroot", ""))
        self.ed_runroot.setText(st.get("runroot", ""))
        if st.get("driver"):
            i = self.cmb_driver.findText(st["driver"])
            if i >= 0:
                self.cmb_driver.setCurrentIndex(i)
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

    def _move_mirror(self, delta: int) -> None:
        row = self.lst_mirrors.currentRow()
        new = row + delta
        if row < 0 or new < 0 or new >= self.lst_mirrors.count():
            return
        item = self.lst_mirrors.takeItem(row)
        self.lst_mirrors.insertItem(new, item)
        self.lst_mirrors.setCurrentRow(new)
        self._update_mirror_buttons()

    def _on_proxy_toggled(self, on: bool) -> None:
        self.ed_proxy.setEnabled(on)
        self.ed_noproxy.setEnabled(on)
        self.cmb_proxy_preset.setEnabled(on)

    def _on_proxy_preset_selected(self, idx: int) -> None:
        url = self.cmb_proxy_preset.itemData(idx)
        if url:
            self.ed_proxy.setText(url)

    def _save_mirror_config(self) -> None:
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
                    QMessageBox.critical(self, "保存失败", msg)
                    return
            else:
                cfg.clear_registries()

            if self.chk_proxy.isChecked() and self.ed_proxy.text().strip():
                p = self.ed_proxy.text().strip()
                ok, msg = cfg.safe_write_config(
                    "proxy", p, p, self.ed_noproxy.text().strip()
                )
                if not ok:
                    QMessageBox.critical(self, "保存失败", msg)
                    return
            else:
                cfg.clear_proxy()
        except OSError as e:
            QMessageBox.critical(self, "保存失败", f"写入配置出错：\n{e}")
            return

        self._ask_restart("镜像加速配置已保存")

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
                ok, msg = cfg.safe_write_config(
                    "storage",
                    self.cmb_driver.currentText(),
                    root,
                    self.ed_runroot.text().strip(),
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

    def _load_overview(self) -> None:
        """异步加载概览数据（version/system_info 各约 900ms）"""
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
                eng = next((x for x in comps if x.get("Name") == "Podman Engine"), {})
                self._ov_version.setText(eng.get("Version", "—"))
                self._ov_api.setText(str(eng.get("Details", {}).get("APIVersion", "—")))
                self._ov_platform.setText(v.get("Platform", {}).get("Name", "—"))
            elif "version_error" in d:
                self._ov_version.setText(f"失败: {d['version_error']}")

            info = d.get("info")
            if info:
                store = info.get("store") or {}
                host = info.get("host") or {}
                sec = host.get("security") or {}
                self._ov_driver.setText(store.get("graphDriverName", "—"))
                self._ov_root.setText(store.get("graphRoot", "—"))
                self._ov_runroot.setText(store.get("runRoot", "—"))
                self._ov_socket.setText(self.client.socket_path or "—")
                self._ov_rootless.setText("是" if sec.get("rootless") else "否")
                self._ov_cgroup.setText(host.get("cgroupVersion", "—"))
                self._ov_os.setText(str(host.get("os", "—"))[:48])
                self._ov_kernel.setText(host.get("kernel", "—"))

            df = d.get("df")
            if df:
                imgs = df.get("Images") or []
                ctrs = df.get("Containers") or []
                vols = df.get("Volumes") or []
                self._ov_images.setText(
                    f"{len(imgs)} 个 · {fmt_size(sum(x.get('Size',0) or 0 for x in imgs))}"
                )
                self._ov_containers.setText(
                    f"{len(ctrs)} 个 · {fmt_size(sum(x.get('SizeRw',0) or 0 for x in ctrs))}"
                )
                self._ov_volumes.setText(
                    f"{len(vols)} 个 · {fmt_size(sum((x.get('UsageData') or {}).get('Size',0) or 0 for x in vols))}"
                )

        def err(msg):
            self._ov_version.setText(f"加载失败: {msg}")

        run_async(work, on_done=done, on_error=err, parent=self)

    # ── 服务 ──
    def _build_service_tab(self) -> QWidget:
        """Podman 服务与资源限制"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        # ══════════ Podman 服务（用户级 + 系统级合并）══════════
        layout.addWidget(self._build_services_group())

        # ══════════ 资源限制（放最后）══════════
        layout.addWidget(self._build_memory_group())

        layout.addStretch()

        scroll.setWidget(w)
        QTimer.singleShot(80, self._refresh_service)
        return scroll

    def _build_services_group(self) -> QWidget:
        """服务管理：用户级 + 系统级 合并为一个区块"""
        grp = QGroupBox("Podman 服务")
        v = QVBoxLayout(grp)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(10)

        # ── 用户级（当前使用）──
        row1 = QHBoxLayout()
        row1.setSpacing(10)

        self.chk_autostart = QCheckBox("开机自启（用户级）")
        self.chk_autostart.setToolTip(
            "登录系统时自动启动 podman.socket（rootless，无需提权）"
        )
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)
        row1.addWidget(self.chk_autostart)

        self.lbl_user_state = QLabel("—")
        self.lbl_user_state.setObjectName("serviceState")
        row1.addWidget(self.lbl_user_state)

        row1.addStretch()

        for text, slot in [
            ("刷新", self._refresh_service),
            ("启动", lambda: self._service_action("start")),
            ("停止", lambda: self._service_action("stop")),
            ("重启", lambda: self._service_action("restart")),
        ]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row1.addWidget(b)

        v.addLayout(row1)

        # ── 分隔线 ──
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        v.addWidget(line)

        # ── 系统级（rootful，需提权）──
        row2 = QHBoxLayout()
        row2.setSpacing(10)

        self.chk_sys_autostart = QCheckBox("开机自启（系统级）")
        self.chk_sys_autostart.setToolTip(
            "开机时启动系统级 podman.socket（rootful，需要管理员授权）"
        )
        self.chk_sys_autostart.toggled.connect(self._on_sys_autostart_toggled)
        row2.addWidget(self.chk_sys_autostart)

        self.lbl_sys_state = QLabel("—")
        self.lbl_sys_state.setObjectName("serviceState")
        row2.addWidget(self.lbl_sys_state)

        row2.addStretch()

        for text, slot in [
            ("启动", lambda: self._sys_service_action("start")),
            ("停止", lambda: self._sys_service_action("stop")),
            ("重启", lambda: self._sys_service_action("restart")),
        ]:
            b = QPushButton(text)
            b.setToolTip("需要管理员权限，会弹出授权框")
            b.clicked.connect(slot)
            row2.addWidget(b)

        v.addLayout(row2)

        # ── 状态提示 ──
        self.lbl_service_hint = QLabel("")
        self.lbl_service_hint.setObjectName("hintLabel")
        self.lbl_service_hint.setWordWrap(True)
        v.addWidget(self.lbl_service_hint)

        return grp

    # ── 内存限制区块 ──
    def _build_memory_group(self) -> QWidget:
        """资源限制：实时图表 + 阈值设置（放最后）"""
        from ui.charts import ContainerRows, MemorySummary

        grp = QGroupBox("资源限制 · 内存")
        v = QVBoxLayout(grp)
        v.setContentsMargins(12, 12, 12, 10)
        v.setSpacing(10)

        # ── 实时图表 ──
        self.mem_chart = MemorySummary()
        v.addWidget(self.mem_chart)

        # ── 阈值设置（分段按钮，无滑块）──
        limit_box = QFrame()
        limit_box.setObjectName("limitBox")
        lb = QVBoxLayout(limit_box)
        lb.setContentsMargins(14, 10, 14, 10)
        lb.setSpacing(10)

        # 标题行
        head = QHBoxLayout()
        head.setSpacing(8)

        lbl_title = QLabel("启动阈值")
        lbl_title.setStyleSheet("font-weight: 600;")
        head.addWidget(lbl_title)

        self.lbl_mem_hint = QLabel("")
        self.lbl_mem_hint.setObjectName("hintLabel")
        head.addWidget(self.lbl_mem_hint, 1)

        self.btn_apply_limit = QPushButton("应用")
        self.btn_apply_limit.setFixedSize(64, 28)
        self.btn_apply_limit.setEnabled(False)
        self.btn_apply_limit.clicked.connect(self._apply_mem_limit)
        head.addWidget(self.btn_apply_limit)

        lb.addLayout(head)

        # 分段按钮组
        seg = QHBoxLayout()
        seg.setSpacing(8)

        self._preset_buttons: list[QPushButton] = []
        self._preset_values: list[int] = []

        # 先建空容器，实际按钮在 _rebuild_presets() 里按物理内存生成
        self._seg_layout = seg
        lb.addLayout(seg)

        # 内存总量获取后重建（延后到首次刷新）
        self._presets_built = False

        v.addWidget(limit_box)

        # ── 各容器实时占用 ──
        self.mem_bars = ContainerRows()
        v.addWidget(self.mem_bars)

        return grp

    def _rebuild_presets(self, host_gb: float) -> None:
        """按物理内存生成等距预设档位

        规则：
          · 只使用「整齐」的档位值（1/2/4/8/12/16/24/32/48/64/96/128 GB）
          · 全部档位 ≤ 物理内存
          · 至少 4 档，最多 7 档
          · 相邻档位比例尽量均匀，避免 2→4→6→8→10→14 这种跳跃

        示例：
          8G  物理 → 不限制 / 1G / 2G / 4G / 6G / 8G
          16G 物理 → 不限制 / 2G / 4G / 8G / 12G / 16G
          32G 物理 → 不限制 / 4G / 8G / 16G / 24G / 32G
          64G 物理 → 不限制 / 8G / 16G / 32G / 48G / 64G
        """
        if self._presets_built or host_gb <= 0:
            return

        host = int(host_gb)
        if host < 2:
            host = 2

        # 标准档位（等比 1-2-4 序列 + 3/4 位置）
        LADDER = [1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256]

        # 所有 ≤ 物理内存的档位
        usable = [v for v in LADDER if v <= host]
        if not usable:
            usable = [1]

        # 挑最多 6 档：优先保留最大档，再从大到小补齐
        MAX_SLOTS = 6
        if len(usable) <= MAX_SLOTS:
            picked = usable
        else:
            picked = usable[-MAX_SLOTS:]

        # 至少保证 4 档（物理内存很小时从 ladder 低端补）
        while len(picked) < 4:
            for v in LADDER:
                if v <= host and v not in picked:
                    picked.append(v)
                    break
            else:
                break
            picked.sort()

        self._preset_values = [0] + picked

        # 清空旧按钮
        while self._seg_layout.count():
            item = self._seg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._preset_buttons.clear()

        # 重建
        for i, gb in enumerate(self._preset_values):
            text = "不限制" if gb == 0 else f"{gb}G"
            b = QPushButton(text)
            b.setCheckable(True)
            b.setFixedHeight(30)
            b.setMinimumWidth(52)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setProperty("segFirst", i == 0)
            b.setProperty("segLast", i == len(self._preset_values) - 1)
            b.setProperty("segGb", gb)
            b.clicked.connect(lambda _=False, g=gb: self._select_preset(g))
            self._seg_layout.addWidget(b)
            self._preset_buttons.append(b)

        self._seg_layout.addStretch()
        self._presets_built = True

    def _select_preset(self, gb: int) -> None:
        """点击分段按钮"""
        if not self._preset_buttons:
            return
        for b in self._preset_buttons:
            b.setChecked(b.property("segGb") == gb)
        current = cfg.get_memory_limit_gb()
        self.btn_apply_limit.setEnabled(abs(gb - current) > 0.01)

    def _current_preset_gb(self) -> int:
        """当前选中的阈值"""
        for b in self._preset_buttons:
            if b.isChecked():
                return int(b.property("segGb"))
        return int(round(cfg.get_memory_limit_gb()))

    def _apply_mem_limit(self) -> None:
        """应用阈值设置"""
        value = float(self._current_preset_gb())
        cfg.set_memory_limit_gb(value)
        self.btn_apply_limit.setEnabled(False)
        self._refresh_memory()

        if value <= 0:
            self.status_message.emit("已取消内存限制")
            QMessageBox.information(
                self, "已保存",
                "已取消内存限制，容器启动不再检查内存。",
            )
            return

        self.status_message.emit(f"内存阈值已设为 {value:.0f} GB")

        r = QMessageBox.question(
            self, "阈值已保存",
            f"启动阈值已设为 {value:.0f} GB。\n\n"
            f"此设置仅影响本程序的启动前检查，"
            f"不需要重启 Podman 服务即可生效。\n\n"
            f"是否重启服务以应用所有配置？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._do_verified_restart()

    def _do_verified_restart(self) -> None:
        """重启服务并验证"""
        self.status_message.emit("正在重启 Podman 服务…")
        self.btn_apply_limit.setEnabled(False)

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

    def _refresh_memory(self) -> None:
        """刷新内存图表（每秒调用）"""
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

        # 首次拿到物理内存后生成预设
        if not self._presets_built and u["host_gb"] > 0:
            self._rebuild_presets(u["host_gb"])

        # 同步按钮（仅在未编辑时）
        if not self.btn_apply_limit.isEnabled() and self._preset_buttons:
            cur = int(round(u["limit_gb"]))
            closest = min(self._preset_values, key=lambda x: abs(x - cur))
            for b in self._preset_buttons:
                b.setChecked(b.property("segGb") == closest)

        # 提示
        if u["limit_gb"] <= 0:
            self.lbl_mem_hint.setText("未设置阈值（不检查）")
            self.lbl_mem_hint.setStyleSheet("")
        elif u["declared_gb"] > u["limit_gb"]:
            self.lbl_mem_hint.setText(
                f"⚠ 已超限 {u['declared_gb'] - u['limit_gb']:.2f} GB"
            )
            self.lbl_mem_hint.setStyleSheet("color: #e74c3c;")
        else:
            remain = u["limit_gb"] - u["declared_gb"]
            self.lbl_mem_hint.setText(
                f"剩余 {remain:.2f} GB 可分配 · 已用 {u['declared_gb']:.2f} GB"
            )
            self.lbl_mem_hint.setStyleSheet("")

    def _on_tab_changed(self, idx: int) -> None:
        if idx == 1:
            self._refresh_memory()
            self._refresh_service()

    def _tick_memory(self) -> None:
        """每秒刷新（仅服务页可见时）"""
        if not self.isVisible():
            return
        if self.tabs.currentIndex() != 1:
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
        """刷新服务状态"""
        st = cfg.autostart_status()

        blocked = self.chk_autostart.blockSignals(True)
        self.chk_autostart.setChecked(st["enabled"])
        self.chk_autostart.blockSignals(blocked)

        text, color, ikey = self._active_text(st["active_raw"])
        self._set_state_label(self.lbl_user_state, text, color, ikey)

        sst = cfg.system_service_status()
        blocked = self.chk_sys_autostart.blockSignals(True)
        self.chk_sys_autostart.setChecked(sst["enabled"])
        self.chk_sys_autostart.blockSignals(blocked)

        text2, color2, ikey2 = self._active_text(sst["active_raw"])
        self._set_state_label(self.lbl_sys_state, text2, color2, ikey2)

        if st["active"]:
            self.lbl_service_hint.setText(
                f"当前连接：{self.client.socket_path}"
            )
        else:
            self.lbl_service_hint.setText(
                "服务未运行。点击「启动」，或执行："
                "systemctl --user start podman.socket"
            )

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

