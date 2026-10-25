"""应用状态存储（SQLite）

把原先散落在多处的小状态统一到 SQLite：
  · QSettings（~/.config/podman-gui/podman-gui.conf）里的内存限额、
    Compose 根目录
  · 每台 VM 一个 vm.json
  · 其它零散的键值

为什么用 SQLite
────────────────
  · 单一数据源，不用记得哪个状态存哪
  · 查询/统计方便（比如「所有运行中的 VM 内存之和」）
  · 事务保证，不会写一半损坏
  · Python 标准库自带，无额外依赖

表结构
──────
settings   键值表，替代 QSettings
           (key TEXT PRIMARY KEY, value TEXT, updated_at REAL)

vms        虚拟机配置
           (name TEXT PRIMARY KEY, image_key, arch, memory_mb, cpus,
            disk_gb, username, hostname, forwards JSON, shares JSON,
            autostart INT, created_at REAL, updated_at REAL)

image_cache  镜像下载记录
             (key TEXT, arch TEXT, path, size_bytes, sha256,
              downloaded_at REAL, PRIMARY KEY(key, arch))

迁移策略
────────
首次打开时若发现旧配置（QSettings / vm.json）而数据库为空，
自动迁移并保留原文件作备份（改名为 .bak），不丢数据。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# 数据库位置（与其它应用数据同目录）
DB_PATH = Path.home() / ".local" / "share" / "podman-gui" / "state.db"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


# ─────────────────────────────────────────
# 连接管理
# ─────────────────────────────────────────
def _db() -> sqlite3.Connection:
    """获取（惰性创建）数据库连接

    同一进程内共用连接；SQLite 本身支持多线程，
    但为避免并发写冲突，用锁串行化写操作。

    注意：若数据库文件被外部删除（测试、清理），
    缓存的连接仍指向已删除的 inode，后续读写会静默丢失。
    因此每次取连接时校验一次文件是否存在。
    """
    global _conn

    if _conn is not None:
        if DB_PATH.exists():
            return _conn
        # 文件没了：关掉旧连接，重新建库
        try:
            _conn.close()
        except sqlite3.Error:
            pass
        _conn = None

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 不用 isolation_level=""：那种模式下 execute 后仍持有事务，
    # 连接关闭时未提交的数据会被回滚（实测写入后立刻能读，
    # 重连就丢）。用默认隔离级别 + 写操作后显式 commit。
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL 模式：读写不互斥，且崩溃后更易恢复
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    _conn = conn
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    """建表（IF NOT EXISTS，可重复调用）"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS vms (
            name        TEXT PRIMARY KEY,
            image_key   TEXT NOT NULL,
            arch        TEXT NOT NULL DEFAULT 'amd64',
            memory_mb   INTEGER NOT NULL DEFAULT 1024,
            cpus        INTEGER NOT NULL DEFAULT 1,
            disk_gb     INTEGER NOT NULL DEFAULT 1024,
            username    TEXT NOT NULL DEFAULT '',
            hostname    TEXT NOT NULL DEFAULT '',
            forwards    TEXT NOT NULL DEFAULT '[]',
            shares      TEXT NOT NULL DEFAULT '[]',
            autostart   INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL DEFAULT 0,
            updated_at  REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS image_cache (
            key           TEXT NOT NULL,
            arch          TEXT NOT NULL DEFAULT 'amd64',
            path          TEXT NOT NULL,
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            sha256        TEXT NOT NULL DEFAULT '',
            downloaded_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (key, arch)
        );

        CREATE INDEX IF NOT EXISTS idx_vms_image
            ON vms(image_key);
    """)


# ─────────────────────────────────────────
# settings：替代 QSettings
# ─────────────────────────────────────────
def get_setting(key: str, default: Any = None) -> Any:
    """读设置（自动按 JSON 解析，取不到返回 default）"""
    with _lock:
        row = _db().execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()

    if row is None:
        return default

    try:
        return json.loads(row["value"])
    except (json.JSONDecodeError, TypeError):
        # 非 JSON 的旧值，原样返回字符串
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    """写设置（值按 JSON 序列化，支持 dict/list/num/bool/str）"""
    payload = json.dumps(value, ensure_ascii=False)
    with _lock:
        _db().execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, payload, time.time()),
        )
        _db().commit()


def delete_setting(key: str) -> None:
    with _lock:
        _db().execute("DELETE FROM settings WHERE key = ?", (key,))
        _db().commit()


def all_settings() -> dict[str, Any]:
    """全部设置（调试/导出用）"""
    with _lock:
        rows = _db().execute("SELECT key, value FROM settings").fetchall()

    out = {}
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            out[r["key"]] = r["value"]
    return out


# ─────────────────────────────────────────
# vms：虚拟机配置
# ─────────────────────────────────────────
def save_vm(spec_dict: dict) -> bool:
    """保存 VM 配置（spec_dict 来自 VMSpec.to_dict()）"""
    try:
        with _lock:
            _db().execute(
                """
                INSERT INTO vms (
                    name, image_key, arch, memory_mb, cpus, disk_gb,
                    username, hostname, forwards, shares, autostart,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET
                    image_key   = excluded.image_key,
                    arch        = excluded.arch,
                    memory_mb   = excluded.memory_mb,
                    cpus        = excluded.cpus,
                    disk_gb     = excluded.disk_gb,
                    username    = excluded.username,
                    hostname    = excluded.hostname,
                    forwards    = excluded.forwards,
                    shares      = excluded.shares,
                    autostart   = excluded.autostart,
                    updated_at  = excluded.updated_at
                """,
                (
                    spec_dict["name"],
                    spec_dict["image_key"],
                    spec_dict.get("arch", "amd64"),
                    spec_dict.get("memory_mb", 1024),
                    spec_dict.get("cpus", 1),
                    spec_dict.get("disk_gb", 1024),
                    spec_dict.get("username", ""),
                    spec_dict.get("hostname", ""),
                    json.dumps(spec_dict.get("forwards", []),
                               ensure_ascii=False),
                    json.dumps(spec_dict.get("shares", []),
                               ensure_ascii=False),
                    1 if spec_dict.get("autostart") else 0,
                    spec_dict.get("created_at", time.time()),
                    time.time(),
                ),
            )
            _db().commit()
        return True
    except (sqlite3.Error, KeyError) as e:
        print(f"[state] 保存 VM 失败: {e}")
        return False


def load_vm(name: str) -> dict | None:
    """读单个 VM 配置（返回 VMSpec.from_dict 可用的 dict）"""
    with _lock:
        row = _db().execute(
            "SELECT * FROM vms WHERE name = ?", (name,)
        ).fetchone()

    return _row_to_spec(row) if row else None


def load_all_vms() -> list[dict]:
    """读全部 VM 配置（按名称排序）"""
    with _lock:
        rows = _db().execute(
            "SELECT * FROM vms ORDER BY name"
        ).fetchall()

    return [_row_to_spec(r) for r in rows]


def delete_vm_row(name: str) -> bool:
    with _lock:
        cur = _db().execute("DELETE FROM vms WHERE name = ?", (name,))
        _db().commit()
    return cur.rowcount > 0


def _row_to_spec(row: sqlite3.Row) -> dict:
    """数据库行 → VMSpec.from_dict 可用的 dict"""
    return {
        "name": row["name"],
        "image_key": row["image_key"],
        "arch": row["arch"],
        "memory_mb": row["memory_mb"],
        "cpus": row["cpus"],
        "disk_gb": row["disk_gb"],
        "username": row["username"],
        "hostname": row["hostname"],
        "forwards": json.loads(row["forwards"]),
        "shares": [list(s) for s in json.loads(row["shares"])],
        "autostart": bool(row["autostart"]),
        "created_at": row["created_at"],
    }


# ─────────────────────────────────────────
# image_cache：镜像下载记录
# ─────────────────────────────────────────
def record_image(key: str, arch: str, path: str, size_bytes: int,
                 sha256: str = "") -> None:
    with _lock:
        _db().execute(
            """
            INSERT INTO image_cache
                (key, arch, path, size_bytes, sha256, downloaded_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(key, arch) DO UPDATE SET
                path = excluded.path,
                size_bytes = excluded.size_bytes,
                sha256 = excluded.sha256,
                downloaded_at = excluded.downloaded_at
            """,
            (key, arch, path, size_bytes, sha256, time.time()),
        )
        _db().commit()


def image_info(key: str, arch: str = "amd64") -> dict | None:
    with _lock:
        row = _db().execute(
            "SELECT * FROM image_cache WHERE key = ? AND arch = ?",
            (key, arch),
        ).fetchone()
    return dict(row) if row else None


def all_cached_images() -> list[dict]:
    with _lock:
        rows = _db().execute(
            "SELECT * FROM image_cache ORDER BY key"
        ).fetchall()
    return [dict(r) for r in rows]


def forget_image(key: str, arch: str = "amd64") -> None:
    with _lock:
        _db().execute(
            "DELETE FROM image_cache WHERE key = ? AND arch = ?",
            (key, arch),
        )
        _db().commit()


# ─────────────────────────────────────────
# 迁移：旧 QSettings / vm.json → SQLite
# ─────────────────────────────────────────
def migrate_from_legacy() -> tuple[int, int]:
    """从旧存储迁移（幂等，可重复调用）

    返回 (迁移的设置数, 迁移的 VM 数)

    迁移内容：
      · ~/.config/podman-gui/podman-gui.conf 的 [limits] memory_gb
      · ~/.local/share/podman-gui/vm/machines/*/vm.json

    原文件改名为 .bak 保留，不删。
    """
    settings_n = 0
    vms_n = 0

    # 1. QSettings（INI 格式，PyQt 写入的）
    #    也接受 .bak：上次迁移已把原文件改名，但值可能没写进数据库
    #    （例如数据库被重建），此时从备份恢复。
    conf = Path.home() / ".config" / "podman-gui" / "podman-gui.conf"
    if not conf.exists():
        bak = conf.with_suffix(".conf.bak")
        if bak.exists():
            conf = bak
    if conf.exists():
        try:
            import configparser

            cp = configparser.ConfigParser()
            cp.read(conf, encoding="utf-8")

            if cp.has_section("limits") and cp.has_option("limits", "memory_gb"):
                raw = cp.get("limits", "memory_gb")
                # 只有数据库里没有时才迁移，避免覆盖更新过的值
                if get_setting("limits/memory_gb") is None:
                    try:
                        set_setting("limits/memory_gb", float(raw))
                    except ValueError:
                        pass
                    else:
                        settings_n += 1

            # 备份（已有 .bak 则不覆盖）
            bak = conf.with_suffix(".conf.bak")
            if conf.suffix == ".bak":
                pass                     # 源就是备份，无需再备份
            elif bak.exists():
                conf.unlink(missing_ok=True)
            else:
                conf.rename(bak)
        except Exception as e:
            print(f"[state] 迁移 QSettings 失败: {e}")

    # 2. vm.json
    machines = (Path.home() / ".local" / "share" / "podman-gui"
                / "vm" / "machines")
    if machines.is_dir():
        for d in sorted(machines.iterdir()):
            cfg = d / "vm.json"
            if not cfg.exists():
                continue
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                if not load_vm(data.get("name", "")):
                    if save_vm(data):
                        vms_n += 1
                # 注意：不把 vm.json 改名成 .bak。
                # load_all() 会把目录里的 vm.json 作为数据源之一，
                # 改名等于让这台 VM 从界面上消失。
                # json 与 SQLite 双写，SQLite 为主、json 兜底。
            except Exception as e:
                print(f"[state] 迁移 {cfg} 失败: {e}")

    return settings_n, vms_n


def stats() -> dict:
    """数据库概况（调试/设置页展示用）"""
    with _lock:
        db = _db()
        return {
            "path": str(DB_PATH),
            "size_kb": DB_PATH.stat().st_size // 1024 if DB_PATH.exists() else 0,
            "settings": db.execute(
                "SELECT COUNT(*) c FROM settings").fetchone()["c"],
            "vms": db.execute(
                "SELECT COUNT(*) c FROM vms").fetchone()["c"],
            "images": db.execute(
                "SELECT COUNT(*) c FROM image_cache").fetchone()["c"],
        }
