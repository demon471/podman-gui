"""共享目录（9p）：后端接口 + UI 操作逻辑

历史问题：`mount_all_shares` 从未被任何地方调用 ——
用户配好共享目录、重启 VM 后，VM 内 /mnt/host/ 下依然什么都没有。
QEMU 的 -virtfs 只是把目录暴露成 9p 设备，guest 内还要 mount。
"""

from __future__ import annotations

import inspect
import re
import time

import pytest


class TestSshShareApi:
    """core/vm/ssh.py 的共享目录接口"""

    def test_api_functions_exist(self):
        from core.vm import ssh

        for name in (
            "mount_share", "mount_all_shares",
            "unmount_share", "mounted_tags", "share_states",
        ):
            assert hasattr(ssh, name), f"缺少 {name}"

    def test_mount_all_called_somewhere(self):
        """mount_all_shares 必须被调用 —— 否则功能是死的"""
        from pathlib import Path

        root = Path(__file__).parent.parent
        found = []
        for f in list(root.glob("ui/*.py")) + list(root.glob("core/**/*.py")):
            text = f.read_text(encoding="utf-8")
            if "mount_all_shares" in text and "def mount_all_shares" not in text:
                found.append(f.name)
        assert found, (
            "没有任何地方调用 mount_all_shares —— "
            "共享目录配置了也不会在 guest 内挂载"
        )

    def test_unmount_uses_sudo(self):
        """卸载需要 root（9p 挂载点是 root 创建的）"""
        from core.vm import ssh

        src = inspect.getsource(ssh.unmount_share)
        assert "sudo umount" in src

    def test_mounted_tags_parses_both_backends(self):
        """解析 /proc/mounts 时必须同时匹配 virtiofs 和 9p

        只查 9p 会让 virtiofs 挂载显示成「未挂载」——
        用户看到「挂载成功但状态说没挂」。
        """
        from core.vm import ssh

        src = inspect.getsource(ssh.mounted_tags)
        assert "/proc/mounts" in src
        assert "9p" in src
        assert "virtiofs" in src, "必须匹配 virtiofs"

    def test_mount_prefers_virtiofs(self):
        """挂载优先 virtiofs，失败才回退 9p"""
        from core.vm import ssh

        src = inspect.getsource(ssh.mount_share)
        assert "virtiofs" in src
        assert src.find("virtiofs") < src.find("9p"), (
            "virtiofs 应排在 9p 之前尝试"
        )

    def test_share_states_shape(self):
        """share_states 返回 {tag: bool}"""
        from core.vm import ssh

        src = inspect.getsource(ssh.share_states)
        assert "spec.shares" in src
        assert "mounted_tags" in src


class TestShareTagDerivation:
    """挂载点自动推导

    用户报过：选中文目录（如「下载」）时，输入框里显示 `--` ——
    因为旧实现把每个非 ASCII 字符都替换成了 `-`。
    """

    @staticmethod
    def _derive(path: str):
        from ui.vm_detail import VMDetailDialog

        return VMDetailDialog._derive_share_tag(path)

    def test_ascii_name_kept(self):
        tag, auto = self._derive("/home/john/projects")
        assert tag == "projects"
        assert auto is False

    def test_chinese_name_falls_back_to_share(self):
        """中文目录名 → share，绝不能变成 -- 或 ---"""
        for path in ("/home/john/下载", "/home/john/项目", "/home/john/文档"):
            tag, auto = self._derive(path)
            assert tag == "share", f"{path} → {tag}"
            assert auto is True
            assert "--" not in tag

    def test_no_consecutive_dashes(self):
        tag, _ = self._derive("/home/john/--x--")
        assert "--" not in tag
        assert not tag.startswith("-") and not tag.endswith("-")

    def test_dots_and_spaces_removed(self):
        tag, _ = self._derive("/home/john/my.data")
        assert "." not in tag
        tag2, _ = self._derive("/home/john/a b")
        assert " " not in tag2

    def test_root_falls_back(self):
        tag, auto = self._derive("/")
        assert tag == "share"
        assert auto is True

    def test_result_always_valid_tag(self):
        """任何输入的结果都必须能安全用作 9p tag"""
        for path in (
            "/home/john/下载", "/home/john/projects", "/home/john/a b",
            "/home/john/--x--", "/", "/home/john/中文mix",
        ):
            tag, _ = self._derive(path)
            assert re.fullmatch(r"[A-Za-z0-9_-]+", tag), f"{path} → {tag!r}"


