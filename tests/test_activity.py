"""VM 活动探测（core/vm/activity.py）

这些测试守住「自动内存回收不会误杀正在使用的 VM」这条底线。
算法一旦出错，用户会在用着的时候被冻结 —— 属于最严重的回归。

纯逻辑部分（解析、判定）不依赖真实 VM，可以秒级跑完。
"""

from __future__ import annotations

import pytest

from core.vm import activity as A


# ─────────────────────────────────────────
# CPU 行解析
# ─────────────────────────────────────────
class TestParseCpu:
    def test_normal_line(self):
        # user nice system idle iowait irq softirq
        # 字段：user nice system idle iowait irq softirq
        #      1000  50    300   8000  100    0    20
        busy, total = A._parse_cpu("cpu  1000 50 300 8000 100 0 20 0 0 0")
        assert total == 9470            # 所有字段之和
        assert busy == 1370             # 9470 - (8000 idle + 100 iowait)

    def test_two_samples_give_usage(self):
        a = A._parse_cpu("cpu  1000 50 300 8000 100 0 20 0 0 0")
        b = A._parse_cpu("cpu  1100 50 320 8200 100 0 20 0 0 0")
        dbusy = b[0] - a[0]
        dtotal = b[1] - a[1]
        usage = dbusy * 100.0 / dtotal
        assert 0 <= usage <= 100
        assert usage == pytest.approx(37.5, abs=0.1)

    def test_invalid_line_returns_none(self):
        assert A._parse_cpu("") is None
        assert A._parse_cpu("cpu") is None
        assert A._parse_cpu("cpu  abc def") is None
        assert A._parse_cpu("intr 123") is None


# ─────────────────────────────────────────
# 活动判定
# ─────────────────────────────────────────
class TestActivityBusy:
    def test_idle_is_not_busy(self):
        act = A.Activity(
            reachable=True, sessions=0, cpu_percent=0.0,
            host_conns=0, slirp_conns=0, guest_conns=0,
        )
        assert act.busy is False
        assert act.describe() == "无活动"

    @pytest.mark.parametrize("field,value", [
        ("sessions", 1),
        ("host_conns", 1),
        ("slirp_conns", 1),
        ("guest_conns", 1),
    ])
    def test_any_connection_is_busy(self, field, value):
        """任何一类连接活跃都算在用 —— 宁可漏判不可误判"""
        act = A.Activity(reachable=True, **{field: value})
        assert act.busy is True

    def test_high_cpu_is_busy(self):
        act = A.Activity(reachable=True, cpu_percent=A.BUSY_CPU_PERCENT + 1)
        assert act.busy is True

    def test_low_cpu_is_not_busy(self):
        act = A.Activity(reachable=True, cpu_percent=A.BUSY_CPU_PERCENT - 1)
        assert act.busy is False

    def test_describe_lists_all_signals(self):
        act = A.Activity(
            reachable=True, sessions=1, cpu_percent=50,
            host_conns=2, slirp_conns=3, guest_conns=4,
        )
        text = act.describe()
        assert "1 会话" in text
        assert "2 宿主连接" in text
        assert "3 SLIRP 连接" in text
        assert "4 guest 连接" in text
        assert "CPU" in text


# ─────────────────────────────────────────
# 内存压力分级
# ─────────────────────────────────────────
class TestMemoryPressure:
    def test_comfortable_does_not_reclaim(self):
        p = A.MemoryPressure(level="comfortable", available_pct=50)
        assert p.should_reclaim is False

    def test_tight_reclaims(self):
        p = A.MemoryPressure(level="tight", available_pct=10)
        assert p.should_reclaim is True

    def test_critical_reclaims(self):
        p = A.MemoryPressure(level="critical", available_pct=3)
        assert p.should_reclaim is True

    def test_describe_contains_numbers(self):
        p = A.MemoryPressure(
            level="tight", available_mb=1000, total_mb=16000,
            available_pct=6.25,
        )
        text = p.describe()
        assert "tight" in text
        assert "1000" in text
        assert "16000" in text


# ─────────────────────────────────────────
# 噪声端口过滤（关键：防止 NTP 让 VM 永不回收）
# ─────────────────────────────────────────
class TestNoisePorts:
    def test_ntp_is_noise(self):
        """NTP 每几十秒一条连接，若算活动则 VM 永不休眠"""
        assert 123 in A._NOISE_PORTS

    def test_dns_is_noise(self):
        assert 53 in A._NOISE_PORTS

    def test_dhcp_is_noise(self):
        assert 67 in A._NOISE_PORTS
        assert 68 in A._NOISE_PORTS

    def test_web_port_is_not_noise(self):
        """8080 是用户服务，必须算活动"""
        assert 8080 not in A._NOISE_PORTS
        assert 22 not in A._NOISE_PORTS
        assert 80 not in A._NOISE_PORTS


# ─────────────────────────────────────────
# SLIRP 解析（用真实格式的输出样本）
# ─────────────────────────────────────────
class TestSlirpParsing:
    """用真实 `info usernet` 输出样本验证解析

    样本来自实际运行的 QEMU，确保格式匹配。
    """

    SAMPLE_IDLE = """Hub -1 (net0):
  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ
  TCP[HOST_FORWARD]  20       127.0.0.1  2224       10.0.2.15    22     0     0
  UDP[206 sec]       26               * 43067        0.0.0.0   123     0     0
  UDP[204 sec]       54       10.0.2.15 59694  119.28.183.184   123     0     0
"""

    def test_idle_sample_has_no_activity(self, tmp_path, monkeypatch):
        """只有 HOST_FORWARD + NTP 时应判定为 0（可回收）"""
        monkeypatch.setattr(
            A, "_monitor_query", lambda *a, **k: self.SAMPLE_IDLE
        )
        assert A.slirp_activity(tmp_path) == 0

    def test_established_connection_counts(self, tmp_path, monkeypatch):
        """有真实 TCP 连接时算活动"""
        sample = """Hub -1 (net0):
  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ
  TCP[HOST_FORWARD]  20       127.0.0.1  2224       10.0.2.15    22     0     0
  TCP[ESTABLISHED]   33       127.0.0.1  8080       10.0.2.15  8080     0     0
"""
        monkeypatch.setattr(
            A, "_monitor_query", lambda *a, **k: sample
        )
        assert A.slirp_activity(tmp_path) == 1

    def test_time_wait_is_ignored(self, tmp_path, monkeypatch):
        """TIME_WAIT 是已结束连接（探测自己留下的），不能算活动"""
        sample = """Hub -1 (net0):
  Protocol[State]    FD  Source Address  Port   Dest. Address  Port RecvQ SendQ
  TCP[HOST_FORWARD]  20       127.0.0.1  2224       10.0.2.15    22     0     0
  TCP[TIME_WAIT]     44       127.0.0.1  2224       10.0.2.15    22     0     0
"""
        monkeypatch.setattr(
            A, "_monitor_query", lambda *a, **k: sample
        )
        assert A.slirp_activity(tmp_path) == 0

    def test_empty_output(self, tmp_path, monkeypatch):
        monkeypatch.setattr(A, "_monitor_query", lambda *a, **k: "")
        assert A.slirp_activity(tmp_path) == 0

    def test_no_monitor_socket(self, tmp_path):
        """monitor socket 不存在时返回 0，不抛异常"""
        assert A.slirp_activity(tmp_path) == 0
