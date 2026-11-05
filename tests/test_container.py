"""容器后端（core/vm/container.py + manager 容器路由）

纯容器架构的核心守卫：
  · lxc 配置生成（网络/共享目录/idmap/自启）
  · rootfs 播种（用户/密码/SSH 公钥/hostname）
  · 改密码（shadow 行替换）
  · manager 的容器路由（spec 兼容、密码、共享目录校验）

背景（docs/light-machines.md v2）：
  产品只剩系统容器这一种形态，QEMU 全链路已删。
  容器 start 即用户可见的「启动」，stop 即「真休眠」。
"""

from __future__ import annotations

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def make_spec(**kw):
    from core.vm.spec import VMSpec

    base = dict(
        name="t1",
        image_key="alpine-3.22-lxc",
        username="john",
        hostname="t1",
    )
    base.update(kw)
    return VMSpec(**base)


class FakeSpec:
    """鸭子类型测试桩（container.conf_text 不要求真 VMSpec）"""

    def __init__(self, name="t", shares=None, autostart=False):
        self.name = name
        self.hostname = name
        self.arch = "amd64"
        self.shares = shares or []
        self.autostart = autostart


class TestLxcConfig:
    def test_basic_fields(self):
        from core.vm.container import conf_text

        text = conf_text(FakeSpec("dev"), "/tmp/rfs", "pgbr0", 100000, 65536)
        assert "lxc.uts.name = dev" in text
        assert "lxc.rootfs.path = dir:/tmp/rfs" in text
        assert "lxc.include = /usr/share/lxc/config/common.conf" in text

    def test_network_bridge(self):
        from core.vm.container import conf_text

        text = conf_text(FakeSpec(), "/tmp/rfs", "pgbr0", 100000, 65536)
        assert "lxc.net.0.type = veth" in text
        assert "lxc.net.0.link = pgbr0" in text
        assert "lxc.net.0.name = eth0" in text

    def test_idmap_maps_host_uid_first(self):
        """idmap 首段必须映射宿主用户自己

        rootfs 由普通用户解包（属主 = 宿主 uid），而普通用户无权
        chown 到 subuid 段（内核禁止）。所以容器 root(0) 映射到
        宿主 uid，rootfs 属主天然正确；其余 id 走 subuid 段。
        """
        import os

        from core.vm.container import conf_text

        me = os.getuid()
        text = conf_text(FakeSpec(), "/tmp/rfs", "pgbr0", 123456, 65536)
        assert f"lxc.idmap = u 0 {me} 1" in text
        assert f"lxc.idmap = g 0 {me} 1" in text
        # 其余 id 段：容器 1.. → subuid 123456..
        assert "lxc.idmap = u 1 123456 65535" in text
        assert "lxc.idmap = g 1 123456 65535" in text

    def test_idmap_explicit_host_uid(self):
        from core.vm.container import conf_text

        text = conf_text(FakeSpec(), "/tmp/rfs", "pgbr0", 123456, 65536,
                         host_uid=1001)
        assert "lxc.idmap = u 0 1001 1" in text

    def test_shares_bind_mount(self):
        """共享目录 = lxc bind mount（替代 VM 档的 virtiofs）"""
        from core.vm.container import conf_text

        spec = FakeSpec(shares=[("/home/john/下载", "downloads")])
        text = conf_text(spec, "/tmp/rfs", "pgbr0", 100000, 65536)
        assert (
            "lxc.mount.entry = /home/john/下载 downloads "
            "none bind,rw,create=dir 0 0" in text
        )

    def test_share_nested_mount_point(self):
        """挂载点带子路径时去掉开头斜杠（lxc 相对 rootfs）"""
        from core.vm.container import conf_text

        spec = FakeSpec(shares=[("/data", "mnt/share")])
        text = conf_text(spec, "/tmp/rfs", "pgbr0", 100000, 65536)
        assert "none bind,rw,create=dir 0 0" in text
        assert " mnt/share none" in text

    def test_mount_entry_has_no_idmap(self):
        """bind mount 不能带 idmap=container

        lxc 对 bind mount 做 idmap 需要子 userns 的 CAP_SYS_ADMIN，
        我们的 idmap 把容器 root 映射到宿主 uid，实测
        "Failed to idmap detached mount" 直接起不来。
        宿主文件在容器内本来就显示为 root（映射方案），可读写。
        """
        from core.vm.container import conf_text

        spec = FakeSpec(shares=[("/data", "data")])
        text = conf_text(spec, "/tmp/rfs", "pgbr0", 100000, 65536)
        assert "idmap=container" not in text

    def test_autostart(self):
        from core.vm.container import conf_text

        on = conf_text(FakeSpec(autostart=True), "/tmp/rfs", "pgbr0",
                       100000, 65536)
        off = conf_text(FakeSpec(autostart=False), "/tmp/rfs", "pgbr0",
                        100000, 65536)
        assert "lxc.start.autostart = 1" in on
        assert "lxc.start.autostart" not in off


