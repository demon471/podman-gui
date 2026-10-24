"""虚拟机创建向导

参照 KDE 的向导风格（如 Kamoso / Konsole 新建配置）：
- 左侧步骤导航，右侧内容区
- 底部按钮固定（上一步 / 下一步 / 创建）
- 依赖缺失时引导安装，不直接失败

步骤：
  1. 依赖检测（QEMU / KVM / xorriso）
  2. 选择发行版（含下载状态、镜像大小）
  3. 资源配置（CPU / 内存 / 磁盘 / 用户名）
  4. 确认
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.vm import cloudinit, deps, hostinfo, images, manager as vm_manager
from core.vm import qemu as vm_qemu
from resources.icons import ICON_SIZE_TOOLBAR, icon
from ui.utils import fmt_size


class VMWizard(QDialog):
    """新建虚拟机向导"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.created_vm = None
        self._dep_results: list = []
        self._dep_ok = False
        self._busy = False
        self._tasks: list = []

        self.setWindowTitle("新建虚拟机")
        self.resize(880, 620)

        self._host = hostinfo.host_profile()
        self._specs = images.list_images()

        self._build_ui()
        self._goto_step(0)
        QTimer.singleShot(80, self._check_deps)

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # ── 左侧步骤列表 ──
        self.nav = QListWidget()
        self.nav.setObjectName("wizardNav")
        self.nav.setFixedWidth(196)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for i, title in enumerate(["依赖检查", "选择系统", "资源配置", "确认创建"]):
            it = QListWidgetItem(f"{i + 1}.  {title}")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.nav.addItem(it)
        self.nav.setCurrentRow(0)
        self.nav.setStyleSheet(
            "QListWidget#wizardNav { border: none; padding-top: 10px; }"
            "QListWidget#wizardNav::item { height: 36px; padding-left: 14px; }"
        )
        body.addWidget(self.nav)

        # ── 右侧内容 ──
        right = QVBoxLayout()
        right.setContentsMargins(18, 16, 18, 8)
        right.setSpacing(10)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_dep_page())
        self.stack.addWidget(self._build_image_page())
        self.stack.addWidget(self._build_resource_page())
        self.stack.addWidget(self._build_confirm_page())
        right.addWidget(self.stack, 1)

        self.lbl_error = QLabel("")
        self.lbl_error.setObjectName("statusError")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.hide()
        right.addWidget(self.lbl_error)

        body.addLayout(right, 1)
        root.addLayout(body, 1)

        # ── 底部按钮 ──
        bar = QFrame()
        bar.setObjectName("wizardBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(18, 10, 18, 10)

        self.lbl_step = QLabel("")
        self.lbl_step.setObjectName("hintLabel")
        bl.addWidget(self.lbl_step)
        bl.addStretch()

        self.btn_back = QPushButton("上一步")
        self.btn_back.clicked.connect(lambda: self._goto_step(self.stack.currentIndex() - 1))
        bl.addWidget(self.btn_back)

        self.btn_next = QPushButton("下一步")
        self.btn_next.setDefault(True)
        self.btn_next.clicked.connect(self._on_next)
        bl.addWidget(self.btn_next)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        bl.addWidget(self.btn_cancel)

        root.addWidget(bar)

    # ── 步骤 1：依赖 ──
    def _build_dep_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("检查运行环境")
        f = QFont(title.font())
        f.setPointSizeF(f.pointSizeF() + 1.5)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)

        hint = QLabel(
            "虚拟机依赖 QEMU 与 KVM 硬件加速。检测到缺失时可以直接安装。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.dep_list = QListWidget()
        self.dep_list.setAlternatingRowColors(True)
        v.addWidget(self.dep_list, 1)

        row = QHBoxLayout()
        self.btn_recheck = QPushButton("重新检测")
        self.btn_recheck.clicked.connect(self._check_deps)
        row.addWidget(self.btn_recheck)

        self.btn_install = QPushButton("安装缺失依赖…")
        self.btn_install.clicked.connect(self._install_deps)
        self.btn_install.hide()
        row.addWidget(self.btn_install)
        row.addStretch()
        v.addLayout(row)

        self.dep_log = QTextEdit()
        self.dep_log.setReadOnly(True)
        self.dep_log.setFixedHeight(110)
        self.dep_log.hide()
        v.addWidget(self.dep_log)

        return w

    # ── 步骤 2：镜像 ──
    def _build_image_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("选择操作系统")
        f = QFont(title.font())
        f.setPointSizeF(f.pointSizeF() + 1.5)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)

        hint = QLabel(
            "使用官方云镜像（cloud image），首次创建时自动下载并校验。\n"
            "轻量场景推荐 Alpine（guest 内仅约 150 MB 内存）。"
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.img_list = QListWidget()
        self.img_list.setAlternatingRowColors(True)
        self.img_list.currentRowChanged.connect(self._on_image_changed)
        v.addWidget(self.img_list, 1)

        self.lbl_img_detail = QLabel("")
        self.lbl_img_detail.setObjectName("hintLabel")
        self.lbl_img_detail.setWordWrap(True)
        v.addWidget(self.lbl_img_detail)

        self.lbl_img_note = QLabel("")
        self.lbl_img_note.setObjectName("hintLabel")
        self.lbl_img_note.setWordWrap(True)
        v.addWidget(self.lbl_img_note)

        # 下载进度
        self.prog = QProgressBar()
        self.prog.setFixedHeight(18)
        self.prog.hide()
        v.addWidget(self.prog)

        return w

    # ── 步骤 3：资源 ──
    def _build_resource_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("资源配置")
        f = QFont(title.font())
        f.setPointSizeF(f.pointSizeF() + 1.5)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)

        grp = QGroupBox("基本信息")
        form = QFormLayout(grp)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(10)

        self.ed_name = QLineEdit("")
        self.ed_name.setPlaceholderText("例如 ubuntu-dev（只能含字母数字和 - _）")
        form.addRow("名称", self.ed_name)

        self.ed_user = QLineEdit(self._host.username)
        form.addRow("用户名", self.ed_user)

        self.ed_pass = QLineEdit("12345")
        form.addRow("初始密码", self.ed_pass)

        v.addWidget(grp)

        grp2 = QGroupBox("硬件")
        form2 = QFormLayout(grp2)
        form2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form2.setSpacing(10)

        self.sp_cpu = QSpinBox()
        self.sp_cpu.setRange(1, 16)
        self.sp_cpu.setValue(2)
        self.sp_cpu.setSuffix(" vCPU")
        form2.addRow("处理器", self.sp_cpu)

        self.sp_mem = QSpinBox()
        self.sp_mem.setRange(128, 65536)
        self.sp_mem.setSingleStep(128)
        self.sp_mem.setValue(1024)
        self.sp_mem.setSuffix(" MB")
        self.sp_mem.valueChanged.connect(self._on_mem_changed)
        form2.addRow("内存", self.sp_mem)

        self.sp_disk = QSpinBox()
        self.sp_disk.setRange(2, 512)
        self.sp_disk.setValue(20)
        self.sp_disk.setSuffix(" GB")
        form2.addRow("磁盘", self.sp_disk)

        self.lbl_mem_hint = QLabel("")
        self.lbl_mem_hint.setObjectName("hintLabel")
        self.lbl_mem_hint.setWordWrap(True)
        form2.addRow("", self.lbl_mem_hint)

        chk = QLabel(
            "内存使用 virtio-balloon，空闲时可回收给宿主机，"
            "因此分配值可大于实际占用。"
        )
        chk.setObjectName("hintLabel")
        chk.setWordWrap(True)
        form2.addRow("", chk)

        v.addWidget(grp2)
        v.addStretch()

        self._update_mem_hint()
        return w

    # ── 步骤 4：确认 ──
    def _build_confirm_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        title = QLabel("确认创建")
        f = QFont(title.font())
        f.setPointSizeF(f.pointSizeF() + 1.5)
        f.setBold(True)
        title.setFont(f)
        v.addWidget(title)

        self.lbl_summary = QLabel("")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_summary.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        v.addWidget(self.lbl_summary)

        note = QLabel(
            "创建过程包括：下载并校验镜像 → 生成 SSH 密钥 → 生成 cloud-init 配置 "
            "→ 创建 COW 磁盘。首次创建需要下载镜像，可能耗时较长。\n\n"
            "创建完成后可随时启动；虚拟机内的用户名、时区、locale、DNS "
            "均继承自当前宿主机。"
        )
        note.setObjectName("hintLabel")
        note.setWordWrap(True)
        v.addWidget(note)
        v.addStretch()

        return w

    # ─────────────────────────────────────────
    # 步骤控制
    # ─────────────────────────────────────────
    def _goto_step(self, idx: int) -> None:
        idx = max(0, min(idx, self.stack.count() - 1))
        self.stack.setCurrentIndex(idx)
        self.nav.setCurrentRow(idx)
        self.lbl_step.setText(f"第 {idx + 1} / {self.stack.count()} 步")
        self.btn_back.setEnabled(idx > 0)
        self.lbl_error.hide()

        last = idx == self.stack.count() - 1
        self.btn_next.setText("创建" if last else "下一步")

        if idx == 0:
            self.btn_next.setEnabled(self._dep_ok)
        elif idx == 1:
            self.btn_next.setEnabled(self.img_list.currentRow() >= 0)
        elif idx == 2:
            self.btn_next.setEnabled(True)
        else:
            self.btn_next.setEnabled(not self._busy)
            self._build_summary()

    def _on_next(self) -> None:
        idx = self.stack.currentIndex()

        if idx == 0:
            if not self._dep_ok:
                return
            self._goto_step(1)
            return

        if idx == 1:
            if self.img_list.currentRow() < 0:
                return
            self._apply_image_defaults()
            self._goto_step(2)
            return

        if idx == 2:
            if not self._validate():
                return
            self._goto_step(3)
            return

        self._do_create()

    def _fail(self, msg: str) -> None:
        self.lbl_error.setText(msg)
        self.lbl_error.show()

    # ─────────────────────────────────────────
    # 步骤 1：依赖
    # ─────────────────────────────────────────
    def _check_deps(self) -> None:
        self.btn_recheck.setEnabled(False)
        self.dep_list.clear()
        it = QListWidgetItem("检测中…")
        self.dep_list.addItem(it)

        def work():
            return deps.check_all()

        def done(results):
            self._dep_results = results
            self._dep_ok = deps.ready_for_vm(results)
            self.btn_recheck.setEnabled(True)
            self._render_deps()
            self._goto_step(self.stack.currentIndex())

        def error(msg):
            self.btn_recheck.setEnabled(True)
            self._fail(f"检测失败：{msg}")

        self._spawn(work, done, error)

    def _render_deps(self) -> None:
        from PyQt6.QtGui import QColor

        self.dep_list.clear()
        missing = []

        for r in self._dep_results:
            if r.ok:
                mark = "✓"
                text = f"✓  {r.dep.name}"
                if r.detail:
                    text += f"    {r.detail}"
            else:
                mark = "✗" if r.dep.required else "!"
                text = f"{mark}  {r.dep.name}    {r.dep.purpose}"
                if r.detail:
                    text += f"（{r.detail}）"
                if r.dep.required:
                    missing.append(r)

            it = QListWidgetItem(text)
            if not r.ok:
                it.setForeground(
                    QColor("#e74c3c" if r.dep.required else "#f39c12")
                )
            self.dep_list.addItem(it)

        if missing:
            self.btn_install.show()
            self.btn_next.setEnabled(False)
            self._fail(
                f"缺少 {len(missing)} 项必需依赖，无法创建虚拟机。"
                "点击「安装缺失依赖」自动安装。"
            )
        else:
            self.btn_install.hide()
            self.btn_next.setEnabled(True)
            self.lbl_error.hide()
            self._populate_images()

    def _install_deps(self) -> None:
        distro = deps.detect_distro()
        pkgs = deps.missing_packages(self._dep_results, distro)
        if not pkgs:
            QMessageBox.information(
                self, "无需安装",
                "未识别到可通过包管理器安装的缺失项。\n"
                "请手动检查 KVM 权限（将用户加入 kvm 组）。",
            )
            return

        cmd = deps.install_command(distro, pkgs)
        ret = QMessageBox.question(
            self, "安装依赖",
            f"将在终端执行以下命令（需要 sudo 密码）：\n\n{cmd}\n\n继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        self.dep_log.show()
        self.dep_log.clear()
        self.dep_log.append(f"$ {cmd}\n")
        self.btn_install.setEnabled(False)

        def work():
            import subprocess

            # 在终端里跑，便于用户输入 sudo 密码
            term = _terminal_command(cmd)
            r = subprocess.run(
                term, capture_output=True, text=True, timeout=900
            )
            return r.returncode, (r.stdout or "") + (r.stderr or "")

        def done(res):
            rc, out = res
            self.dep_log.append(out[-4000:])
            self.btn_install.setEnabled(True)
            if rc == 0:
                self.dep_log.append("\n✓ 安装完成，重新检测…")
                self._check_deps()
            else:
                self.dep_log.append(f"\n✗ 安装失败（退出码 {rc}）")

        def error(msg):
            self.btn_install.setEnabled(True)
            self.dep_log.append(f"\n✗ {msg}")

        self._spawn(work, done, error)

    # ─────────────────────────────────────────
    # 步骤 2：镜像
    # ─────────────────────────────────────────
    def _populate_images(self) -> None:
        if self.img_list.count():
            return

        for spec in self._specs:
            downloaded = images.is_downloaded(spec)
            mark = "已下载" if downloaded else "未下载"
            it = QListWidgetItem(f"{spec.name}    [{mark}]")
            it.setData(Qt.ItemDataRole.UserRole, spec.key)
            self.img_list.addItem(it)

        # 默认选 Alpine（内存友好）
        for i, spec in enumerate(self._specs):
            if spec.family == "alpine":
                self.img_list.setCurrentRow(i)
                break
        else:
            self.img_list.setCurrentRow(0)

    def _current_spec(self):
        row = self.img_list.currentRow()
        if row < 0 or row >= len(self._specs):
            return None
        return self._specs[row]

    def _on_image_changed(self, _row: int) -> None:
        spec = self._current_spec()
        if spec is None:
            return

        p = images.image_path(spec)
        size = ""
        if p.exists():
            size = f"（本地 {fmt_size(p.stat().st_size)}）"

        self.lbl_img_detail.setText(
            f"<b>{spec.name}</b>{size}<br>"
            f"架构：{spec.family} 家族 · 默认用户：{spec.default_user}<br>"
            f"来源：{spec.url()}"
        )
        self.lbl_img_note.setText(spec.note or "")
        self.btn_next.setEnabled(True)

    def _apply_image_defaults(self) -> None:
        """按所选镜像调整默认资源配置"""
        spec = self._current_spec()
        if spec is None:
            return
        self.sp_cpu.setValue(max(1, spec.default_cpus))
        self.sp_mem.setValue(max(256, spec.default_mem_mb))
        self.sp_disk.setValue(max(4, spec.default_disk_gb))

        # 名称默认用镜像 key 简化形式
        if not self.ed_name.text().strip():
            self.ed_name.setText(spec.key.replace(".", "-").replace("_", "-"))

    # ─────────────────────────────────────────
    # 步骤 3：资源
    # ─────────────────────────────────────────
    def _on_mem_changed(self) -> None:
        self._update_mem_hint()

    def _update_mem_hint(self) -> None:
        from core.vm import memory as vm_memory

        want = self.sp_mem.value()
        try:
            hm = vm_memory.host_memory()
            avail = hm.available_mb
            if want > avail:
                self.lbl_mem_hint.setText(
                    f"⚠ 宿主机当前可用 {avail} MB，分配 {want} MB 可能失败。"
                )
                self.lbl_mem_hint.setObjectName("statusError")
            else:
                self.lbl_mem_hint.setText(
                    f"宿主机可用 {avail} MB / 共 {hm.total_mb} MB，分配后剩余约 "
                    f"{avail - want} MB。"
                )
                self.lbl_mem_hint.setObjectName("hintLabel")
            self.lbl_mem_hint.style().unpolish(self.lbl_mem_hint)
            self.lbl_mem_hint.style().polish(self.lbl_mem_hint)
        except Exception:
            self.lbl_mem_hint.setText("")

    def _validate(self) -> bool:
        name = self.ed_name.text().strip()
        if not name:
            self._fail("请填写虚拟机名称。")
            return False
        if "/" in name or name.startswith(".") or " " in name:
            self._fail("名称不能包含空格、/ 或以 . 开头。")
            return False
        if vm_manager.name_taken(name):
            self._fail(f"名称「{name}」已被占用，请换一个。")
            return False
        if not self.ed_user.text().strip():
            self._fail("请填写用户名。")
            return False
        if not cloudinit.iso_tool_available():
            self._fail(
                "缺少 ISO 生成工具（xorriso / genisoimage），无法生成 cloud-init 配置。"
            )
            return False
        return True

    # ─────────────────────────────────────────
    # 步骤 4：确认
    # ─────────────────────────────────────────
    def _build_summary(self) -> None:
        spec = self._current_spec()
        img = spec.name if spec else "-"
        downloaded = images.is_downloaded(spec) if spec else False

        rows = [
            ("名称", self.ed_name.text().strip()),
            ("操作系统", img + ("" if downloaded else "（需下载）")),
            ("处理器", f"{self.sp_cpu.value()} vCPU"),
            ("内存", f"{self.sp_mem.value()} MB"),
            ("磁盘", f"{self.sp_disk.value()} GB"),
            ("用户名", self.ed_user.text().strip()),
            ("初始密码", self.ed_pass.text()),
            ("时区 / 语言", f"{self._host.timezone} · {self._host.locale}"),
            ("DNS", "、".join(self._host.dns) or "（自动）"),
        ]

        html = "<table cellspacing='0' cellpadding='4'>"
        for k, val in rows:
            html += (
                f"<tr><td style='color:gray'>{k}</td>"
                f"<td><b>{val}</b></td></tr>"
            )
        html += "</table>"
        self.lbl_summary.setText(html)

    # ─────────────────────────────────────────
    # 创建
    # ─────────────────────────────────────────
    def _do_create(self) -> None:
        if self._busy:
            return

        name = self.ed_name.text().strip()
        spec = self._current_spec()
        if spec is None:
            return

        self._busy = True
        self.btn_next.setEnabled(False)
        self.btn_back.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.prog.show()
        self.prog.setRange(0, 0)
        self._fail("正在创建，首次创建需要下载镜像，请耐心等待…")

        def on_log(msg: str) -> None:
            self.lbl_error.setText(msg)

        def on_progress(cur: int, total: int) -> None:
            if total > 0:
                self.prog.setRange(0, total)
                self.prog.setValue(cur)

        def work():
            return vm_manager.create_vm(
                name=name,
                image_key=spec.key,
                memory_mb=self.sp_mem.value(),
                cpus=self.sp_cpu.value(),
                disk_gb=self.sp_disk.value(),
                username=self.ed_user.text().strip(),
                on_log=on_log,
                on_progress=on_progress,
            )

        def done(res):
            vm, err = res
            self._busy = False
            self.btn_cancel.setEnabled(True)
            self.prog.hide()

            if vm is None:
                self.btn_next.setEnabled(True)
                self.btn_back.setEnabled(True)
                self._fail(f"创建失败：{err}")
                QMessageBox.warning(self, "创建失败", err)
                return

            self.created_vm = vm
            self.accept()

        def error(msg):
            self._busy = False
            self.prog.hide()
            self.btn_next.setEnabled(True)
            self.btn_back.setEnabled(True)
            self.btn_cancel.setEnabled(True)
            self._fail(f"创建失败：{msg}")
            QMessageBox.warning(self, "创建失败", msg)

        self._spawn(work, done, error)

    # ─────────────────────────────────────────
    # 线程
    # ─────────────────────────────────────────
    def _spawn(self, work, done, error) -> None:
        task = run_async(work, on_done=done, on_error=error, parent=self)
        self._tasks.append(task)

    def closeEvent(self, e) -> None:
        if self._busy:
            ret = QMessageBox.question(
                self, "正在创建",
                "虚拟机正在创建中，关闭窗口会中断创建。确定关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        super().closeEvent(e)


# ─────────────────────────────────────────
# 辅助
# ─────────────────────────────────────────
def _terminal_command(cmd: str) -> list[str]:
    """找到可用于执行 sudo 命令的终端（便于输入密码）"""
    import shutil

    for term, args in [
        ("konsole", ["-e", "bash", "-c", cmd + "; echo; read -p '按回车关闭…'"]),
        ("gnome-terminal", ["--", "bash", "-c", cmd + "; read -p '按回车关闭…'"]),
        ("xterm", ["-e", "bash", "-c", cmd + "; read -p '按回车关闭…'"]),
    ]:
        if shutil.which(term):
            return [term] + args
    # 兜底：无终端时直接执行（sudo 会因无 tty 失败）
    return ["bash", "-c", cmd]