class TestShareUiSource:
    """UI 逻辑的静态检查（不启动真实 VM）"""

    @staticmethod
    def _src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "ui" / "vm_detail.py").read_text(
            encoding="utf-8"
        )

    def test_add_uses_file_dialog(self):
        """添加共享用目录选择器，不用手打路径"""
        src = self._src()
        start = src.find("def _add_share")
        end = src.find("def _del_share")
        body = src[start:end]
        assert "getExistingDirectory" in body, "应该用目录选择对话框"
        assert "DontUseNativeDialog" in body, (
            "KDE Wayland 下原生 portal 不可靠"
        )

    def test_add_derives_tag_automatically(self):
        """添加时应调用推导函数，而不是让用户从空白开始手打"""
        src = self._src()
        start = src.find("def _add_share")
        end = src.find("def _del_share")
        body = src[start:end]
        assert "_derive_share_tag" in body, "挂载点应自动推导"

    def test_dialog_has_placeholder(self):
        """输入框要有 placeholder 提示格式"""
        src = self._src()
        start = src.find("def _add_share")
        end = src.find("def _del_share")
        body = src[start:end]
        assert "setPlaceholderText" in body

    def test_delete_unmounts_first(self):
        """移除共享时要先 umount，避免 Stale file handle"""
        src = self._src()
        start = src.find("def _del_share")
        end = src.find("def _mount_selected_share")
        body = src[start:end]
        assert "unmount_share" in body

    def test_has_apply_and_restart(self):
        """必须有「应用并重启」直达按钮"""
        src = self._src()
        assert "_apply_shares" in src
        assert "restart_vm" in src

    def test_table_has_state_column(self):
        """表格要有状态列"""
        src = self._src()
        assert '"状态"' in src
        assert "share_states" in src

    def test_dirty_flag_initialized(self):
        """_shares_dirty 必须在 __init__ 初始化，否则首次读会 AttributeError"""
        src = self._src()
        init = src[src.find("def __init__"):src.find("def _build_ui")]
        assert "_shares_dirty" in init


@pytest.mark.needs_vm
class TestShareEndToEnd:
    """真实 VM 上的完整流程（慢，默认跳过）"""

    def test_mount_and_access(self):
        from core.vm import manager, ssh

        vm = manager.load("dev") or manager.load("rocky")
        if vm is None:
            pytest.skip("没有虚拟机")
        if not vm.spec.shares:
            pytest.skip("该虚拟机没有配置共享目录")
        if not vm.running:
            pytest.skip("虚拟机未运行")

        results = ssh.mount_all_shares(vm)
        assert all(ok for _t, ok, _m in results), results

        states = ssh.share_states(vm)
        assert all(states.values()), states

        # guest 内真的能列目录
        tag = vm.spec.shares[0][1]
        ok, out = ssh.run_command(vm, f"ls /mnt/host/{tag}", timeout=20)
        assert ok, out


class TestVirtiofsModule:
    """core/vm/virtiofs.py 后端"""

    def test_daemon_available(self):
        """宿主机应有 virtiofsd（QEMU 自带）"""
        from core.vm import virtiofs

        assert virtiofs.available(), "未找到 virtiofsd"
        assert virtiofs.virtiofsd_path() is not None

    def test_socket_and_pid_paths(self, tmp_path):
        from core.vm import virtiofs

        sock = virtiofs.socket_path(tmp_path, "my-share")
        assert sock.parent == tmp_path
        assert "my-share" in sock.name
        pid = virtiofs.pid_path(tmp_path, "my-share")
        assert pid.suffix == ".pid"

    def test_tag_with_special_chars_sanitized(self, tmp_path):
        from core.vm import virtiofs

        sock = virtiofs.socket_path(tmp_path, "a/b:c")
        assert "/" not in sock.name and ":" not in sock.name

    def test_qemu_args_shape(self, tmp_path):
        from core.vm import virtiofs

        shares = [("/home/x/a", "ta"), ("/home/x/b", "tb")]
        args = virtiofs.qemu_args(tmp_path, shares)
        joined = " ".join(args)
        assert "vhost-user-fs-pci" in joined
        assert "tag=ta" in joined and "tag=tb" in joined
        assert "-chardev" in joined

    def test_no_shares_gives_no_args(self, tmp_path):
        from core.vm import virtiofs

        assert virtiofs.qemu_args(tmp_path, []) == []

    def test_start_daemon_rejects_missing_dir(self, tmp_path):
        from core.vm import virtiofs

        ok, err = virtiofs.start_daemon(tmp_path, "/nonexistent/xyz", "t")
        assert ok is False
        assert "不存在" in err


class TestQemuMemfd:
    """QEMU 必须用 memfd 共享内存后端

    vhost-user（virtiofs 用的协议）要求把 guest 内存共享给
    virtiofsd。默认匿名内存无法 mmap，virtiofsd 会报
    HandleRequest(InvalidParam) 并断开，表现为 mount 卡死。
    """

    def test_build_command_uses_memfd(self, qapp):
        import inspect

        from core.vm import qemu

        src = inspect.getsource(qemu.build_command)
        assert "memory-backend-memfd" in src
        assert "share=on" in src