class TestSeedRootfs:
    @pytest.fixture
    def rootfs(self, tmp_path):
        """模拟一个最小发行版 rootfs"""
        r = tmp_path / "rootfs"
        for sub in ("etc", "usr/sbin", "home", "root"):
            (r / sub).mkdir(parents=True)
        (r / "etc" / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/sh\n",
            encoding="utf-8",
        )
        (r / "etc" / "shadow").write_text(
            "root:!:19000:0:99999:7:::\n",
            encoding="utf-8",
        )
        return r

    def test_hostname_written(self, rootfs):
        from core.vm.container import seed_rootfs

        ok, err = seed_rootfs(
            rootfs, hostname="dev", username="john",
            ssh_pubkey="ssh-ed25519 AAA test", password_hash="$6$abc",
        )
        assert ok, err
        assert (rootfs / "etc" / "hostname").read_text().strip() == "dev"
        assert "127.0.1.1\tdev" in (rootfs / "etc" / "hosts").read_text()

    def test_user_created(self, rootfs):
        from core.vm.container import seed_rootfs

        ok, _ = seed_rootfs(
            rootfs, hostname="dev", username="john",
            ssh_pubkey="", password_hash="$6$abc",
        )
        assert ok
        passwd = (rootfs / "etc" / "passwd").read_text()
        shadow = (rootfs / "etc" / "shadow").read_text()
        assert "john:x:1000:1000::/home/john:" in passwd
        assert "john:$6$abc:19000" in shadow

    def test_ssh_keys_for_root_and_user(self, rootfs):
        from core.vm.container import seed_rootfs

        ok, _ = seed_rootfs(
            rootfs, hostname="dev", username="john",
            ssh_pubkey="ssh-ed25519 AAA test", password_hash="$6$abc",
        )
        assert ok
        for home in ("root", "home/john"):
            ak = rootfs / home / ".ssh" / "authorized_keys"
            assert ak.exists(), home
            assert "ssh-ed25519 AAA test" in ak.read_text()

    def test_idempotent_pubkey(self, rootfs):
        """重复播种不产生重复公钥行"""
        from core.vm.container import seed_rootfs

        kw = dict(hostname="dev", username="john",
                  ssh_pubkey="ssh-ed25519 AAA test",
                  password_hash="$6$abc")
        seed_rootfs(rootfs, **kw)
        seed_rootfs(rootfs, **kw)
        ak = rootfs / "home" / "john" / ".ssh" / "authorized_keys"
        assert ak.read_text().count("ssh-ed25519") == 1

    def test_reseed_replaces_user(self, rootfs):
        """用户已存在（rootfs 预置）时不追加重复行"""
        from core.vm.container import seed_rootfs

        (rootfs / "etc" / "passwd").write_text(
            "root:x:0:0:root:/root:/bin/sh\n"
            "john:x:1000:1000::/home/john:/bin/bash\n",
            encoding="utf-8",
        )
        (rootfs / "etc" / "shadow").write_text(
            "root:!:19000:0:99999:7:::\n"
            "john:OLD:19000:0:99999:7:::\n",
            encoding="utf-8",
        )
        ok, _ = seed_rootfs(
            rootfs, hostname="dev", username="john",
            ssh_pubkey="", password_hash="$6$new",
        )
        assert ok
        passwd_lines = (rootfs / "etc" / "passwd").read_text().splitlines()
        shadow = (rootfs / "etc" / "shadow").read_text()
        # 数行而不是数子串：home 路径 "/home/john:" 也含 "john:"
        john_lines = [l for l in passwd_lines if l.startswith("john:")]
        assert len(john_lines) == 1
        assert "$6$new" in shadow
        assert "OLD" not in shadow


