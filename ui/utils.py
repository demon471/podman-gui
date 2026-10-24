"""UI 通用工具函数"""

from __future__ import annotations

from datetime import datetime, timezone


def fmt_time(ts) -> str:
    """格式化时间

    支持：
    - Unix 时间戳（int / 数字字符串）
    - ISO 8601 字符串（含 Z 后缀）
    - None / 空 → "-"
    """
    if ts is None or ts == "" or ts == 0:
        return "-"

    # 纯数字（可能是 str 形式的 int）
    if isinstance(ts, (int, float)):
        return _from_unix(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if s.isdigit():
            return _from_unix(int(s))
        # ISO 格式
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return s[:16]

    return str(ts)[:16]


def _from_unix(n) -> str:
    try:
        dt = datetime.fromtimestamp(float(n), tz=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError, TypeError):
        return str(n)


def fmt_size(n) -> str:
    """格式化字节数"""
    if not n:
        return "0 B"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)

    negative = n < 0
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024:
            s = f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
            return f"-{s}" if negative else s
        n /= 1024
    return f"{n:.2f} EB"


def fmt_relative(ts) -> str:
    """相对时间，如「3 分钟前」"""
    if ts is None or ts == "":
        return "-"

    try:
        if isinstance(ts, (int, float)) or (
            isinstance(ts, str) and ts.strip().isdigit()
        ):
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        elif isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        else:
            return str(ts)[:16]
    except (ValueError, OSError, OverflowError, TypeError):
        return str(ts)[:16]

    now = datetime.now(timezone.utc)
    delta = (now - dt).total_seconds()

    if delta < 0:
        return fmt_time(ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    return fmt_time(ts)


def host_ip() -> str:
    """获取本机在局域网中的 IP（用于生成可外部访问的地址）

    优先取默认路由出口 IP；失败则回退 127.0.0.1。
    结果缓存 30 秒，避免频繁调用。
    """
    global _HOST_IP_CACHE
    import time

    now = time.time()
    if _HOST_IP_CACHE and now - _HOST_IP_CACHE[1] < 30:
        return _HOST_IP_CACHE[0]

    ip = ""
    # 方法 1：UDP 连接默认路由（不实际发包）
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.3)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip = ""

    # 方法 2：主机名解析
    if not ip or ip.startswith("127."):
        try:
            import socket

            for addr in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not addr.startswith("127."):
                    ip = addr
                    break
        except OSError:
            pass

    if not ip:
        ip = "127.0.0.1"

    _HOST_IP_CACHE = (ip, now)
    return ip


_HOST_IP_CACHE: tuple[str, float] | None = None
