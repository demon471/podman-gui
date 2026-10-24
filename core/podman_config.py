"""Podman 配置管理

管理用户级配置文件：
- ~/.config/containers/registries.conf   — 镜像仓库、加速镜像
- ~/.config/containers/containers.conf   — 引擎配置（代理、存储等）
- ~/.config/containers/storage.conf      — 存储驱动、graphRoot

只改用户级配置，不动 /etc 下的系统级配置。
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "containers"
REGISTRIES_CONF = CONFIG_DIR / "registries.conf"
CONTAINERS_CONF = CONFIG_DIR / "containers.conf"
STORAGE_CONF = CONFIG_DIR / "storage.conf"

# ─────────────────────────────────────────
# 国内常用镜像加速地址
# ─────────────────────────────────────────
MIRROR_PRESETS: list[tuple[str, str]] = [
    ("阿里云（需替换个人 ID）", "https://<你的ID>.mirror.aliyuncs.com"),
    ("中科大 USTC", "https://docker.mirrors.ustc.edu.cn"),
    ("网易 163", "https://hub-mirror.c.163.com"),
    ("腾讯云", "https://mirror.ccs.tencentyun.com"),
    ("DaoCloud", "https://docker.m.daocloud.io"),
    ("南京大学 NJU", "https://docker.nju.edu.cn"),
    ("上海交大 SJTU", "https://docker.mirrors.sjtug.sjtu.edu.cn"),
]

# 常用代理预设
PROXY_PRESETS: list[tuple[str, str]] = [
    ("本机 7890（Clash/FlClash）", "http://127.0.0.1:7890"),
    ("本机 7897（Clash Verge）", "http://127.0.0.1:7897"),
    ("本机 1080（SOCKS5）", "socks5://127.0.0.1:1080"),
    ("本机 8080", "http://127.0.0.1:8080"),
]


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────
# registries.conf
# ─────────────────────────────────────────
def read_registries() -> dict:
    """读取 registries.conf

    返回:
        {
            "mirrors": {registry: [url, ...]},
            "unqualified_search": [registry, ...],
            "raw": 原始文本,
        }
    """
    out = {"mirrors": {}, "unqualified_search": [], "raw": ""}
    if not REGISTRIES_CONF.exists():
        return out

    raw = REGISTRIES_CONF.read_text(encoding="utf-8")
    out["raw"] = raw

    # 解析 TOML（Python 3.11+ 内置 tomllib）
    try:
        import tomllib

        data = tomllib.loads(raw)
    except Exception:
        # tomllib 不可用或解析失败时用正则兜底
        return _parse_registries_fallback(raw, out)

    # 新格式 [[registry]] + [[registry.mirror]]
    for reg in data.get("registry", []):
        prefix = reg.get("prefix") or reg.get("location") or ""
        mirrors = [m.get("location", "") for m in reg.get("mirror", [])]
        mirrors = [m for m in mirrors if m]
        if prefix and mirrors:
            out["mirrors"][prefix] = mirrors

    out["unqualified_search"] = data.get("unqualified-search-registries", [])
    return out


def _parse_registries_fallback(raw: str, out: dict) -> dict:
    """正则兜底解析"""
    # unqualified-search-registries = ["docker.io", ...]
    m = re.search(r"unqualified-search-registries\s*=\s*\[(.*?)\]", raw, re.S)
    if m:
        out["unqualified_search"] = re.findall(r'"([^"]+)"', m.group(1))

    # 逐块找 [[registry]] ... [[registry.mirror]]
    blocks = re.split(r"\[\[registry\]\]", raw)
    for blk in blocks[1:]:
        prefix_m = re.search(r'(?:prefix|location)\s*=\s*"([^"]+)"', blk)
        if not prefix_m:
            continue
        prefix = prefix_m.group(1)
        mirrors = re.findall(
            r"\[\[registry\.mirror\]\][^\[]*?location\s*=\s*\"([^\"]+)\"",
            blk, re.S,
        )
        if mirrors:
            out["mirrors"][prefix] = mirrors
    return out


def write_registries(
    mirrors: dict[str, list[str]],
    unqualified_search: list[str] | None = None,
) -> None:
    """写入 registries.conf（TOML 格式）

    mirrors: {"docker.io": ["https://mirror1", "https://mirror2"], ...}
    """
    _ensure_dir()

    # 备份原文件
    if REGISTRIES_CONF.exists():
        backup = REGISTRIES_CONF.with_suffix(".conf.bak")
        shutil.copy2(REGISTRIES_CONF, backup)

    search = unqualified_search or [
        "docker.io", "quay.io", "registry.fedoraproject.org",
    ]

    lines = [
        "# 由 Podman 管理器生成",
        "# 修改后需重启 Podman 服务：systemctl --user restart podman.socket",
        "",
        "unqualified-search-registries = ["
        + ", ".join(f'"{r}"' for r in search) + "]",
        "",
    ]

    for prefix, urls in mirrors.items():
        if not urls:
            continue
        lines.append("[[registry]]")
        lines.append(f'prefix = "{prefix}"')
        lines.append('location = "%s"' % prefix)
        lines.append("")
        for url in urls:
            if not url.strip():
                continue
            lines.append("  [[registry.mirror]]")
            lines.append(f'  location = "{url.strip()}"')
            lines.append("")
        lines.append("")

    REGISTRIES_CONF.write_text("\n".join(lines), encoding="utf-8")


def clear_registries() -> None:
    """清空加速配置（恢复默认）"""
    if REGISTRIES_CONF.exists():
        backup = REGISTRIES_CONF.with_suffix(".conf.bak")
        shutil.copy2(REGISTRIES_CONF, backup)
        REGISTRIES_CONF.unlink()


# ─────────────────────────────────────────
# containers.conf（代理）
# ─────────────────────────────────────────
def read_proxy() -> dict:
    """读取 containers.conf 中的代理配置"""
    out = {"http_proxy": "", "https_proxy": "", "no_proxy": "", "raw": ""}
    if not CONTAINERS_CONF.exists():
        return out

    raw = CONTAINERS_CONF.read_text(encoding="utf-8")
    out["raw"] = raw

    try:
        import tomllib

        data = tomllib.loads(raw)
        engine = data.get("engine", {})
        env = engine.get("env", [])
        for item in env:
            if "=" not in item:
                continue
            k, v = item.split("=", 1)
            k = k.lower()
            if k in ("http_proxy", "https_proxy", "no_proxy"):
                out[k] = v
    except Exception:
        for key in ("http_proxy", "https_proxy", "no_proxy"):
            m = re.search(rf'{key}\s*=\s*"([^"]*)"', raw, re.I)
            if m:
                out[key] = m.group(1)

    return out


def write_proxy(http_proxy: str, https_proxy: str = "", no_proxy: str = "") -> None:
    """写入代理配置到 containers.conf

    保留文件中的其他配置项，只改 [engine] 的 env。
    """
    _ensure_dir()

    if CONTAINERS_CONF.exists():
        backup = CONTAINERS_CONF.with_suffix(".conf.bak")
        shutil.copy2(CONTAINERS_CONF, backup)

    existing = read_proxy()
    no_proxy = no_proxy or existing.get("no_proxy") or "localhost,127.0.0.1"

    env_items = []
    if http_proxy.strip():
        p = http_proxy.strip()
        env_items.append(f'"HTTP_PROXY={p}"')
        env_items.append(f'"http_proxy={p}"')
    if https_proxy.strip():
        p = https_proxy.strip()
        env_items.append(f'"HTTPS_PROXY={p}"')
        env_items.append(f'"https_proxy={p}"')
    if no_proxy.strip():
        np = no_proxy.strip()
        env_items.append(f'"NO_PROXY={np}"')
        env_items.append(f'"no_proxy={np}"')

    content = [
        "# 由 Podman 管理器生成",
        "# 修改后需重启 Podman 服务：systemctl --user restart podman.socket",
        "",
        "[engine]",
    ]
    if env_items:
        content.append("env = [")
        # TOML 数组元素之间必须有逗号
        for i, item in enumerate(env_items):
            comma = "," if i < len(env_items) - 1 else ""
            content.append(f"  {item}{comma}")
        content.append("]")
    content.append("")

    CONTAINERS_CONF.write_text("\n".join(content), encoding="utf-8")


def clear_proxy() -> None:
    """清除代理配置"""
    if CONTAINERS_CONF.exists():
        backup = CONTAINERS_CONF.with_suffix(".conf.bak")
        shutil.copy2(CONTAINERS_CONF, backup)
        CONTAINERS_CONF.unlink()


# ─────────────────────────────────────────
# storage.conf（graphRoot）
# ─────────────────────────────────────────
def read_storage() -> dict:
    """读取 storage.conf"""
    out = {"driver": "", "graphroot": "", "runroot": "", "raw": ""}
    if not STORAGE_CONF.exists():
        return out

    raw = STORAGE_CONF.read_text(encoding="utf-8")
    out["raw"] = raw

    try:
        import tomllib

        data = tomllib.loads(raw)
        storage = data.get("storage", {})
        out["driver"] = storage.get("driver", "")
        out["graphroot"] = storage.get("graphroot", "")
        out["runroot"] = storage.get("runroot", "")
    except Exception:
        for key in ("driver", "graphroot", "runroot"):
            m = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', raw, re.M)
            if m:
                out[key] = m.group(1)

    return out


def write_storage(driver: str, graphroot: str, runroot: str = "") -> None:
    """写入存储配置"""
    _ensure_dir()

    if STORAGE_CONF.exists():
        backup = STORAGE_CONF.with_suffix(".conf.bak")
        shutil.copy2(STORAGE_CONF, backup)

    lines = [
        "# 由 Podman 管理器生成",
        "# 修改后需重启 Podman 服务：systemctl --user restart podman.socket",
        "",
        "[storage]",
        f'driver = "{driver or "overlay"}"',
        f'graphroot = "{graphroot}"',
    ]
    if runroot:
        lines.append(f'runroot = "{runroot}"')
    lines.append("")

    STORAGE_CONF.write_text("\n".join(lines), encoding="utf-8")


def clear_storage() -> None:
    if STORAGE_CONF.exists():
        backup = STORAGE_CONF.with_suffix(".bak")
        shutil.copy2(STORAGE_CONF, backup)
        STORAGE_CONF.unlink()


# ─────────────────────────────────────────
# 服务重启
# ─────────────────────────────────────────
def restart_podman_socket() -> tuple[bool, str]:
    """重启用户级 podman.socket，使配置生效

    返回 (成功, 消息)
    """
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "podman.socket"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True, "Podman 服务已重启"
        return False, r.stderr.strip() or "重启失败"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def socket_status() -> tuple[bool, str]:
    """检查 podman.socket 状态"""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "podman.socket"],
            capture_output=True, text=True, timeout=5,
        )
        state = r.stdout.strip()
        return state == "active", state
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


# ─────────────────────────────────────────
# 磁盘空间
# ─────────────────────────────────────────
def disk_free(path: str) -> tuple[int, int]:
    """返回 (可用字节, 总字节)"""
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return 0, 0


# ─────────────────────────────────────────
# 开机自启（systemd user socket）
# ─────────────────────────────────────────
def autostart_status() -> dict:
    """查询 podman.socket 的自启与运行状态

    返回 {"enabled": bool, "enabled_raw": str, "active": bool, "active_raw": str}
    """
    def _run(*args):
        try:
            r = subprocess.run(
                ["systemctl", "--user", *args],
                capture_output=True, text=True, timeout=8,
            )
            return r.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return "unknown"

    en = _run("is-enabled", "podman.socket")
    ac = _run("is-active", "podman.socket")
    return {
        "enabled": en == "enabled",
        "enabled_raw": en,
        "active": ac == "active",
        "active_raw": ac,
    }


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """开启/关闭 podman.socket 开机自启

    开启时会同时立即启动 socket，关闭时仅取消自启（不停止当前运行）。
    返回 (成功, 消息)
    """
    action = "enable" if enabled else "disable"
    args = ["systemctl", "--user", action, "podman.socket"]
    if enabled:
        args.append("--now")

    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)

    if r.returncode == 0:
        return True, f"已{'开启' if enabled else '关闭'}开机自启"
    return False, (r.stderr or r.stdout).strip() or "操作失败"


# ─────────────────────────────────────────
# 系统级服务（rootful）—— 需要提权
# ─────────────────────────────────────────
def system_service_status() -> dict:
    """查询系统级 podman.socket 状态（rootful）"""
    def _run(*args):
        try:
            r = subprocess.run(
                ["systemctl", *args],
                capture_output=True, text=True, timeout=8,
            )
            return r.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return "unknown"

    en = _run("is-enabled", "podman.socket")
    ac = _run("is-active", "podman.socket")
    return {
        "enabled": en == "enabled",
        "enabled_raw": en,
        "active": ac == "active",
        "active_raw": ac,
    }


def _run_privileged(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    """以提权方式执行命令

    优先用 pkexec（弹图形授权框），失败则回退 sudo -n（免密）。
    返回 (成功, 输出/错误)
    """
    # 1. pkexec：KDE 下会弹出 polkit 认证对话框
    if shutil.which("pkexec"):
        try:
            r = subprocess.run(
                ["pkexec"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if r.returncode == 0:
                return True, r.stdout.strip()
            err = (r.stderr or r.stdout).strip()
            # 126/127 通常表示用户取消授权
            if r.returncode in (126, 127):
                return False, "已取消授权"
            return False, err or f"退出码 {r.returncode}"
        except subprocess.TimeoutExpired:
            return False, "操作超时"
        except OSError as e:
            return False, str(e)

    # 2. 回退 sudo -n（仅在已配置免密时有效，不会卡住等密码）
    if shutil.which("sudo"):
        try:
            r = subprocess.run(
                ["sudo", "-n"] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            if r.returncode == 0:
                return True, r.stdout.strip()
            return False, (r.stderr or "").strip() or "需要密码"
        except (subprocess.TimeoutExpired, OSError) as e:
            return False, str(e)

    return False, "未找到 pkexec 或 sudo"


def set_system_autostart(enabled: bool) -> tuple[bool, str]:
    """开启/关闭系统级 podman.socket 开机自启（需提权）"""
    action = "enable" if enabled else "disable"
    args = ["systemctl", action, "podman.socket"]
    if enabled:
        args.append("--now")
    ok, msg = _run_privileged(args)
    if ok:
        return True, f"系统级已{'开启' if enabled else '关闭'}开机自启"
    return False, msg


def system_service_action(action: str) -> tuple[bool, str]:
    """系统级服务的启动/停止/重启（需提权）"""
    ok, msg = _run_privileged(["systemctl", action, "podman.socket"])
    label = {"start": "启动", "stop": "停止", "restart": "重启"}.get(action, action)
    if ok:
        return True, f"系统级服务{label}成功"
    return False, msg


# ─────────────────────────────────────────
# 配置安全写入
# ─────────────────────────────────────────
def validate_toml(path) -> tuple[bool, str]:
    """校验 TOML 文件是否可解析

    返回 (有效, 错误信息)
    """
    import tomllib

    p = Path(path)
    if not p.exists():
        return True, ""
    try:
        tomllib.loads(p.read_text(encoding="utf-8"))
        return True, ""
    except tomllib.TOMLDecodeError as e:
        return False, str(e)
    except OSError as e:
        return False, str(e)


def safe_write_config(kind: str, *args, **kwargs) -> tuple[bool, str]:
    """写入配置并校验，失败则自动回滚

    kind: "proxy" | "registries" | "storage"
    返回 (成功, 消息)
    """
    targets = {
        "proxy": CONTAINERS_CONF,
        "registries": REGISTRIES_CONF,
        "storage": STORAGE_CONF,
    }
    writers = {
        "proxy": write_proxy,
        "registries": write_registries,
        "storage": write_storage,
    }

    path = targets.get(kind)
    writer = writers.get(kind)
    if not path or not writer:
        return False, f"未知配置类型: {kind}"

    # 备份现有内容
    backup = None
    if path.exists():
        backup = path.read_text(encoding="utf-8")

    # 写入
    try:
        writer(*args, **kwargs)
    except Exception as e:
        if backup is not None:
            path.write_text(backup, encoding="utf-8")
        return False, f"写入失败: {e}"

    # 校验
    ok, err = validate_toml(path)
    if not ok:
        # 回滚
        if backup is not None:
            path.write_text(backup, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)
        return False, f"生成的配置格式错误，已回滚：\n{err}"

    return True, "配置已保存"


def restart_and_verify(timeout: int = 15) -> tuple[bool, str]:
    """重启 podman.socket 并验证是否真的可用

    失败时返回错误详情，便于用户定位。
    """
    import time

    # 1. 先清理失败状态
    try:
        subprocess.run(
            ["systemctl", "--user", "reset-failed", "podman.socket", "podman.service"],
            capture_output=True, text=True, timeout=8,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass

    # 2. 重启
    ok, msg = restart_podman_socket()
    if not ok:
        return False, f"重启失败：{msg}"

    # 3. 等待并验证
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            from core.podman_client import PodmanClient

            client = PodmanClient()
            if client.ping():
                return True, "服务已重启并连接正常"
        except Exception:
            pass
        time.sleep(0.5)

    # 4. 失败 → 收集诊断信息
    detail = _diagnose_service_failure()
    return False, detail


def _diagnose_service_failure() -> str:
    """收集服务启动失败的诊断信息"""
    lines = ["Podman 服务启动失败。"]

    # 检查配置文件
    for name, path in [
        ("containers.conf", CONTAINERS_CONF),
        ("registries.conf", REGISTRIES_CONF),
        ("storage.conf", STORAGE_CONF),
    ]:
        if path.exists():
            ok, err = validate_toml(path)
            if not ok:
                lines.append(f"\n✗ {name} 格式错误：\n  {err}")
                lines.append(f"  文件：{path}")

    # 取 systemd 日志
    try:
        r = subprocess.run(
            ["journalctl", "--user", "-u", "podman.service",
             "--no-pager", "-n", "8", "-o", "cat"],
            capture_output=True, text=True, timeout=8,
        )
        log = (r.stdout or "").strip()
        if log:
            lines.append("\n最近日志：")
            for line in log.splitlines()[-6:]:
                lines.append(f"  {line}")
    except (subprocess.TimeoutExpired, OSError):
        pass

    lines.append("\n可在「设置 → 服务」中查看状态，或手动执行：")
    lines.append("  systemctl --user status podman.service")

    return "\n".join(lines)


# ─────────────────────────────────────────
# 容器内存限额管理
#
# 原理：Podman 没有全局容器内存上限，但每个容器可通过 --memory
# 设置 mem_limit。这里在「启动容器」前预检查：
#   已运行容器 mem_limit 之和 + 本次要启动的 limit > 阈值 → 拒绝启动
#
# 阈值保存在 QSettings（不写入 Podman 配置），默认 0 表示不限制。
# ─────────────────────────────────────────

def get_memory_limit_gb() -> float:
    """读取用户设置的内存阈值（GB，0 表示不限制）"""
    from PyQt6.QtCore import QSettings

    s = QSettings("podman-gui", "podman-gui")
    try:
        return float(s.value("limits/memory_gb", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def set_memory_limit_gb(gb: float) -> None:
    """保存内存阈值（GB，0 表示不限制）"""
    from PyQt6.QtCore import QSettings

    s = QSettings("podman-gui", "podman-gui")
    s.setValue("limits/memory_gb", float(gb))
    s.sync()


def container_memory_bytes(info: dict) -> int:
    """从 inspect 结果取容器内存限制（字节，0 表示无限制）"""
    hc = info.get("HostConfig") or {}
    try:
        return int(hc.get("Memory") or 0)
    except (TypeError, ValueError):
        return 0


def compute_usage(client) -> dict:
    """统计当前内存使用情况

    返回:
        {
            "limit_gb":   阈值（GB，0=不限制）,
            "declared_gb": 所有运行容器 mem_limit 之和（GB）,
            "actual_gb":  实际占用（GB，来自 cgroup）,
            "host_gb":    整机内存（GB）,
            "containers": [{"name","state","limit_gb","actual_gb"}...],
            "unlimited":  [无 mem_limit 的容器名],
        }
    """
    out = {
        "limit_gb": get_memory_limit_gb(),
        "declared_gb": 0.0,
        "actual_gb": 0.0,
        "host_gb": 0.0,
        "containers": [],
        "unlimited": [],
    }

    # 整机内存
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    out["host_gb"] = kb / 1024 / 1024
                    break
    except (OSError, ValueError, IndexError):
        pass

    # 各容器
    try:
        ctrs = client.list_containers(all=True)
    except Exception:
        return out

    for c in ctrs:
        name = (c.get("Names") or ["?"])[0]
        state = str(c.get("State", "")).lower()
        try:
            info = client.inspect_container(c.get("Id", ""))
        except Exception:
            continue

        limit = container_memory_bytes(info)
        limit_gb = limit / 1024 ** 3

        # 实际占用：从 cgroup 读（需拼上 /sys/fs/cgroup 前缀）
        actual_gb = 0.0
        cg = info.get("State", {}).get("CgroupPath") or ""
        if cg:
            full = cg if cg.startswith("/sys/fs/cgroup") else "/sys/fs/cgroup" + cg
            for fname in ("memory.current", "memory.usage_in_bytes"):
                try:
                    with open(f"{full}/{fname}") as f:
                        actual_gb = int(f.read().strip()) / 1024 ** 3
                    break
                except (OSError, ValueError):
                    continue

        out["containers"].append({
            "name": name,
            "state": state,
            "limit_gb": limit_gb,
            "actual_gb": actual_gb,
            "unlimited": limit == 0,
        })

        if state == "running":
            out["declared_gb"] += limit_gb
            out["actual_gb"] += actual_gb
            if limit == 0:
                out["unlimited"].append(name)

    return out


def check_start_allowed(client, target_cid: str) -> tuple[bool, str]:
    """启动前预检查：加上目标容器后是否超过阈值

    返回 (允许, 说明)
    """
    limit_gb = get_memory_limit_gb()
    if limit_gb <= 0:
        return True, ""      # 未设置限制

    try:
        ctrs = client.list_containers(all=True)
    except Exception:
        return True, ""      # 查不到就放行，不阻塞用户

    # 已运行容器（排除目标自己）的 limit 之和
    used = 0.0
    for c in ctrs:
        if c.get("Id") == target_cid:
            continue
        if str(c.get("State", "")).lower() != "running":
            continue
        try:
            info = client.inspect_container(c.get("Id", ""))
        except Exception:
            continue
        used += container_memory_bytes(info) / 1024 ** 3

    # 目标容器的 limit
    try:
        tinfo = client.inspect_container(target_cid)
        target_gb = container_memory_bytes(tinfo) / 1024 ** 3
    except Exception:
        target_gb = 0.0

    total = used + target_gb
    if total > limit_gb + 1e-6:
        return False, (
            f"内存超限，无法启动。\n\n"
            f"  当前阈值：{limit_gb:.2f} GB\n"
            f"  已占用：  {used:.2f} GB（运行中容器的 mem_limit 之和）\n"
            f"  本容器：  {target_gb:.2f} GB\n"
            f"  合计：    {total:.2f} GB\n\n"
            f"请先停止其他容器，或在「设置 → 资源限制」中提高阈值。"
        )

    return True, ""


def check_compose_allowed(project_name: str, services_gb: float) -> tuple[bool, str]:
    """Compose 项目启动前预检查"""
    limit_gb = get_memory_limit_gb()
    if limit_gb <= 0:
        return True, ""

    try:
        from core.podman_client import get_client

        client = get_client()
        ctrs = client.list_containers(all=True)
    except Exception:
        return True, ""

    used = 0.0
    for c in ctrs:
        if str(c.get("State", "")).lower() != "running":
            continue
        try:
            info = client.inspect_container(c.get("Id", ""))
        except Exception:
            continue
        used += container_memory_bytes(info) / 1024 ** 3

    total = used + services_gb
    if total > limit_gb + 1e-6:
        return False, (
            f"内存超限，无法启动 {project_name}。\n\n"
            f"  当前阈值：{limit_gb:.2f} GB\n"
            f"  已占用：  {used:.2f} GB\n"
            f"  本项目：  {services_gb:.2f} GB\n"
            f"  合计：    {total:.2f} GB\n\n"
            f"请先停止其他项目，或提高阈值。"
        )
    return True, ""