class TestPasswordChange:
    def test_shadow_replaced(self, tmp_path):
        from core.vm.container import set_root_password_hash, seed_rootfs

        rootfs = tmp_path / "rfs"
        (rootfs / "etc").mkdir(parents=True)
        seed_rootfs(
            rootfs, hostname="dev", username="john",
            ssh_pubkey="", password_hash="$6$old",
        )
        ok, err = set_root_password_hash(rootfs, "john", "$6$new")
        assert ok, err
        assert "$6$new" in (rootfs / "etc" / "shadow").read_text()

    def test_missing_user(self, tmp_path):
        from core.vm.container import set_root_password_hash

        rootfs = tmp_path / "rfs"
        (rootfs / "etc").mkdir(parents=True)
        (rootfs / "etc" / "shadow").write_text(
            "root:!:19000:0:99999:7:::\n", encoding="utf-8"
        )
        ok, err = set_root_password_hash(rootfs, "ghost", "$6$x")
        assert not ok
        assert "ghost" in err


class TestSpecCompat:
    """spec 序列化兼容（SQLite 列保留，老数据可读）"""

    def test_roundtrip(self):
        from core.vm.spec import PortForward, VMSpec

        spec = VMSpec(
            name="dev", image_key="alpine-3.22-lxc",
            username="john",
            forwards=[PortForward(8080, 80)],
            shares=[("/data", "data")],
            autostart=True,
            created_at=123.0,
        )
        data = spec.to_dict()
        spec2 = VMSpec.from_dict(data)
        assert spec2 == spec

    def test_old_vm_json_still_loads(self):
        """VM 档时代的 vm.json（含 memory_mb 等字段）能读进来"""
        from core.vm.spec import VMSpec

        old = {
            "name": "legacy", "image_key": "alpine-3.22",
            "arch": "amd64", "memory_mb": 2048, "cpus": 2,
            "disk_gb": 1024, "username": "dev", "hostname": "legacy",
            "forwards": [], "shares": [], "autostart": False,
            "created_at": 1.0,
        }
        spec = VMSpec.from_dict(old)
        assert spec.name == "legacy"
        assert spec.username == "dev"

    def test_hostname_defaults_to_name(self):
        from core.vm.spec import VMSpec

        assert VMSpec(name="x", image_key="k").hostname == "x"


