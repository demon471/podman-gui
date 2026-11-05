"""共享网桥：VM 互通 + 宿主机用内部 IP 直连

背景
────
QEMU 默认用户网络（SLIRP）有两个硬限制：
  · VM 之间互相看不见
  · 宿主机只能用 127.0.0.1:转发端口 访问

改用共享网桥后两者都解决。踩过的两个坑：
  1. /etc/qemu/bridge.conf 必须 644 —— QEMU 自己解析它，
     640 root:kvm 时普通用户读不到，启动直接报
     "failed to parse default acl file"
  2. 每台 VM 必须有唯一 MAC —— QEMU 对第一块网卡默认
     52:54:00:12:34:56，两台一样时网桥分不清，互 ping 必然失败
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent



class TestContainerNetwork:
    """容器档的网络：veth 挂网桥 + DHCP，无 MAC 撞车问题

    QEMU 时代每台 VM 要手动生成唯一 MAC（默认 52:54:00:12:34:56
    会撞）；lxc 的 lxc-user-nic 会给每台容器生成独立 veth 和 MAC，
    这里验证配置生成层不再有 MAC 相关参数。
    """

    def test_conf_has_no_fixed_mac(self):
        from core.vm.container import conf_text

        class Spec:
            name = "t"
            hostname = "t"
            arch = "amd64"
            shares = []
            autostart = False

        text = conf_text(Spec(), Path("/tmp/rfs"), "pgbr0", 100000, 65536)
        assert "52:54:00:12:34:56" not in text

    def test_conf_uses_bridge(self):
        from core.vm.container import conf_text

        class Spec:
            name = "t"
            hostname = "t"
            arch = "amd64"
            shares = []
            autostart = False

        text = conf_text(Spec(), Path("/tmp/rfs"), "pgbr0", 100000, 65536)
        assert "lxc.net.0.type = veth" in text
        assert "lxc.net.0.link = pgbr0" in text


class TestBridgeModule:
    def test_api_exists(self):
        from core.vm import bridge

        for fn in ("ready", "setup", "teardown", "qemu_args",
                   "lease_ip", "bridge_exists", "helper_available"):
            assert callable(getattr(bridge, fn)), fn

    def test_qemu_args_contain_bridge(self):
        from core.vm import bridge

        args = bridge.qemu_args(mac="52:54:00:aa:bb:cc")
        joined = " ".join(args)
        assert f"br={bridge.BRIDGE}" in joined
        assert "mac=52:54:00:aa:bb:cc" in joined

    def test_qemu_args_mac_optional(self):
        from core.vm import bridge

        joined = " ".join(bridge.qemu_args())
        assert "mac=" not in joined

    def test_constants(self):
        from core.vm import bridge

        assert bridge.BRIDGE.startswith("pg")
        assert "/16" in bridge.SUBNET
        assert bridge.HOST_IP.startswith("10.")

    def test_bridge_conf_readable_check(self):
        """ready() 不能因为读不到 bridge.conf 就永久返回 False"""
        import inspect

        from core.vm import bridge

        src = inspect.getsource(bridge.ready)
        assert "os.access" in src
        assert "R_OK" in src


class TestBridgeSetupScript:
    def test_no_qemu_leftover(self):
        """纯容器架构不再写 /etc/qemu/bridge.conf（qemu 非依赖）"""
        from core.vm import bridge

        script = bridge._setup_script()
        assert "/etc/qemu" not in script

    def test_container_fixups_present(self):
        """非特权容器前置（ACL + lxc-usernet）必须在脚本里"""
        from core.vm import bridge

        script = bridge._setup_script()
        assert "lxc-usernet" in script
        assert "setfacl" in script

    def test_masquerade(self):
        """guest 要能上外网"""
        from core.vm import bridge

        script = bridge._setup_script()
        assert "MASQUERADE" in script
        assert "ip_forward=1" in script

    def test_dhcp(self):
        """guest 靠 DHCP 拿地址（cloud-init 不会自动配静态 IP）"""
        from core.vm import bridge

        script = bridge._setup_script()
        assert "--dhcp-range" in script
        assert f"--interface={bridge.BRIDGE}" in script

    def test_idempotent(self):
        from core.vm import bridge

        script = bridge._setup_script()
        assert 'if [ ! -e /sys/class/net/' in script, "建网桥要先判断"
        assert "grep -qE" in script, "写白名单要先判断"


class TestHostUsesGuestIp:
    """网桥模式下 SSH 目标改成 guest 自己的 IP"""

    def test_ssh_host_property(self):
        from core.vm import manager

        assert hasattr(manager.VM, "ssh_host")

    def test_sshconfig_uses_ssh_host(self):
        src = (ROOT / "core" / "vm" / "sshconfig.py").read_text(
            encoding="utf-8"
        )
        start = src.find("Host {domain}")
        body = src[max(0, start - 400):start + 400]
        assert "vm.ssh_host" in body
        assert "vm.ssh_port" in body


class TestDnsPerVm:
    def test_address_records(self):
        from core.vm import dns

        dns.set_vm_addresses({"dev": "10.99.1.5"})
        try:
            conf = dns.dnsmasq_conf_content()
            assert "address=/dev.vm.local/10.99.1.5" in conf
            # 通配兜底仍在
            assert f"address=/{dns.DOMAIN_SUFFIX}/127.0.0.1" in conf
        finally:
            dns.set_vm_addresses({})


class TestSshTargetUsesGuestIp:
    """SSH 必须连到 guest 自己的 IP，不能连 127.0.0.1

    这是真实踩过的坑：网桥模式没有 hostfwd，guest 端口就是 22。
    如果连接目标还写死 127.0.0.1，那条命令会打到宿主机自己的
    22 端口 —— Connection refused。表面上「SSH 连不上」，
    实际是连错了机器，从界面上完全看不出来。
    文件页、共享挂载、终端走的都是同一个底层入口，
    所以这里必须卡住。
    """

    def test_helper_exists(self):
        from core.vm.ssh import _ssh_target

        assert callable(_ssh_target)

    def test_uses_ssh_host(self):
        """网桥模式下目标必须是 guest IP"""

        class V:
            ssh_host = "10.99.1.7"

        from core.vm.ssh import _ssh_target

        assert _ssh_target(V()) == "10.99.1.7"

    def test_falls_back_to_loopback(self):
        """SLIRP 模式（没有 ssh_host）才退回 127.0.0.1"""

        class V:
            ssh_host = ""

        from core.vm.ssh import _ssh_target

        assert _ssh_target(V()) == "127.0.0.1"

    def test_no_hardcoded_loopback_in_ssh_module(self):
        """ssh.py 里不能再有写死的 127.0.0.1 连接目标"""
        import re

        src = (ROOT / "core" / "vm" / "ssh.py").read_text(encoding="utf-8")
        # 排除辅助函数里合法的兜底常量和注释
        body = src.split("def _ssh_target", 1)[-1]
        hits = [
            l.strip()
            for l in body.splitlines()
            if re.search(r'@["\']?127\.0\.0\.1|hostname\s*=\s*["\']127', l)
        ]
        assert not hits, f"仍有硬编码回环地址：{hits}"

    def test_files_module_uses_helper(self):
        """SFTP 连接也必须走同一个解析，不能自己写 127.0.0.1"""
        src = (ROOT / "core" / "vm" / "files.py").read_text(encoding="utf-8")
        assert "_ssh_target(" in src
        assert 'hostname="127.0.0.1"' not in src