class TestRestartAndMountFlow:
    """「应用并重启」→ 关闭详情 → 列表 loading → 自动重开

    用户要求的交互：
      1. 确认重启后关闭详情对话框
      2. 列表该行显示「目录挂载中…」的 loading
      3. 重启 + 挂载完成后自动重新打开详情，定位到共享目录 tab
    """

    @staticmethod
    def _page_src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "ui" / "vm_page.py").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _detail_src() -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "ui" / "vm_detail.py").read_text(
            encoding="utf-8"
        )

    def test_detail_emits_signal_and_closes(self):
        """详情页不自己重启，而是发信号并关闭"""
        src = self._detail_src()
        start = src.find("def _apply_shares")
        end = src.find("# 底部动作", start)
        body = src[start:end]
        assert "restart_and_mount_requested.emit" in body, "应发信号"
        assert "self.accept()" in body, "应关闭自己"
        # 不应该在详情页里做重启
        assert "restart_vm" not in body, "重启应由列表页接管"

    def test_detail_has_signal(self):
        src = self._detail_src()
        assert "restart_and_mount_requested = pyqtSignal(str)" in src

    def test_detail_supports_initial_tab(self):
        src = self._detail_src()
        assert "initial_tab" in src
        start = src.find("def __init__")
        end = src.find("def _build_ui", start)
        assert "initial_tab" in src[start:end]

    def test_page_has_mounting_state(self):
        """列表要能显示「目录挂载中…」"""
        src = self._page_src()
        assert "_mounting" in src
        assert "目录挂载中" in src

    def test_page_handles_restart_request(self):
        src = self._page_src()
        assert "def _restart_and_mount" in src
        assert "def _reopen_detail_shares" in src
        # 连接信号
        assert "restart_and_mount_requested.connect" in src

    def test_restart_waits_for_ssh(self):
        """重启后必须等 SSH 就绪再挂载（否则 mount 失败）"""
        src = self._page_src()
        start = src.find("def _restart_and_mount")
        end = src.find("def _reopen_detail_shares", start)
        body = src[start:end]
        assert "test_connection" in body, "必须等 SSH 就绪"
        assert "mount_all_shares" in body

    def test_reopen_targets_shares_tab(self):
        src = self._page_src()
        start = src.find("def _reopen_detail_shares")
        end = src.find("\n    def ", start + 10)
        body = src[start:end]
        assert "initial_tab" in body
        assert "DETAIL_TAB_SHARES" in body


class TestSleepRestoreShares:
    """休眠/恢复必须处理共享目录

    virtiofs 的 inode 由 virtiofsd 进程维护，快照只保存 guest 内存。
    恢复时 virtiofsd 是新进程，guest 缓存的 inode 可能指向错误文件
    —— 属于静默数据错乱。所以休眠前卸载、恢复后重挂。
    """

    @staticmethod
    def _src(name: str) -> str:
        from pathlib import Path

        return (Path(__file__).parent.parent / "core" / "vm" / name).read_text(
            encoding="utf-8"
        )

    def test_soft_stop_unmounts_before_snapshot(self):
        src = self._src("manager.py")
        start = src.find("def soft_stop")
        end = src.find("def resume_vm", start)
        body = src[start:end]
        assert "unmount_all_shares" in body, "休眠前必须卸载共享"
        # 卸载必须在 snapshot_save 之前
        assert body.find("unmount_all_shares") < body.find("snapshot_save")

    def test_resume_remounts_after_restore(self):
        src = self._src("manager.py")
        start = src.find("def resume_vm")
        end = src.find("def desired_running_vms", start)
        body = src[start:end]
        assert "mount_all_shares" in body, "恢复后必须重挂共享"
        assert body.find("snapshot_restore") < body.find("mount_all_shares")

    def test_unmount_all_shares_exists(self):
        src = self._src("ssh.py")
        assert "def unmount_all_shares" in src

    def test_snapshot_restore_starts_virtiofsd(self):
        """快照恢复也要拉起 virtiofsd（否则 vhost-user 设备连不上）"""
        src = self._src("qemu.py")
        start = src.find("def snapshot_restore")
        end = src.find("def _cleanup_share_daemons", start)
        body = src[start:end]
        assert "ensure_all" in body, "恢复前必须启动 virtiofsd"
        # 用 "cmd = build_command(" 定位真实调用，避免匹配到注释里的字样
        assert body.find("ensure_all") < body.find("cmd = build_command(")