class TestManagerRouting:
    """manager 的容器路由（不起真容器，只验证分支与校验）"""

    @pytest.fixture
    def mgr(self, monkeypatch, tmp_path):
        """把 machines_dir 指到临时目录，隔离真实数据"""
        import core.vm.manager as m

        monkeypatch.setattr(m, "machines_dir", lambda: tmp_path / "machines")
        monkeypatch.setattr(
            m, "DEFAULT_VM_ROOT", tmp_path, raising=False
        )
        return m

    def test_create_rejects_bad_name(self, mgr):
        vm, err = mgr.create_vm(name="a/b", image_key="alpine-3.22-lxc")
        assert vm is None and "名称无效" in err

    def test_create_rejects_taken_name(self, mgr, tmp_path):
        d = tmp_path / "machines" / "dev"
        d.mkdir(parents=True)
        (d / "vm.json").write_text(
            '{"name":"dev","image_key":"alpine-3.22-lxc"}',
            encoding="utf-8",
        )
        vm, err = mgr.create_vm(name="dev", image_key="alpine-3.22-lxc")
        assert vm is None and "已存在" in err

    def test_create_requires_lxc(self, mgr, monkeypatch):
        import core.vm.container as c

        monkeypatch.setattr(c, "lxc_version", lambda: None)
        vm, err = mgr.create_vm(name="dev", image_key="alpine-3.22-lxc")
        assert vm is None and "LXC" in err

    def test_create_requires_bridge(self, mgr, monkeypatch, tmp_path):
        import core.vm.bridge as b
        import core.vm.container as c

        monkeypatch.setattr(c, "lxc_version", lambda: "7.0.0")
        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))
        monkeypatch.setattr(b, "ready", lambda: False)

        vm, err = mgr.create_vm(name="dev", image_key="alpine-3.22-lxc")
        assert vm is None and "共享网络" in err

    def test_create_happy_path(self, mgr, monkeypatch, tmp_path):
        """用假 rootfs tarball 走通创建全流程"""
        import core.vm.bridge as b
        import core.vm.container as c
        from core.vm import images

        monkeypatch.setattr(c, "lxc_version", lambda: "7.0.0")
        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))
        monkeypatch.setattr(b, "ready", lambda: True)
        monkeypatch.setattr(
            images, "rootfs_downloaded", lambda spec, arch: True
        )

        # 假 rootfs：extract 就是从缓存 tarball 拷一份假树
        cache = tmp_path / "cache.tar.xz"
        cache.write_text("fake", encoding="utf-8")
        monkeypatch.setattr(
            images, "rootfs_path", lambda spec, arch="amd64": cache
        )

        def fake_extract(spec, arch, target_dir):
            for sub in ("etc", "usr/sbin", "home", "root"):
                (target_dir / sub).mkdir(parents=True, exist_ok=True)
            (target_dir / "etc" / "passwd").write_text(
                "root:x:0:0:root:/root:/bin/sh\n", encoding="utf-8"
            )
            (target_dir / "etc" / "shadow").write_text(
                "root:!:19000:0:99999:7:::\n", encoding="utf-8"
            )
            return True, ""

        monkeypatch.setattr(images, "extract_rootfs", fake_extract)

        logs = []
        vm, err = mgr.create_vm(
            name="dev", image_key="alpine-3.22-lxc",
            on_log=logs.append,
        )
        assert vm is not None, err
        assert (vm.dir / "config").exists()
        assert (vm.dir / "ssh_key").exists()
        assert (vm.dir / "rootfs" / "etc" / "hostname").read_text().strip() \
            == "dev"
        # 密码播种的是默认密码
        shadow = (vm.dir / "rootfs" / "etc" / "shadow").read_text()
        assert "john:$6$" in shadow
        # lxc 配置包含网桥
        conf = (vm.dir / "config").read_text()
        assert "lxc.net.0.link = pgbr0" in conf
        # 用户名继承宿主机
        assert vm.spec.username

    def test_add_share_updates_lxc_conf(self, mgr, monkeypatch, tmp_path):
        """添加共享目录必须同步重写 lxc 配置"""
        import core.vm.container as c

        d = tmp_path / "machines" / "dev"
        d.mkdir(parents=True)
        (d / "vm.json").write_text(
            '{"name":"dev","image_key":"alpine-3.22-lxc",'
            '"username":"john"}',
            encoding="utf-8",
        )
        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))

        vm = mgr.load("dev")
        assert vm is not None

        share_dir = tmp_path / "shared"
        share_dir.mkdir()
        ok, err = mgr.add_share(vm, str(share_dir), "data")
        assert ok, err
        conf = (d / "config").read_text()
        assert f"lxc.mount.entry = {share_dir} data" in conf

    def test_add_share_validates_path(self, mgr, tmp_path):
        d = tmp_path / "machines" / "dev2"
        d.mkdir(parents=True)
        (d / "vm.json").write_text(
            '{"name":"dev2","image_key":"alpine-3.22-lxc"}',
            encoding="utf-8",
        )
        vm = mgr.load("dev2")
        ok, err = mgr.add_share(vm, "not/abs", "x")
        assert not ok and "绝对路径" in err
        ok, err = mgr.add_share(vm, "/nonexistent_dir_xyz", "x")
        assert not ok and "不存在" in err

    def test_set_root_password_flow(self, mgr, monkeypatch, tmp_path):
        """改密码 = shadow 行替换 + state 记录"""
        import core.vm.container as c
        from core.vm.container import seed_rootfs

        d = tmp_path / "machines" / "dev3"
        (d / "rootfs" / "etc").mkdir(parents=True)
        seed_rootfs(
            d / "rootfs", hostname="dev3", username="john",
            ssh_pubkey="", password_hash="$6$old",
        )
        (d / "vm.json").write_text(
            '{"name":"dev3","image_key":"alpine-3.22-lxc",'
            '"username":"john"}',
            encoding="utf-8",
        )
        vm = mgr.load("dev3")
        ok, err = mgr.set_root_password(vm, "abc12345")
        assert ok, err
        shadow = (d / "rootfs" / "etc" / "shadow").read_text()
        assert "$6$" in shadow and "$6$old" not in shadow
        assert mgr.get_password("dev3") == "abc12345"

    def test_set_root_password_rejects_short(self, mgr, tmp_path):
        d = tmp_path / "machines" / "dev4"
        (d / "rootfs" / "etc").mkdir(parents=True)
        (d / "vm.json").write_text(
            '{"name":"dev4","image_key":"alpine-3.22-lxc"}',
            encoding="utf-8",
        )
        vm = mgr.load("dev4")
        ok, err = mgr.set_root_password(vm, "12")
        assert not ok and "太短" in err


    def test_load_all_self_heals(self, mgr, monkeypatch, tmp_path):
        """SQLite 有行但目录没了 → 记录清掉，列表不留幽灵"""
        from core import state as _state

        import core.state as real_state

        recorded = {}

        def fake_load_all_vms():
            return [
                {"name": "alive", "image_key": "alpine-3.22-lxc",
                 "username": "j"},
                {"name": "ghost", "image_key": "x", "username": "j"},
            ]

        def fake_delete_vm_row(name):
            recorded[name] = True

        # 直接 patch 模块函数：manager 里是 `from core import state`
        # 延迟导入，sys.modules 补丁对包属性绑定无效
        monkeypatch.setattr(real_state, "load_all_vms", fake_load_all_vms)
        monkeypatch.setattr(real_state, "delete_vm_row", fake_delete_vm_row)

        mdir = tmp_path / "machines" / "alive"
        mdir.mkdir(parents=True)
        (mdir / "vm.json").write_text(
            '{"name":"alive","image_key":"alpine-3.22-lxc"}',
            encoding="utf-8",
        )

        vms = mgr.load_all()
        names = [v.spec.name for v in vms]
        assert names == ["alive"]
        assert recorded.get("ghost") is True


