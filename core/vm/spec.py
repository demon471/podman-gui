"""机器规格与共享常量

原先放在 qemu.py 里；纯容器化后 QEMU 整个模块删除，
规格独立成模块（manager / network / ui 都要引用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 新建机器的默认登录密码（详情页可改，改完记录在 state 里）
DEFAULT_PASSWORD = "12345"


@dataclass
class PortForward:
    """端口记录

    容器挂在共享网桥上，端口本来就是直通的，
    这里的记录只用于界面展示与统计。
    """

    host_port: int
    guest_port: int
    protocol: str = "tcp"          # tcp | udp
    host_addr: str = "127.0.0.1"   # 绑定地址，0.0.0.0 表示局域网可访问


@dataclass
class VMSpec:
    """机器运行配置（持久化到 vm.json + SQLite）

    名字保留 VMSpec：全代码库大量 vm.spec.xxx 引用，改名属于
    PodBox 收尾阶段一起做（见 docs/rename.md）。
    memory_mb / cpus / disk_gb 仅为 SQLite 列兼容保留（填 0），
    容器档没有独立内核，这些概念不存在，界面不再展示。
    """

    name: str
    image_key: str
    arch: str = "amd64"
    memory_mb: int = 0
    cpus: int = 0
    disk_gb: int = 0
    username: str = "dev"
    hostname: str = ""
    # 端口记录（展示用）
    forwards: list[PortForward] = field(default_factory=list)
    # 共享目录：宿主机路径 → 机器内挂载点
    shares: list[tuple[str, str]] = field(default_factory=list)
    # 是否开机自启（随应用启动）
    autostart: bool = False
    # 创建时间
    created_at: float = 0.0

    def __post_init__(self):
        if not self.hostname:
            self.hostname = self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "image_key": self.image_key,
            "arch": self.arch,
            "memory_mb": self.memory_mb,
            "cpus": self.cpus,
            "disk_gb": self.disk_gb,
            "username": self.username,
            "hostname": self.hostname,
            "forwards": [
                {
                    "host_port": f.host_port,
                    "guest_port": f.guest_port,
                    "protocol": f.protocol,
                    "host_addr": f.host_addr,
                }
                for f in self.forwards
            ],
            "shares": [list(s) for s in self.shares],
            "autostart": self.autostart,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "VMSpec":
        return cls(
            name=d["name"],
            image_key=d["image_key"],
            arch=d.get("arch", "amd64"),
            memory_mb=d.get("memory_mb", 0),
            cpus=d.get("cpus", 0),
            disk_gb=d.get("disk_gb", 0),
            username=d.get("username", "dev"),
            hostname=d.get("hostname", d["name"]),
            forwards=[
                PortForward(
                    host_port=f["host_port"],
                    guest_port=f["guest_port"],
                    protocol=f.get("protocol", "tcp"),
                    host_addr=f.get("host_addr", "127.0.0.1"),
                )
                for f in d.get("forwards", [])
            ],
            shares=[tuple(s) for s in d.get("shares", [])],
            autostart=d.get("autostart", False),
            created_at=d.get("created_at", 0.0),
        )
