"""容器内文件浏览（只读）

用户要的是「进去看看有什么」，所以刻意只提供查看：
不提供上传/下载/新建/重命名/删除 —— 容器是临时环境，
改动应该走镜像或挂载，而不是手改运行中的容器。
挂载目录要标出来，因为用户最关心数据落在宿主机哪里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestLsParsing:
    """解析 `ls -la` 输出"""

    SAMPLE = """total 8
drwxr-xr-x    1 root     root            18 Nov  1 19:09 .
drwxr-xr-x    1 root     root            18 Nov  1 19:09 ..
drwxr-xr-x    1 root     root           242 Sep 18 05:32 bin
drwxr-xr-x    2 redis    root            42 Nov  1 19:04 data
-rw-r--r--    1 root     root           123 Nov  1 19:04 dump.rdb
lrwxrwxrwx    1 root     root             7 Sep 18 05:32 lib -> usr/lib
"""

    def test_parses_entries(self):
        from core.container_files import _parse_ls

        entries = _parse_ls(self.SAMPLE)
        names = [e.name for e in entries]
        assert "bin" in names and "data" in names and "dump.rdb" in names
        assert "." not in names and ".." not in names

    def test_detects_dir_and_file(self):
        from core.container_files import _parse_ls

        by = {e.name: e for e in _parse_ls(self.SAMPLE)}
        assert by["bin"].is_dir is True
        assert by["dump.rdb"].is_dir is False
        assert by["dump.rdb"].size == 123

    def test_parses_symlink_target(self):
        from core.container_files import _parse_ls

        by = {e.name: e for e in _parse_ls(self.SAMPLE)}
        assert by["lib"].is_link is True
        assert by["lib"].target == "usr/lib"

    def test_dirs_sort_first(self):
        from core.container_files import _parse_ls

        entries = _parse_ls(self.SAMPLE)
        assert entries[0].is_dir, "目录应排在文件前面"

    def test_handles_spaces_in_name(self):
        from core.container_files import _parse_ls

        line = "-rw-r--r--  1 root root  10 Nov  1 19:04 my file.txt"
        entries = _parse_ls(line)
        assert entries[0].name == "my file.txt"

    def test_empty_output(self):
        from core.container_files import _parse_ls

        assert _parse_ls("") == []


class TestMounts:
    def test_mounts_returns_dict(self):
        from core.container_files import mounts

        assert isinstance(mounts, object)

    def test_list_dir_signature(self):
        import inspect

        from core.container_files import list_dir

        params = inspect.signature(list_dir).parameters
        assert "cid" in params and "path" in params


class TestReadOnlyOnly:
    """只读：不许有写操作"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "container_files.py").read_text(encoding="utf-8")

    def test_no_write_actions(self):
        """只读：不允许有写操作的方法或按钮"""
        src = self._src()
        for forbidden in (
            "def _upload", "def _download", "def _mkdir",
            "def _rename", "def _delete", "def _put",
        ):
            assert forbidden not in src, f"只读浏览不该有 {forbidden}"

        # 按钮文案也不该出现写操作（文档字符串里说明「不提供」不算）
        import re

        for label in ("上传", "下载", "新建目录", "重命名", "删除"):
            assert not re.search(
                rf'(QPushButton|addAction)\([^)]*"{label}"', src
            ), f"只读浏览不该有「{label}」按钮"

    def test_has_navigation(self):
        src = self._src()
        for need in ("_go_up", "_go_typed", "navigate", "_on_double_click"):
            assert need in src, f"缺少导航能力：{need}"

    def test_marks_mount_column(self):
        """挂载单独一列，且排在权限前面"""
        src = self._src()
        assert '"挂载"' in src
        cols = src[src.find("COLUMNS = ["):src.find("]", src.find("COLUMNS = ["))]
        assert cols.index("挂载") < cols.index("权限"), "挂载列应在权限之前"

    def test_mount_shows_host_source(self):
        src = self._src()
        assert "_mount_source" in src
        assert "挂载自" in src


class TestEntryPoint:
    def test_container_page_has_browse(self):
        src = (ROOT / "ui" / "container_page.py").read_text(encoding="utf-8")
        assert "act_browse" in src
        assert "ContainerFilesDialog" in src
        assert '"浏览"' in src

    def test_browse_in_context_menu(self):
        src = (ROOT / "ui" / "container_page.py").read_text(encoding="utf-8")
        start = src.find("def _context_menu")
        body = src[start:start + 800]
        assert "act_browse" in body


class TestVmDetailCopyButton:
    """VM 详情头部的复制按钮"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "vm_detail.py").read_text(encoding="utf-8")

    def test_has_copy_button(self):
        src = self._src()
        assert "btn_copy_ssh" in src

    def test_button_has_fixed_size(self):
        """固定尺寸：复制后的反馈只换图标，不会把行撑开"""
        src = self._src()
        start = src.find("self.btn_copy_ssh = QToolButton()")
        body = src[start:start + 600]
        assert "setFixedSize" in body

    def test_feedback_is_icon_only(self):
        """复制后不能往状态栏写完整命令（会把详情页拉长）"""
        src = self._src()
        start = src.find("def _copy_ssh")
        body = src[start:src.find("def _restore_copy_icon", start)]
        assert "lbl_status" not in body, "复制反馈不应写状态栏"
        assert "_restore_copy_icon" in body

    def test_ssh_label_not_shadowed(self):
        """账户页不能复用 lbl_ssh 这个名字，否则头部会一直空白"""
        src = self._src()
        assert src.count("self.lbl_ssh = QLabel") == 1, (
            "lbl_ssh 只能定义一次（头部），否则引用会被覆盖"
        )
        # 账户页用的是另外一套控件
        assert "lbl_guest_host" in src


class TestBrowseButtonState:
    """浏览按钮要跟随选中状态（和其它操作按钮一致）"""

    @staticmethod
    def _src() -> str:
        return (ROOT / "ui" / "container_page.py").read_text(encoding="utf-8")

    def test_disabled_when_nothing_selected(self, qapp):
        from core.podman_client import PodmanClient
        from ui.container_page import ContainerPage

        page = ContainerPage(PodmanClient())
        for _ in range(10):
            qapp.processEvents()
        page.table.clearSelection()
        page._update_actions()
        assert page.act_browse.isEnabled() is False, "无选中时浏览应置灰"
        page.deleteLater()

    def test_enabled_when_selected(self, qapp):
        from core.podman_client import PodmanClient
        from ui.container_page import ContainerPage

        page = ContainerPage(PodmanClient())
        for _ in range(10):
            qapp.processEvents()
        if page.table.model().rowCount() == 0:
            pytest.skip("没有容器")
        page.table.selectRow(0)
        page._update_actions()
        assert page.act_browse.isEnabled() is True
        page.deleteLater()

    def test_in_update_actions(self):
        src = self._src()
        start = src.find("def _update_actions")
        body = src[start:start + 700]
        assert "act_browse.setEnabled" in body

    def test_disabled_during_operation(self):
        """操作进行中也要禁用，避免并发 exec"""
        src = self._src()
        start = src.find("def _set_actions_enabled")
        body = src[start:start + 400]
        assert "self.act_browse" in body
