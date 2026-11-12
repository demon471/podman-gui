"""容器内文件浏览（只读）

用 `podman exec` 执行 `ls` 列目录，不依赖 SSH / SFTP ——
容器里通常没有 sshd，也不需要为了看一眼目录去装东西。

设计约束：
  · **只读**：只提供列目录，不提供上传/下载/删除/重命名
  · 不依赖镜像内容：优先 ls，没有 ls 时退回 sh 内建能力，
    两者都没有就明确告诉用户（distroless 类镜像）
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

# 列目录超时：容器卡死时不能把界面挂住
TIMEOUT = 20


@dataclass
class Entry:
    name: str
    is_dir: bool
    is_link: bool
    size: int
    perms: str
    mtime: str
    target: str = ""          # 符号链接指向
    is_mount: bool = False    # 是否是挂载点（宿主机目录/卷）


def _run(cid: str, argv: list[str]) -> tuple[bool, str]:
    """在容器里执行命令，返回 (成功, 输出)"""
    try:
        r = subprocess.run(
            ["podman", "exec", cid, *argv],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except FileNotFoundError:
        return False, "找不到 podman 命令"
    except subprocess.TimeoutExpired:
        return False, f"命令超时（{TIMEOUT}s），容器可能无响应"
    except OSError as e:
        return False, str(e)

    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()
        return False, detail[:300] or "执行失败"
    return True, r.stdout or ""


def mounts(cid: str) -> dict[str, str]:
    """容器挂载点：{容器内路径: 宿主机来源}

    用来在界面上标出「这个目录其实是宿主机上的」。
    """
    try:
        r = subprocess.run(
            ["podman", "inspect", cid,
             "--format", "{{json .Mounts}}"],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}

    if r.returncode != 0:
        return {}

    import json

    try:
        data = json.loads(r.stdout or "[]")
    except ValueError:
        return {}

    out: dict[str, str] = {}
    for m in data or []:
        dest = (m.get("Destination") or "").rstrip("/") or "/"
        src = m.get("Source") or ""
        name = m.get("Name") or ""        # 命名卷
        out[dest] = src or (f"卷 {name}" if name else "")
    return out


def _parse_ls(text: str) -> list[Entry]:
    """解析 `ls -la` 输出

    行格式：
      drwxr-xr-x  2 root root 4096 Nov  1 19:04 bin
      lrwxrwxrwx  1 root root    7 Sep 18 05:32 lib -> usr/lib
    """
    out: list[Entry] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if line.startswith("total "):
            continue

        parts = line.split(None, 8)
        if len(parts) < 9:
            continue

        perms, _links, _owner, _group, size_s, mon, day, time_or_year, rest = parts
        if not perms or perms[0] not in "dl-bcsp":
            continue

        name = rest
        target = ""
        if " -> " in name:
            name, target = name.split(" -> ", 1)

        if name in (".", ".."):
            continue

        try:
            size = int(size_s)
        except ValueError:
            size = 0

        out.append(Entry(
            name=name,
            is_dir=perms.startswith("d"),
            is_link=perms.startswith("l"),
            size=size,
            perms=perms,
            mtime=f"{mon} {day} {time_or_year}",
            target=target,
        ))

    out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return out


def list_dir(cid: str, path: str, mount_map: dict[str, str] | None = None
             ) -> tuple[bool, list[Entry] | str]:
    """列出容器内目录

    返回 (成功, 条目列表 或 错误信息)
    """
    p = (path or "/").strip() or "/"

    # -a 显示隐藏项；-l 长格式；LANG=C 保证英文月份，解析稳定
    ok, out = _run(cid, ["sh", "-c", f"LANG=C ls -la {shlex.quote(p)} 2>&1"])
    if not ok:
        # 极简镜像可能没有 sh，直接试 ls
        ok, out = _run(cid, ["ls", "-la", p])
    if not ok:
        return False, out

    entries = _parse_ls(out)
    if not entries and out.strip() and "No such file" in out:
        return False, f"目录不存在：{p}"

    mm = mount_map if mount_map is not None else mounts(cid)
    mount_dirs = set(mm.keys())
    for e in entries:
        full = (p.rstrip("/") + "/" + e.name) if p != "/" else "/" + e.name
        if full in mount_dirs:
            e.is_mount = True

    return True, entries


def mount_label(path: str, mount_map: dict[str, str]) -> str:
    """某个挂载点对应宿主机上的什么（用于提示）"""
    return mount_map.get(path.rstrip("/") or "/", "")