class TestImageCatalog:
    def test_lxc_entries_exist(self):
        from core.vm import images

        for key in ("alpine-3.22-lxc", "rocky-10-lxc",
                    "debian-13-lxc"):
            spec = images.get_spec(key)
            assert spec is not None, key
            assert spec.format == "rootfs"
            assert not spec.uefi

    def test_rootfs_path_layout(self):
        from core.vm import images

        spec = images.get_spec("alpine-3.22-lxc")
        p = images.rootfs_path(spec)
        assert p.name == "alpine-3.22-lxc-amd64.tar.xz"


class TestSystemFixups:
    """非特权容器的系统前置检查（ACL / lxc-usernet）

    这两项缺失时容器必挂且报错难懂（"Could not access /home" /
    "Failed to open lxc-usernet"），已并入「启用共享网络」的
    一次性初始化脚本。这里守检查函数本身。
    """

    def test_acl_ok_getfacl_missing(self, monkeypatch):
        """getfacl 不可用时放行（无法判断别挡路）"""
        import subprocess

        import core.vm.container as c

        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))

        def raise_os(*a, **kw):
            raise FileNotFoundError("getfacl")

        monkeypatch.setattr(subprocess, "run", raise_os)
        assert c.acl_ok() is True

    def test_acl_ok_detects_missing(self, monkeypatch):
        import subprocess

        import core.vm.container as c

        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))

        class R:
            returncode = 0
            stdout = "user::rwx\nuser:john:rwx\n"

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: R()
        )
        assert c.acl_ok() is False

    def test_acl_ok_detects_present(self, monkeypatch):
        import subprocess

        import core.vm.container as c

        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))

        class R:
            returncode = 0
            stdout = "user::rwx\nuser:100000:--x\n"

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: R()
        )
        assert c.acl_ok() is True

    def test_usernet_ok_missing_file(self, tmp_path):
        """配额文件不存在 → False（FileNotFoundError 分支）"""
        from core.vm.container import _usernet_check

        assert _usernet_check(tmp_path / "nope", user="john") is False

    def test_usernet_check_found(self, tmp_path):
        from core.vm.container import _usernet_check

        f = tmp_path / "lxc-usernet"
        f.write_text("john veth pgbr0 10\n", encoding="utf-8")
        assert _usernet_check(f, user="john") is True
        assert _usernet_check(f, user="alice") is False

    def test_pending_fixups_lists_both(self, monkeypatch):
        import core.vm.container as c

        monkeypatch.setattr(c, "acl_ok", lambda: False)
        monkeypatch.setattr(c, "usernet_ok", lambda: False)
        items = c.pending_fixups()
        assert len(items) == 2
        assert any("ACL" in i for i in items)
        assert any("usernet" in i for i in items)

    def test_prerequisites_reports_fixups(self, monkeypatch):
        import core.vm.container as c

        monkeypatch.setattr(c, "lxc_version", lambda: "7.0.0")
        monkeypatch.setattr(c, "subuid_range", lambda: (100000, 65536))
        monkeypatch.setattr(c, "pending_fixups",
                            lambda: ["家目录 ACL"])
        ok, err = c.prerequisites()
        assert not ok
        assert "ACL" in err
        assert "启用共享网络" in err

    def test_setup_script_contains_fixups(self):
        """初始化脚本必须包含 ACL 和 lxc-usernet 配置"""
        from core.vm import bridge

        script = bridge._setup_script()
        assert "lxc-usernet" in script
        assert "setfacl" in script
        # pkexec 环境里拿发起用户
        assert "PKEXEC_UID" in script

    def test_list_images_includes_rootfs(self):
        """rootfs 条目没有静态 url，不能被 list_images 过滤掉

        否则向导里没有可下载的镜像（用户实测踩坑）。
        """
        from core.vm import images

        keys = [sp.key for sp in images.list_images()]
        assert "alpine-3.22-lxc" in keys
        assert "rocky-10-lxc" in keys
        assert "debian-13-lxc" in keys


