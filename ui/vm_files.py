"""虚拟机文件浏览面板

走 SFTP 浏览 VM 内文件系统，支持：
  · 双击进入目录 / 上级
  · 面包屑路径导航
  · 上传 / 下载
  · 新建目录、重命名、删除
  · 双击文本文件查看内容

所有远程操作跑在后台线程（SSH 握手 + 网络 IO 会卡 UI）。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.async_task import run_async
from core.notify import notifier
from core.vm import files as vm_files
from resources.icons import ICON_SIZE_BUTTON, icon


class FilePanel(QWidget):
    """VM 内文件浏览"""

    # 忙碌状态变化（外层据此禁用/恢复「关闭」按钮）
    busy_changed = pyqtSignal(bool)

    status = pyqtSignal(str)

    def __init__(self, vm, parent: QWidget | None = None):
        super().__init__(parent)
        self.vm = vm
        self.cwd = "/"
        self._entries: list[vm_files.RemoteEntry] = []
        self._session: vm_files.SftpSession | None = None
        self._tasks: list = []
        self._busy = False
        # 导航请求序号：用于丢弃过期响应（快速连点时只认最后一次）
        self._nav_seq = 0

        self._build_ui()

        # 打开面板就加载当前目录 —— 否则表格一直是空的，
        # 用户看到的「文件页没反应」就是这个原因。
        # 用 singleShot 让界面先显示出来，再后台拉数据。
        from PyQt6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self.navigate(self.cwd))

    # ─────────────────────────────────────────
    # UI
    # ─────────────────────────────────────────
    def _build_ui(self) -> None:
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 12, 10, 10)
        v.setSpacing(8)

        # ── 路径栏 ──
        nav = QHBoxLayout()
        nav.setSpacing(4)

        self.btn_up = QPushButton()
        self.btn_up.setIcon(icon("up", ICON_SIZE_BUTTON.width()))
        self.btn_up.setToolTip("上级目录")
        self.btn_up.setFixedWidth(34)
        self.btn_up.clicked.connect(self._go_up)
        nav.addWidget(self.btn_up)

        self.btn_home = QPushButton()
        self.btn_home.setIcon(icon("home", ICON_SIZE_BUTTON.width()))
        self.btn_home.setToolTip("家目录")
        self.btn_home.setFixedWidth(34)
        self.btn_home.clicked.connect(self._go_home)
        nav.addWidget(self.btn_home)

        self.ed_path = QLineEdit()
        self.ed_path.setPlaceholderText("/")
        self.ed_path.returnPressed.connect(self._go_typed)
        nav.addWidget(self.ed_path, 1)

        self.btn_refresh = QPushButton()
        self.btn_refresh.setIcon(icon("refresh", ICON_SIZE_BUTTON.width()))
        self.btn_refresh.setToolTip("刷新")
        self.btn_refresh.setFixedWidth(34)
        self.btn_refresh.clicked.connect(lambda: self.navigate(self.cwd))
        nav.addWidget(self.btn_refresh)

        v.addLayout(nav)

        # ── 文件列表 ──
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["名称", "大小", "权限", "修改时间"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(self._on_double_click)

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.table, 1)

        # ── 操作栏 ──
        row = QHBoxLayout()
        row.setSpacing(6)

        self.btn_upload = QPushButton("上传")
        self.btn_upload.setIcon(icon("upload", ICON_SIZE_BUTTON.width()))
        self.btn_upload.clicked.connect(self._upload)
        row.addWidget(self.btn_upload)

        self.btn_download = QPushButton("下载")
        self.btn_download.setIcon(icon("download", ICON_SIZE_BUTTON.width()))
        self.btn_download.clicked.connect(self._download)
        row.addWidget(self.btn_download)

        row.addSpacing(10)

        self.btn_mkdir = QPushButton("新建目录")
        self.btn_mkdir.setIcon(icon("add", ICON_SIZE_BUTTON.width()))
        self.btn_mkdir.clicked.connect(self._mkdir)
        row.addWidget(self.btn_mkdir)

        self.btn_rename = QPushButton("重命名")
        self.btn_rename.setIcon(icon("edit", ICON_SIZE_BUTTON.width()))
        self.btn_rename.clicked.connect(self._rename)
        row.addWidget(self.btn_rename)

        self.btn_delete = QPushButton("删除")
        self.btn_delete.setIcon(
            icon("delete", ICON_SIZE_BUTTON.width(), danger=True)
        )
        self.btn_delete.clicked.connect(self._delete)
        row.addWidget(self.btn_delete)

        row.addStretch()
        v.addLayout(row)

    # ─────────────────────────────────────────
    # 导航
    # ─────────────────────────────────────────
    def navigate(self, path: str) -> None:
        """进入指定目录（后台加载）

        允许并发导航：用户快速连点「上一级」或切目录时，
        每次请求都发出去，只采用**最后一次**的结果。
        用递增序号识别过期响应，避免旧目录数据覆盖新目录。

        之前用「busy 就丢弃」的写法，导致快速操作被静默忽略，
        用户看到的是按钮失灵（点了没反应）。
        """
        self._nav_seq += 1
        seq = self._nav_seq

        self._set_busy(True)

        def work():
            # 复用连接（同一面板内一直用同一个 session）
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
                # 首次连接顺手检查 home 可写性并自动修复 ——
                # 云镜像常把 /home/<user> 留成 root 所有，
                # 导致上传/新建目录全部失败（浏览却正常）。
                # 放在这里做一次，用户不会遇到「莫名其妙写不进去」。
                try:
                    self._session.ensure_home_writable()
                except Exception:
                    pass
            return self._session.listdir(path)

        def done(entries):
            # 过期响应：期间用户已经切到别的目录，丢弃
            if seq != self._nav_seq:
                return
            self._set_busy(False)
            self.cwd = path
            self.ed_path.setText(path)
            self._entries = entries
            self._fill_table(entries)
            self.status.emit(f"{len(entries)} 项")

        def error(msg):
            if seq != self._nav_seq:
                return
            self._set_busy(False)
            m = str(msg)
            self.status.emit(f"读取失败: {m}")
            # 权限类错误提示修复（home 目录 owner 被镜像置成 root 的情况）
            if "Permission denied" in m or "EACCES" in m:
                self._offer_fix_perms(path)

        self._spawn(work, done, error)

    def _go_up(self) -> None:
        if self.cwd == "/":
            return
        parent = str(Path(self.cwd).parent)
        self.navigate(parent or "/")

    def _go_home(self) -> None:
        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            return self._session.home()

        def done(path):
            self.navigate(path or "/")

        self._spawn(work, done, lambda m: self.status.emit(f"失败: {m}"))

    def _go_typed(self) -> None:
        p = self.ed_path.text().strip()
        if p:
            self.navigate(p)

    # ─────────────────────────────────────────
    # 表格
    # ─────────────────────────────────────────
    def _fill_table(self, entries: list[vm_files.RemoteEntry]) -> None:
        import time

        self.table.setRowCount(0)
        self.table.setRowCount(len(entries))

        for r, e in enumerate(entries):
            # 名称（目录带图标）
            name_item = QTableWidgetItem(e.name)
            if e.is_dir:
                name_item.setIcon(icon("folder"))
            else:
                name_item.setIcon(icon("file"))
            name_item.setData(Qt.ItemDataRole.UserRole, e)
            self.table.setItem(r, 0, name_item)

            self.table.setItem(r, 1, QTableWidgetItem(e.size_text))
            self.table.setItem(r, 2, QTableWidgetItem(e.perms_text))

            mt = ""
            if e.mtime:
                mt = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(e.mtime)
                )
            self.table.setItem(r, 3, QTableWidgetItem(mt))

    def _selected(self) -> list[vm_files.RemoteEntry]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for r in rows:
            item = self.table.item(r, 0)
            if item:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _on_double_click(self, index) -> None:
        item = self.table.item(index.row(), 0)
        if not item:
            return
        e: vm_files.RemoteEntry = item.data(Qt.ItemDataRole.UserRole)

        if e.is_dir:
            self.navigate(e.path)
        elif e.size <= 512 * 1024:
            self._view_file(e)

    # ─────────────────────────────────────────
    # 文件操作
    # ─────────────────────────────────────────
    def _view_file(self, entry: vm_files.RemoteEntry) -> None:
        """查看文本文件内容"""
        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            return self._session.read_text(entry.path)

        def done(content):
            dlg = QDialog(self)
            dlg.setWindowTitle(entry.name)
            dlg.resize(760, 560)
            lay = QVBoxLayout(dlg)

            te = QPlainTextEdit()
            te.setPlainText(content)
            te.setReadOnly(True)
            te.setFontFamily("monospace")
            lay.addWidget(te)

            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
            bb.rejected.connect(dlg.reject)
            lay.addWidget(bb)

            dlg.exec()

        self._spawn(work, done, lambda m: self.status.emit(f"读取失败: {m}"))

    @staticmethod
    def _file_dialog_kwargs() -> dict:
        """强制使用 Qt 内置文件对话框

        为什么要这么做：
        KDE Wayland 下 Qt 默认走 XDG Desktop Portal 的原生对话框。
        PyInstaller 打包后，portal 通信常因缺少 D-Bus 服务名或
        权限而静默失败 —— 表现为「对话框打开但选不中文件、
        确认后返回空列表」。

        用 Qt 自己的对话框（非原生）在所有环境下都可靠，
        外观也由本应用的 QSS 统一控制。
        """
        return {"options": QFileDialog.Option.DontUseNativeDialog}

    def _upload(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要上传的文件", str(Path.home()),
            "所有文件 (*)",
            **self._file_dialog_kwargs(),
        )
        if not files:
            return

        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            n = 0
            for f in files:
                remote = f"{self.cwd.rstrip('/')}/{Path(f).name}"
                self._session.put(f, remote)
                n += 1
            return n

        def done(n):
            self.status.emit(f"已上传 {n} 个文件")
            self.navigate(self.cwd)

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    def _download(self) -> None:
        sel = self._selected()
        if not sel:
            self.status.emit("请先在列表中选择要下载的文件或目录")
            return

        target = QFileDialog.getExistingDirectory(
            self, "选择保存位置",
            str(Path.home() / "下载"),
            **self._file_dialog_kwargs(),
        )
        if not target:
            return

        files = [e for e in sel if not e.is_dir]
        dirs = [e for e in sel if e.is_dir]

        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            n = 0
            # 普通文件直接下
            for e in files:
                local = str(Path(target) / e.name)
                self._session.get(e.path, local)
                n += 1
            # 目录递归下（保持相对结构）
            for d in dirs:
                n += self._download_dir(d.path, Path(target) / d.name)
            return n

        def done(n):
            # 必须解除 busy —— 否则所有按钮永久变灰，
            # 用户只能关掉窗口重开（之前就是这样）
            self._set_busy(False)
            self.status.emit(f"已下载 {n} 项到 {target}")
            notifier().info("下载完成", f"{n} 项 → {target}")

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    def _download_dir(self, remote: str, local: Path) -> int:
        """递归下载目录（在工作线程里执行）"""
        count = 0
        try:
            local.mkdir(parents=True, exist_ok=True)
            entries = self._session.listdir(remote)
        except Exception:
            return 0
        for e in entries:
            if e.is_dir:
                count += self._download_dir(e.path, local / e.name)
            else:
                try:
                    self._session.get(e.path, str(local / e.name))
                    count += 1
                except Exception:
                    pass
        return count

    def _mkdir(self) -> None:
        name, ok = QInputDialog.getText(self, "新建目录", "目录名：")
        if not ok or not name.strip():
            return

        target = f"{self.cwd.rstrip('/')}/{name.strip()}"

        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            self._session.mkdir(target)
            return target

        def done(_):
            self.status.emit(f"已创建 {name}")
            self.navigate(self.cwd)

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    def _rename(self) -> None:
        sel = self._selected()
        if len(sel) != 1:
            self.status.emit("请选择一个文件重命名")
            return

        e = sel[0]
        new, ok = QInputDialog.getText(
            self, "重命名", "新名称：", text=e.name
        )
        if not ok or not new.strip() or new.strip() == e.name:
            return

        target = f"{self.cwd.rstrip('/')}/{new.strip()}"

        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            self._session.rename(e.path, target)
            return target

        def done(_):
            self.status.emit(f"已重命名为 {new}")
            self.navigate(self.cwd)

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    def _delete(self) -> None:
        sel = self._selected()
        if not sel:
            return

        names = "、".join(e.name for e in sel[:3])
        if len(sel) > 3:
            names += f" 等 {len(sel)} 项"

        r = QMessageBox.question(
            self,
            "确认删除",
            f"删除 {names}？\n\n此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        def work():
            if self._session is None:
                self._session = vm_files.SftpSession(self.vm)
            n = 0
            errors = []
            for e in sel:
                try:
                    if e.is_dir:
                        self._session.rmdir(e.path)
                    else:
                        self._session.remove(e.path)
                    n += 1
                except Exception as ex:
                    errors.append(f"{e.name}: {ex}")
            return n, errors

        def done(res):
            n, errors = res
            self.status.emit(f"已删除 {n} 项")
            if errors:
                QMessageBox.warning(
                    self, "部分失败", "\n".join(errors[:5])
                )
            self.navigate(self.cwd)

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    # ─────────────────────────────────────────
    # 右键菜单
    # ─────────────────────────────────────────
    def _context_menu(self, pos) -> None:
        sel = self._selected()

        m = QMenu(self)

        if sel and len(sel) == 1 and sel[0].is_dir:
            a = QAction(icon("folder"), "打开", self)
            a.triggered.connect(lambda: self.navigate(sel[0].path))
            m.addAction(a)

        if sel and len(sel) == 1 and not sel[0].is_dir:
            a = QAction(icon("file"), "查看", self)
            a.triggered.connect(lambda: self._view_file(sel[0]))
            m.addAction(a)

        if sel and any(not e.is_dir for e in sel):
            a = QAction(icon("download"), "下载", self)
            a.triggered.connect(self._download)
            m.addAction(a)

        if sel:
            a = QAction(icon("edit"), "重命名", self)
            a.triggered.connect(self._rename)
            m.addAction(a)

        m.addSeparator()

        a = QAction(icon("upload"), "上传到此处", self)
        a.triggered.connect(self._upload)
        m.addAction(a)

        a = QAction(icon("add"), "新建目录", self)
        a.triggered.connect(self._mkdir)
        m.addAction(a)

        a = QAction(icon("refresh"), "刷新", self)
        a.triggered.connect(lambda: self.navigate(self.cwd))
        m.addAction(a)

        if sel:
            m.addSeparator()
            a = QAction(icon("copy"), "复制路径", self)
            a.triggered.connect(self._copy_paths)
            m.addAction(a)

            a = QAction(icon("delete", danger=True), "删除", self)
            a.triggered.connect(self._delete)
            m.addAction(a)

        m.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_paths(self) -> None:
        sel = self._selected()
        if sel:
            QGuiApplication.clipboard().setText(
                "\n".join(e.path for e in sel)
            )
            self.status.emit(f"已复制 {len(sel)} 个路径")

    # ─────────────────────────────────────────
    # 工具
    # ─────────────────────────────────────────
    def _offer_fix_perms(self, path: str) -> None:
        """home 目录权限修复提示

        云镜像（cloudimg）里 /home/<user> 有时属于 root 且不可写，
        cloud-init 只在首次启动时修，老 VM 可能漏掉。
        弹窗一键修复（需要免密 sudo，与 KSM/DNS 自动配置相同条件）。
        """
        from core.vm import ssh as vm_ssh

        r = QMessageBox.question(
            self, "无法访问",
            "该目录无法写入，可能是 home 目录权限被镜像置错。\n"
            "是否执行一键修复？（需要 VM 内 sudo 可用）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if r != QMessageBox.StandardButton.Yes:
            return

        user = self.vm.spec.username

        def work():
            fix = (
                f"sudo -n chown {user}:{user} /home/{user} && "
                f"sudo -n chmod 755 /home/{user} && "
                f"sudo -n chmod 700 /home/{user}/.ssh 2>/dev/null; "
                f"echo FIX_DONE"
            )
            ok, out = vm_ssh.run_command(self.vm, fix, timeout=20)
            return ok, out

        def done(res):
            ok, out = res
            if ok and "FIX_DONE" in out:
                self.status.emit("权限已修复，正在刷新…")
                self.navigate(self.cwd)
            else:
                self.status.emit(f"修复失败: {out}")

        self._set_busy(True)
        self._spawn(work, done, self._op_error)

    def _set_busy(self, busy: bool) -> None:
        """切换忙碌态

        忙碌时禁用本面板所有按钮，并通过 busy_changed 信号
        通知外层（详情对话框）也禁用「关闭」——
        下载/上传进行中关掉窗口会让后台线程访问已销毁对象。
        """
        self._busy = busy
        for b in (self.btn_up, self.btn_home, self.btn_refresh,
                  self.btn_upload, self.btn_download, self.btn_mkdir,
                  self.btn_rename, self.btn_delete):
            b.setEnabled(not busy)
        self.busy_changed.emit(busy)
        if busy:
            self.status.emit("加载中…")

    def _op_error(self, msg: str) -> None:
        self._set_busy(False)
        text = str(msg)
        self.status.emit(f"操作失败: {text}")

        # 权限类失败 → 直接给一键提权入口
        # 用户不必自己去概览页找那个按钮
        if self._is_permission_error(text):
            self._offer_sudo(text)

    @staticmethod
    def _is_permission_error(msg: str) -> bool:
        low = msg.lower()
        return any(k in low for k in (
            "permission denied", "eacces", "operation not permitted",
            "permission", "拒绝访问", "权限",
        ))

    def _offer_sudo(self, err: str) -> None:
        """文件操作因权限失败时，提示并支持一键提权

        提权成功后这台 VM 永久免密（规则写进 guest 的 sudoers），
        用户只需要授权这一次。
        """
        from PyQt6.QtWidgets import QInputDialog

        ret = QMessageBox.question(
            self, "需要 root 权限",
            f"操作失败：{err[:200]}\n\n"
            "这个目录需要管理员权限才能写入。\n"
            "是否现在授权一次？授权后该虚拟机的所有操作"
            "都会自动获得 root 权限，不再询问。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return

        default_pwd = ""
        try:
            from core.vm.cloudinit import DEFAULT_PASSWORD

            default_pwd = DEFAULT_PASSWORD
        except Exception:
            pass

        pwd, ok = QInputDialog.getText(
            self, "启用 root 权限",
            f"请输入「{self.vm.spec.name}」内用户 "
            f"{self.vm.spec.username} 的登录密码：",
            QLineEdit.EchoMode.Password,
            default_pwd,
        )
        if not ok or not pwd:
            return

        from core.vm import sudo as vm_sudo

        vm_sudo.set_password(pwd)
        self.status.emit("正在授权…")
        self._set_busy(True)

        def work():
            return vm_sudo.ensure_nopass(self.vm)

        def done(success):
            self._set_busy(False)
            if success:
                self.status.emit("已获得 root 权限，正在重试…")
                notifier().info("已启用 root 权限", self.vm.spec.name)
                self.navigate(self.cwd)
            else:
                self.status.emit("授权失败：密码不正确或无 sudo 权限")

        def on_err(m: str) -> None:
            self._set_busy(False)
            self.status.emit(f"授权失败: {m}")

        self._spawn(work, done, on_err)

    def _spawn(self, work, done, error=None) -> None:
        """后台执行（保持引用，避免任务被回收）"""
        def _err(m):
            (error or (lambda x: None))(m)

        t = run_async(work, on_done=done, on_error=_err, parent=self)
        self._tasks.append(t)

    def close_session(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None


# 延迟导入，避免顶部堆太多
from PyQt6.QtWidgets import (  # noqa: E402
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
)
