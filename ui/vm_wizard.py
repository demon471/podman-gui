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
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.vm import cloudinit, deps, hostinfo, images, manager as vm_manager
from core.vm import qemu as vm_qemu
from resources.icons import ICON_SIZE_TOOLBAR, icon


class VMWizard(QDialog):
    """新建虚拟机向导"""

    # 下载进度/日志必须跨线程传回 UI 线程。
    # 直接在工作线程里调 self.prog.setValue() / self.lbl_error.setText()
    # 会触发 Qt 的 QMessageLogger::fatal → abort（进程闪退，
    # 没有 Python traceback）。这两个信号用 Queued 连接，
    # 由 Qt 自动投递到 UI 线程执行。
    _sig_progress = pyqtSignal(int, int)
    _sig_log = pyqtSignal(str)

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

        # 下载回调 → 信号 → UI 线程更新控件
        # 用 QueuedConnection 显式声明：worker 线程 emit，
        # UI 线程执行槽，绝不跨线程碰 QWidget。
        from PyQt6.QtCore import Qt as _Qt

        self._sig_progress.connect(
            self._on_dl_progress, _Qt.ConnectionType.QueuedConnection
        )
        self._sig_log.connect(
            self._on_dl_log, _Qt.ConnectionType.QueuedConnection
        )

        self._goto_step(0)
        QTimer.singleShot(80, self._check_deps)

    # ─────────────────────────────────────────
    # 下载回调（UI 线程执行）
    # ─────────────────────────────────────────
    def _on_dl_progress(self, cur: int, total: int) -> None:
        """更新进度条（在 UI 线程）"""
        try:
            if total > 0:
                self.prog.setRange(0, total)
                self.prog.setValue(cur)
        except RuntimeError:
            # 向导已被关闭，控件已销毁
            pass

    def _on_dl_log(self, msg: str) -> None:
        """更新日志标签（在 UI 线程）"""
        try:
            self.lbl_error.setText(msg)
        except RuntimeError:
            pass

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)


        # ── 左侧步骤列表 ──
        # 单步向导：不需要左侧步骤导航，直接铺内容区
        right = QVBoxLayout()
        right.setContentsMargins(18, 16, 18, 8)
        right.setSpacing(10)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_config_page())
        right.addWidget(self.stack, 1)

        self.lbl_error = QLabel("")
        self.lbl_error.setObjectName("statusError")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.hide()
        right.addWidget(self.lbl_error)

        root.addLayout(right, 1)

        # ── 底部按钮 ──
        bar = QFrame()
        bar.setObjectName("wizardBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(18, 10, 18, 10)

        bl.addStretch()

        self.btn_next = QPushButton("下一步")
        self.btn_next.setDefault(True)
        self.btn_next.clicked.connect(self._on_next)
        bl.addWidget(self.btn_next)

        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self.reject)
        bl.addWidget(self.btn_cancel)

        root.addWidget(bar)

    # ── 新建虚拟机（一页搞定）──
    def _build_config_page(self) -> QWidget:
        """镜像选择 + 名称，合并为一页

        磁盘与内存全部用默认值，不再让用户选：
          · 磁盘：所有虚拟机共用一个稀疏基础镜像，按需增长
          · 内存：用镜像推荐值，与容器共用设置里的内存预算
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # ── 名称 ──
        g1 = QGroupBox("名称")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        f1.setSpacing(8)

        self.ed_name = QLineEdit("")
        self.ed_name.setPlaceholderText("例如 ubuntu-dev、my-server")
        f1.addRow("虚拟机名称", self.ed_name)

        self.ed_user = QLineEdit(self._host.username)
        self.ed_user.setToolTip("虚拟机内的登录用户名")
        f1.addRow("登录用户名", self.ed_user)

        v.addWidget(g1)

        # ── 依赖提示（检测中/缺失时显示）──
        # 依赖提示行：就绪时一行绿字，缺失时列出缺什么 + 安装按钮
        self.dep_row_widget = QWidget()
        dep_row = QHBoxLayout(self.dep_row_widget)
        dep_row.setContentsMargins(0, 0, 0, 0)
        dep_row.setSpacing(8)

        self.lbl_dep = QLabel("正在检查运行环境…")
        self.lbl_dep.setObjectName("hintLabel")
        self.lbl_dep.setWordWrap(True)
        dep_row.addWidget(self.lbl_dep, 1)

        self.btn_install = QPushButton("安装组件…")
        self.btn_install.setIcon(
            icon("settings", ICON_SIZE_TOOLBAR.width())
        )
        self.btn_install.clicked.connect(self._install_deps)
        self.btn_install.hide()
        dep_row.addWidget(self.btn_install)

        v.addWidget(self.dep_row_widget)

        # ── 镜像 ──
        g2 = QGroupBox("选择系统")
        v2 = QVBoxLayout(g2)
        v2.setSpacing(8)

        self.img_list = QListWidget()
        self.img_list.setAlternatingRowColors(True)
        self.img_list.currentRowChanged.connect(self._on_image_changed)
        v2.addWidget(self.img_list, 1)


        v.addWidget(g2, 1)

        self.prog = QProgressBar()
        self.prog.setFixedHeight(18)
        self.prog.hide()
        v.addWidget(self.prog)

        return w


    # ─────────────────────────────────────────
    # 步骤控制
    # ─────────────────────────────────────────
    def _goto_step(self, idx: int) -> None:
        """切页（单步向导，保留以兼容旧调用）"""
        self.stack.setCurrentIndex(0)
        self.lbl_error.hide()
        self.btn_next.setText("创建")
        self.btn_next.setEnabled(not self._busy and self._dep_ok)

    def _on_next(self) -> None:
        if not self._dep_ok:
            return
        if not self._validate():
            return
        self._do_create()

    def _fail(self, msg: str) -> None:
        self.lbl_error.setText(msg)
        self.lbl_error.show()

    def _validate(self) -> bool:
        """创建前校验（名称、用户名、镜像）"""
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
                "缺少 ISO 生成工具（xorriso / genisoimage），"
                "无法生成 cloud-init 配置。"
            )
            return False
        return True

    # ─────────────────────────────────────────
    # 步骤 1：依赖
    # ─────────────────────────────────────────
    def _check_deps(self) -> None:
        """检测依赖

        刻意在 UI 线程执行（不用 run_async）：deps.check_all() 内部
        会跑多条 subprocess（qemu-system-x86_64 --version、test -r /dev/kvm
        等）。实测把这些 subprocess 放在 BackgroundTask 的工作线程里执行，
        会与 Qt 事件循环冲突并触发 QMessageLogger::fatal，整个进程 abort。
        检测本身很快（几十毫秒），在 UI 线程延迟到界面渲染完再跑即可。
        """
        # 检测开始时保持隐藏；只有缺失时才显示提示行
        self.dep_row_widget.hide()
        QTimer.singleShot(0, self._do_check_deps)

    def _do_check_deps(self) -> None:
        try:
            results = deps.check_all()
        except Exception as e:
            self._fail(f"检测失败：{e}")
            return

        self._dep_results = results
        self._dep_ok = deps.ready_for_vm(results)
        self._render_deps()
        self._goto_step(self.stack.currentIndex())

    def _render_deps(self) -> None:
        """把依赖检测结果以行内提示显示

        全部就绪 → 显示一行绿色「运行环境就绪」，其余时间不打扰。
        有缺失   → 显示哪些缺、怎么装，并禁用创建按钮。
        """
        from PyQt6.QtGui import QColor

        missing = [r for r in self._dep_results
                   if not r.ok and r.dep.required]
        optional = [r for r in self._dep_results
                    if not r.ok and not r.dep.required]

        if not missing:
            # 环境就绪：整行隐藏，不占空间也不打扰
            self.dep_row_widget.hide()
            self.btn_next.setEnabled(not self._busy)
            self.lbl_error.hide()
            self._populate_images()
            return

        # 有必需依赖缺失：显示提示行和安装按钮
        self.dep_row_widget.show()
        self.btn_install.show()
        self.btn_next.setEnabled(False)

        lines = [f"✗ 缺少 {len(missing)} 项必需组件："]
        for r in missing:
            lines.append(f"   · {r.dep.name}（{r.dep.purpose}）")
        if optional:
            lines.append(
                f"   另有 {len(optional)} 项可选组件未安装。"
            )
        lines.append("点击「安装组件」自动安装。")

        self.lbl_dep.setText("\n".join(lines))
        self.lbl_dep.setStyleSheet("color: #e74c3c;")
        self._fail(
            f"缺少 {len(missing)} 项必需组件，无法创建虚拟机。"
            "点击「安装组件」自动安装。"
        )

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

        self.lbl_dep.setText("正在安装组件…（需要输入 sudo 密码）")
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
            self.btn_install.setEnabled(True)
            if rc == 0:
                self.lbl_dep.setText("✓ 安装完成，正在重新检测…")
                self._check_deps()
            else:
                tail = out.strip().splitlines()[-3:]
                self.lbl_dep.setText(
                    f"✗ 安装失败（退出码 {rc}）\n"
                    + "\n".join(tail)
                )

        def error(msg):
            self.btn_install.setEnabled(True)
            self.lbl_dep.setText(f"✗ {msg}")

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
        # 界面不再显示镜像详情，选中变化只需刷新按钮状态
        if self._current_spec() is not None:
            self.btn_next.setEnabled(True)



    # ─────────────────────────────────────────
    # 步骤 4：确认
    # ─────────────────────────────────────────

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
        self.btn_cancel.setEnabled(False)
        self.prog.show()
        self.prog.setRange(0, 0)
        self._fail("正在创建，首次创建需要下载镜像，请耐心等待…")

        # 这两个回调在后台线程被调用 —— 只 emit 信号，
        # 绝不直接碰控件（会 Qt fatal → 闪退）
        def on_log(msg: str) -> None:
            self._sig_log.emit(msg)

        def on_progress(cur: int, total: int) -> None:
            self._sig_progress.emit(cur, total)

        def work():
            # 只传名称、镜像、用户名 —— 磁盘与内存全部用默认值
            return vm_manager.create_vm(
                name=name,
                image_key=spec.key,
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
                self._fail(f"创建失败：{err}")
                QMessageBox.warning(self, "创建失败", err)
                return

            self.created_vm = vm
            self.accept()

        def error(msg):
            self._busy = False
            self.prog.hide()
            self.btn_next.setEnabled(True)
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

        # 关键：等后台任务结束再销毁窗口。
        # 任务 parent=self，若窗口先死，工作线程随后 emit 的
        # 进度信号会命中已销毁的 QObject → Qt fatal → abort（闪退）。
        # 断点续传场景必现：下载慢，用户等不住关窗口。
        # 下载线程内已有 cancel_flag 检查，最多等 2 秒。
        from core.async_task import shutdown_tasks

        shutdown_tasks(self._tasks, timeout_ms=2000)
        self._tasks.clear()

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
