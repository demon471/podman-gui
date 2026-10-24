"""虚拟机功能自动初始化

设计原则：全部默认启用，不暴露配置项。

用户在首次使用时不需要理解 KSM、气球回收、DNS 转发这些概念，
应用启动时静默把能开的都开好。失败也不弹窗打断（只写日志），
用户真正需要知道的信息在虚拟机页面按需展示。

三项自动初始化
──────────────
1. KSM（内核同页合并）
   多 VM 同时运行时合并相同内存页，省内存。
   需要写 /sys/kernel/mm/ksm/run，即 root。

2. 气球自动回收
   空闲 VM 的空闲内存归还宿主机。纯虚拟机内机制
   （QEMU virtio-balloon + free-page-reporting），不需要 root。

3. 域名解析
   让 <vm>.vm.local 可访问。需要写 /etc 下两个配置文件，即 root。

sudo 处理
─────────
不主动弹窗要密码（启动时打断用户很烦）。
策略：仅在 sudo 免密可用时自动配置；
     需要密码的项保持未配置状态，等用户在需要时触发
     （例如打开虚拟机页面时提示一次，或配置按钮）。

这样既满足「默认启用」，又不会在启动时弹密码框。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from core.vm import dns as vm_dns
from core.vm import memory as vm_memory


@dataclass
class InitResult:
    """自动初始化结果"""

    ksm: bool = False
    ksm_detail: str = ""
    balloon: bool = True          # 气球默认就是开着的（QEMU 参数）
    dns: bool = False
    dns_detail: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return self.ksm and self.dns


_lock = threading.Lock()
_done = False
_result: InitResult | None = None


def ensure_once(*, blocking: bool = False) -> InitResult | None:
    """执行一次自动初始化（幂等）

    blocking=False（默认）：后台线程执行，立即返回 None。
    blocking=True：同步执行并返回结果。
    """
    global _done

    with _lock:
        if _done:
            return _result
        if not blocking:
            _done = True   # 占位，避免并发重复触发

    if not blocking:
        t = threading.Thread(
            target=_run, name="vm-autoconfig", daemon=True
        )
        t.start()
        return None

    _run()
    return _result


def _run() -> None:
    """实际执行（可能被后台线程调用）"""
    global _result

    res = InitResult()

    # 1. 域名解析（最影响体验，优先）
    try:
        st = vm_dns.status()
        if st.ready:
            res.dns = True
            res.dns_detail = "已启用"
        elif not st.dnsmasq_installed:
            res.dns_detail = "缺少 dnsmasq"
        elif _sudo_nopass_available():
            ok, err = vm_dns.configure()
            res.dns = ok
            res.dns_detail = "已自动启用" if ok else err[:120]
            if not ok:
                res.errors.append(f"域名解析: {err[:120]}")
        else:
            res.dns_detail = "需要管理员权限（首次使用时配置）"
    except Exception as e:
        res.dns_detail = str(e)[:120]
        res.errors.append(f"域名解析: {e}")

    # 2. KSM（只在免密时自动开；KSM 是系统级共享资源，
    #    启停会影响所有进程，不适合反复打扰用户）
    try:
        if not vm_memory.ksm_available():
            res.ksm_detail = "内核不支持"
        elif vm_memory.ksm_status().enabled:
            res.ksm = True
            res.ksm_detail = "已启用"
        elif _sudo_nopass_available():
            ok, err = vm_memory.enable_ksm()
            res.ksm = ok
            res.ksm_detail = "已自动启用" if ok else err[:120]
            if ok:
                # 顺便装开机自启，避免重启后失效
                try:
                    vm_memory.install_ksm_service()
                except Exception:
                    pass
            else:
                res.errors.append(f"KSM: {err[:120]}")
        else:
            res.ksm_detail = "需要管理员权限"
    except Exception as e:
        res.ksm_detail = str(e)[:120]
        res.errors.append(f"KSM: {e}")

    # 3. 气球回收：QEMU 启动参数已带，无需额外操作
    res.balloon = True

    _result = res


def _sudo_nopass_available() -> bool:
    """sudo 是否免密（用于判断能否静默配置）"""
    import subprocess
    from pathlib import Path

    if not Path("/usr/bin/sudo").exists():
        return False
    try:
        r = subprocess.run(
            ["/usr/bin/sudo", "-n", "true"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def last_result() -> InitResult | None:
    """上次初始化结果（未执行过返回 None）"""
    return _result


def needs_attention() -> tuple[bool, str]:
    """是否有需要用户处理的项（实时查询，不依赖初始化结果）

    返回 (需要处理, 说明文案)。虚拟机页面用它决定是否显示提示。
    """
    missing = []

    # 域名解析
    try:
        st = vm_dns.status()
        if not st.ready:
            if st.dnsmasq_installed:
                missing.append(
                    f"域名解析（*.{vm_dns.DOMAIN_SUFFIX}）"
                )
            else:
                missing.append("域名解析（需安装 dnsmasq）")
    except Exception:
        missing.append("域名解析")

    # KSM
    try:
        if vm_memory.ksm_available() and not vm_memory.ksm_status().enabled:
            missing.append("KSM 内存合并")
    except Exception:
        pass

    if not missing:
        return False, ""

    if missing:
        return True, (
            f"以下优化可自动启用：{'、'.join(missing)}。"
            "需要一次管理员权限。"
        )
    return False, ""