class TestShareMountPoint:
    """共享目录挂载点统一为 /<tag>（Bug 修复守卫）

    旧 VM 档用 /mnt/host/<tag>（9p），容器 bind mount 是 /<tag>，
    界面与 ssh 命令曾因此显示错路径 + 永远「未挂载」。
    """

    def test_share_mount_point(self):
        from core.vm.ssh import share_mount_point

        assert share_mount_point("downloads") == "/downloads"
        assert share_mount_point("/downloads/") == "/downloads"

    def test_no_legacy_mnt_host(self):
        """代码里不允许再出现 /mnt/host 路径"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for f in ("core/vm/ssh.py", "ui/vm_detail.py"):
            src = (root / f).read_text(encoding="utf-8")
            # 历史注释（docstring 里的说明）不算
            code = "\n".join(
                l for l in src.splitlines()
                if not l.strip().startswith("#")
            )
            assert '"/mnt/host/' not in code, f

    def test_mount_share_verifies_bind_mount(self):
        """容器档 mount_share 只验证（bind mount 由 lxc 自动挂）"""
        import inspect

        from core.vm import ssh

        src = inspect.getsource(ssh.mount_share)
        assert "/proc/mounts" in src
        assert "mount -t" not in src

    def test_mounted_tags_matches_by_path(self):
        """挂载状态按挂载点路径匹配，不是按文件系统类型"""
        import inspect

        from core.vm import ssh

        src = inspect.getsource(ssh.mounted_tags)
        assert "awk '{print $2}'" in src


class TestFilePanelDefaults:
    """文件页默认家目录（Bug 修复守卫）"""

    def test_default_cwd_is_home(self, qapp):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        src = (root / "ui" / "vm_files.py").read_text(encoding="utf-8")
        assert 'self.cwd = f"/home/{vm.spec.username}"' in src

    def test_double_click_starts_when_stopped(self):
        """双击 = 未启动则启动；已启动不响应"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        src = (root / "ui" / "vm_page.py").read_text(encoding="utf-8")
        start = src.index("def _on_row_activated")
        body = src[start:start + 700]
        assert "_start_selected()" in body
        assert "if vm.running" in body


class TestSudoProvisioned:
    """ensure_sshd 必须装 sudo + 免密 sudoers（文件页提权兜底）"""

    def test_alpine_cmd_includes_sudo(self):
        import inspect

        from core.vm import container

        src = inspect.getsource(container.ensure_sshd)
        assert "sudo" in src
        assert "NOPASSWD" in src
        assert "/etc/sudoers.d/podbox" in src


class TestExtractSetuid:
    """普通用户解包丢 setuid 位，extract 必须补回

    内核禁止非 root 设置 setuid —— 不补的话容器里 sudo 直接
    "must be owned by uid 0 and have the setuid bit set"，
    文件页授权全部失效（用户实测 debian）。
    """

    def test_extract_restores_setuid(self, monkeypatch, tmp_path):
        import tarfile

        from core.vm import images

        # 造一个带 setuid 文件的 rootfs tarball
        src = tmp_path / "src"
        (src / "usr" / "bin").mkdir(parents=True)
        fake = src / "usr" / "bin" / "sudo"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        fake.chmod(0o4755)
        tarball = tmp_path / "r.tar.xz"
        with tarfile.open(tarball, "w:xz") as tf:
            tf.add(fake, arcname="./usr/bin/sudo")

        spec = images.get_spec("alpine-3.22-lxc")
        monkeypatch.setattr(images, "rootfs_path",
                            lambda s, arch="amd64": tarball)
        out = tmp_path / "out"
        ok, err = images.extract_rootfs(spec, "amd64", out)
        assert ok, err
        mode = (out / "usr" / "bin" / "sudo").stat().st_mode
        assert mode & 0o4000, f"setuid 位丢了: {oct(mode)}"
