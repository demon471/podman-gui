"""虚拟机创建向导

参照 KDE 的向导风格（如 Kamoso / Konsole 新建配置）：
- 左侧步骤导航，右侧内容区
- 底部按钮固定（上一步 / 下一步 / 创建）
- 依赖缺失时引导安装，不直接失败

步骤：
  1. 依赖检测（LXC / SSH 工具）
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
from core.vm import container, deps, hostinfo, images, manager as vm_manager
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
    _sig_catalog = pyqtSignal(list)   # 动态镜像目录拉取完成

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.created_vm = None
        self._dep_results: list = []
        self._dep_ok = False
        self._busy = False
        self._tasks: list = []

        self.setWindowTitle("新建虚拟机")
        self.resize(880, 620)

        # 默认用户名（纯 Python，零开销）。不要在这里调
        # host_profile()：它串行跑 timedatectl / localectl /
        # resolvectl 三组 D-Bus 调用，systemd 会话冷启动时
        # 第一次要 1~2s，正是「第一次点开新建卡住」的主因。
        # 而向导实际只需要用户名这一项。
        self._host_username = hostinfo.username()
        # 镜像目录一次性加载：打开时显示「正在加载…」，目录就绪后
        # 只填充一次。此前是「静态精选秒开 → 后台拉到完整目录后
        # 原地重建列表和按钮」——用户正要点击时列表数据跳变
        # （大类按钮增减、行位置移动），观感很差。
        self._specs: list = []
        self._images_loaded = False

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
        self._sig_catalog.connect(
            self._on_catalog_ready, _Qt.ConnectionType.QueuedConnection
        )
        self._refresh_catalog_async()

        # 目录加载兜底：镜像站索引最坏 2×15s 超时，12s 还没回来
        # 就先用静态精选目录填充（离线场景也走这里）；迟到的
        # 动态目录会被 _on_catalog_ready 的一次性加载守卫忽略，
        # 保证列表绝不中途跳变。
        QTimer.singleShot(12000, self._catalog_timeout)

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

        self.ed_user = QLineEdit(self._host_username)
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

        # ── 镜像：两级选择（大类 → 版本）──
        # 52 个镜像平铺没法选。大类 = 发行版家族（横向档位按钮，
        # 推荐的在前），小类 = 该发行版的版本列表。
        g2 = QGroupBox("选择系统")
        v2 = QVBoxLayout(g2)
        v2.setSpacing(8)

        # 大类行
        fam_row = QHBoxLayout()
        fam_row.setSpacing(6)
        self._fam_buttons: list[QPushButton] = []
        self._fam_layout = fam_row
        v2.addLayout(fam_row)

        # 小类列表
        # 目录未就绪时列表为空 + 一行居中提示，就绪后填充一次。
        # 保持列表控件始终可见（不 hide），避免布局高度跳动。
        self.lbl_img_loading = QLabel("正在加载镜像目录…")
        self.lbl_img_loading.setObjectName("hintLabel")
        self.lbl_img_loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v2.addWidget(self.lbl_img_loading)

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
        ok, err = container.prerequisites()
        if not ok:
            self._fail(err)
            return False
        return True

    # ─────────────────────────────────────────
    # 步骤 1：依赖
    # ─────────────────────────────────────────
    def _check_deps(self) -> None:
        """后台检测依赖

        deps.check_all() 要串行跑 8 条 --version 子进程，
        冷启动时一次 fork+exec 几十毫秒，全放 UI 线程会把
        刚弹出的窗口冻住几百毫秒（首次打开尤其明显）。

        工作线程跑 subprocess 本身没有问题 —— 本文件的
        _install_deps、manager.create_vm 都在工作线程跑
        subprocess；之前观察到 abort 的根因是工作线程直接
        碰控件，run_async 的 QueuedConnection 已规避。
        """
        # 检测期间显示「正在检查…」，完成后按结果渲染
        self.lbl_dep.setText("正在检查运行环境…")
        self.lbl_dep.setStyleSheet("")
        self.dep_row_widget.show()
        self.btn_install.hide()

        self._spawn(
            deps.check_all,
            self._after_check_deps,
            lambda msg: self._fail(f"检测失败：{msg}"),
        )

    def _after_check_deps(self, results) -> None:
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
    # 大类（family）→ 显示名与排序
    FAMILIES = [
        ("alpine", "Alpine"),
        ("debian", "Debian 系"),
        ("rocky", "RHEL 系"),
        ("fedora", "Fedora 系"),
        ("arch", "Arch 系"),
        ("suse", "SUSE"),
        ("其它", "其它"),
    ]

    def _refresh_catalog_async(self) -> None:
        """后台拉完整镜像目录（含索引网络请求）

        无论成功失败都发 _sig_catalog：离线时 list_images()
        回落静态精选目录，照常填充一次。
        """
        from core.async_task import run_async

        def work():
            images.reset_discovered()
            return list(images.list_images())

        def done(specs):
            self._sig_catalog.emit(specs)

        self._tasks.append(
            run_async(work, on_done=done,
                      on_error=lambda _m: self._catalog_timeout(),
                      parent=self)
        )

    def _catalog_timeout(self) -> None:
        """兜底：目录迟迟未回（离线/网络慢）就用静态精选填充"""
        if not self._images_loaded:
            self._sig_catalog.emit(list(images.CATALOG.values()))

    def _on_catalog_ready(self, specs: list) -> None:
        """目录就绪：一次性填充列表与按钮

        一次性加载守卫：之后的迟到目录一律忽略，
        用户操作期间列表数据绝不跳变。
        """
        if self._images_loaded:
            return
        self._images_loaded = True

        self._specs = specs
        self.lbl_img_loading.hide()
        self._populate_images()

    def _populate_images(self) -> None:
        """按大类分组填充：先横向大类按钮，列表只显示该类版本"""
        if not self._specs:
            return

        # family → [specs]（保持动态目录的版本排序）
        groups: dict[str, list] = {}
        for spec in self._specs:
            groups.setdefault(spec.family, []).append(spec)

        # 大类按钮（只有实际有版本的家族才显示）
        fam_row = self._fam_layout
        for name, label in self.FAMILIES:
            if name != "其它" and name not in groups:
                continue
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("familyKey", name)
            btn.clicked.connect(
                lambda _c, k=name: self._select_family(k)
            )
            fam_row.addWidget(btn)
            self._fam_buttons.append(btn)
        fam_row.addStretch()

        # 默认选 Alpine（最轻最快）
        self._select_family("alpine")

    def _select_family(self, family: str) -> None:
        """切换大类：刷新版本列表 + 按钮选中态"""
        for b in self._fam_buttons:
            b.setChecked(b.property("familyKey") == family)

        self.img_list.blockSignals(True)
        self.img_list.clear()

        known = {f for f, _ in self.FAMILIES[:-1]}
        if family == "其它":
            items = [sp for sp in self._specs if sp.family not in known]
        else:
            items = [sp for sp in self._specs if sp.family == family]
        for spec in items:
            downloaded = images.rootfs_downloaded(spec)
            mark = "已下载" if downloaded else "未下载"
            it = QListWidgetItem(f"{spec.name}    [{mark}]")
            it.setData(Qt.ItemDataRole.UserRole, spec.key)
            self.img_list.addItem(it)
        self.img_list.blockSignals(False)

        if self.img_list.count():
            self.img_list.setCurrentRow(0)
        self._on_image_changed(0 if items else -1)

    def _current_spec(self):
        """当前选中的镜像

        必须从列表项的 UserRole 读 spec.key 再查目录——列表显示
        的是当前大类的过滤子集，行号不能直接当 self._specs 索引
        用（旧实现返回错镜像，选中第 2 行可能创建第 2 个发行版）。
        """
        it = self.img_list.currentItem()
        if it is None:
            return None
        return images.get_spec(it.data(Qt.ItemDataRole.UserRole))

    def _on_image_changed(self, _row: int) -> None:
        # 选中镜像只在依赖就绪时才允许创建
        self.btn_next.setEnabled(
            self._dep_ok and not self._busy
            and self._current_spec() is not None
        )



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
