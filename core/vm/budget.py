"""内存预算管理：容器与虚拟机共用一个上限

设计
────
用户只需要在设置里配一次「内存上限」（已有功能，见
core/podman_config.py 的资源限制区块），容器和虚拟机
共用这个预算：

    宿主机总内存
      ├─ 系统与其他程序占用（不参与预算）
      └─ 用户设定的预算（默认宿主机的一半）
           ├─ 容器：各容器 mem_limit 之和
           └─ 虚拟机：各 VM 的 memory_mb 之和

启动前检查：容器已用 + VM 已用 + 本次申请 > 预算 → 拒绝。

为什么这样设计
──────────────
之前容器和 VM 各自独立判断「宿主机可用内存」，会出现：
  · 容器把内存吃满，VM 启动时才发现不够
  · VM 占着内存，容器启动被拒
  · 用户要在两个地方分别设阈值，容易混淆

共用一个预算后，用户只需理解一件事：这个数是我给
「Podman 管理器里所有东西」的内存总额。

预算为 0 表示不限制（沿用原有语义）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 默认预算占宿主机内存的比例（用户未设置时）
DEFAULT_BUDGET_RATIO = 0.5


@dataclass
class BudgetStatus:
    """内存预算使用情况"""

    budget_mb: int = 0           # 预算上限（MB，0=不限制）
    containers_mb: float = 0.0   # 容器已声明（mem_limit 之和）
    vms_mb: float = 0.0          # VM 已分配（memory_mb 之和）
    containers_actual_mb: float = 0.0   # 容器实际占用（cgroup）
    vms_actual_mb: float = 0.0          # VM 实际占用（RSS）
    host_total_mb: int = 0
    host_available_mb: int = 0

    @property
    def unlimited(self) -> bool:
        return self.budget_mb <= 0

    @property
    def declared_mb(self) -> float:
        """已声明（容器限额 + VM 分配）"""
        return self.containers_mb + self.vms_mb

    @property
    def actual_mb(self) -> float:
        """实际占用（容器 cgroup + VM RSS）"""
        return self.containers_actual_mb + self.vms_actual_mb

    @property
    def remaining_mb(self) -> float:
        if self.unlimited:
            return float(self.host_available_mb)
        return max(0.0, self.budget_mb - self.declared_mb)

    @property
    def used_percent(self) -> float:
        if self.unlimited:
            return 0.0
        return self.declared_mb / self.budget_mb * 100

    def can_allocate(self, need_mb: int) -> tuple[bool, str]:
        """检查是否还能分配 need_mb

        返回 (允许, 提示信息)
        """
        if self.unlimited:
            return True, ""

        after = self.declared_mb + need_mb
        if after <= self.budget_mb:
            return True, ""

        return False, (
            f"内存预算不足：已用 {self.declared_mb:.0f} MB / "
            f"预算 {self.budget_mb} MB，本次需 {need_mb} MB。\n\n"
            f"容器占用 {self.containers_mb:.0f} MB，"
            f"虚拟机占用 {self.vms_mb:.0f} MB。\n"
            "可在「设置 → 服务」中调整内存上限，"
            "或先停止部分容器/虚拟机。"
        )

    def display(self) -> str:
        """概览用的一行摘要"""
        if self.unlimited:
            return "未设置内存上限"
        return (
            f"{self.declared_mb:.1f} / {self.budget_mb} GB"
        )


def get_budget_mb() -> int:
    """读取内存预算（MB，0=不限制）

    复用 podman_config 里已有的「资源限制」设置（单位 GB），
    保证用户只需在一处配置。
    """
    from core.vm import memory as vm_memory

    # 用户设置优先
    try:
        from core import podman_config as cfg

        gb = cfg.get_memory_limit_gb()
        if gb and gb > 0:
            return int(gb * 1024)
    except Exception:
        pass

    # 未设置时给一个合理默认值：宿主机内存的一半
    # 注意：这里只作为「建议值」参与计算，不写入设置，
    # 避免用户没主动设置却被悄悄限制。
    try:
        return int(vm_memory.host_memory().total_mb * DEFAULT_BUDGET_RATIO)
    except Exception:
        return 0


def container_declared_mb(client) -> float:
    """容器已声明的内存（所有运行容器 mem_limit 之和，MB）"""
    try:
        from core import podman_config as cfg

        usage = cfg.compute_usage(client)
        return float(usage.get("declared_gb", 0.0)) * 1024
    except Exception:
        return 0.0


def container_actual_mb(client) -> float:
    """容器实际占用（MB）"""
    try:
        from core import podman_config as cfg

        usage = cfg.compute_usage(client)
        return float(usage.get("actual_gb", 0.0)) * 1024
    except Exception:
        return 0.0


def vm_allocated_mb() -> float:
    """所有运行中 VM 的已分配内存之和（MB）"""
    try:
        from core.vm.manager import load_all

        return float(sum(
            vm.spec.memory_mb for vm in load_all() if vm.running
        ))
    except Exception:
        return 0.0


def vm_actual_mb() -> float:
    """所有运行中 VM 的实际占用之和（MB）"""
    try:
        from core.vm.manager import load_all
        from core.vm import memory as vm_memory

        total = 0.0
        for vm in load_all():
            if vm.running:
                try:
                    total += vm_memory.vm_mem_usage(vm).rss_mb
                except Exception:
                    continue
        return total
    except Exception:
        return 0.0


def status(client=None) -> BudgetStatus:
    """读取当前内存预算使用情况"""
    from core.vm import memory as vm_memory

    st = BudgetStatus()
    st.budget_mb = get_budget_mb()

    try:
        h = vm_memory.host_memory()
        st.host_total_mb = h.total_mb
        st.host_available_mb = h.available_mb
    except Exception:
        pass

    if client is not None:
        st.containers_mb = container_declared_mb(client)
        st.containers_actual_mb = container_actual_mb(client)

    st.vms_mb = vm_allocated_mb()
    st.vms_actual_mb = vm_actual_mb()

    return st


def check_vm_start(client, need_mb: int) -> tuple[bool, str]:
    """启动 VM 前的内存预算检查

    与容器的 check_start_allowed 共用同一套预算口径。
    """
    st = status(client)
    return st.can_allocate(need_mb)
