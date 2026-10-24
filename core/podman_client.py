"""Podman REST API 客户端

通过 Unix socket 直连 podman.sock，不依赖 CLI 解析。
API 文档: https://docs.podman.io/en/latest/_static/api.html
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from pathlib import Path
from typing import Any

from .config import SOCKET_CANDIDATES


def _demux_log_stream(data: bytes) -> str:
    """解析 Docker/Podman 的复用流日志格式

    格式（每帧 8 字节头 + 负载）：
        byte 0    : 流类型 (0=stdin, 1=stdout, 2=stderr)
        byte 1-3  : 填充
        byte 4-7  : 负载长度（大端 uint32）
        byte 8-   : 负载

    非复用流（纯文本）会原样返回。
    """
    if not data:
        return ""

    # 快速判断：首字节是否为合法流类型且后续长度合理
    if data[0] not in (0, 1, 2) or len(data) < 8:
        return data.decode("utf-8", errors="replace")

    out: list[bytes] = []
    i = 0
    n = len(data)
    frames = 0

    while i + 8 <= n:
        stream = data[i]
        if stream not in (0, 1, 2):
            # 不是复用流，整体按纯文本处理
            return data.decode("utf-8", errors="replace")

        size = int.from_bytes(data[i + 4 : i + 8], "big")
        # 长度异常 → 判定为非复用流
        if size > n - i - 8:
            if frames == 0:
                return data.decode("utf-8", errors="replace")
            break

        out.append(data[i + 8 : i + 8 + size])
        i += 8 + size
        frames += 1

    # 尾部残余数据
    if i < n:
        out.append(data[i:])

    if frames == 0:
        return data.decode("utf-8", errors="replace")

    return b"".join(out).decode("utf-8", errors="replace")


class PodmanError(Exception):
    """Podman API 调用错误"""

    def __init__(self, message: str, status: int = 0, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail


class UnixHTTPConnection(http.client.HTTPConnection):
    """基于 Unix socket 的 HTTP 连接"""

    def __init__(self, socket_path: str, timeout: float = 30.0):
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._socket_path)
        except OSError as e:
            sock.close()
            raise PodmanError(f"无法连接 Podman socket: {e}") from e
        self.sock = sock


def _socket_usable(path: str) -> bool:
    """判断 socket 路径是否存在且可连接

    注意：Path.exists() 在父目录无读权限时会抛 PermissionError
    （例如 rootful 的 /run/podman/ 对普通用户不可读），必须捕获。
    """
    try:
        st = os.stat(path)
    except (OSError, PermissionError):
        # 不存在 / 无权限 / 路径无效，一律视为不可用
        return False

    # 必须是 socket 类型（防止误匹配普通文件）
    import stat as _stat

    if not _stat.S_ISSOCK(st.st_mode):
        return False

    # 检查当前进程是否有读写权限
    if not os.access(path, os.R_OK | os.W_OK):
        return False

    return True


def find_socket() -> str | None:
    """探测可用的 Podman socket

    按 SOCKET_CANDIDATES 顺序尝试，返回第一个存在且可连接的路径。
    全程捕获异常：权限不足、路径不存在、符号链接失效等都跳过而非崩溃。
    """
    try:
        uid = os.getuid()
    except AttributeError:
        uid = 0

    for tmpl in SOCKET_CANDIDATES:
        try:
            candidate = tmpl.format(uid=uid)
        except (KeyError, IndexError):
            continue

        if _socket_usable(candidate):
            return candidate

    # 环境变量覆盖（Podman 官方支持 CONTAINER_HOST）
    env_host = os.environ.get("CONTAINER_HOST", "")
    if env_host.startswith("unix://"):
        path = env_host[len("unix://"):]
        if _socket_usable(path):
            return path

    return None


class PodmanClient:
    """Podman REST API 客户端"""

    def __init__(self, socket_path: str | None = None, timeout: float = 30.0):
        self.socket_path = socket_path or find_socket()
        self.timeout = timeout
        self._api_version: str | None = None

        # 连接状态：None=未知, True=正常, False=断开
        self._connected: bool | None = None
        # 状态变化回调（由 UI 注册）
        self._on_state_change: list = []

    # ─────────────────────────────────────────
    # 连接状态
    # ─────────────────────────────────────────
    def on_state_change(self, callback) -> None:
        """注册连接状态变化回调（callback(connected: bool)）"""
        if callback not in self._on_state_change:
            self._on_state_change.append(callback)

    def _set_connected(self, connected: bool) -> None:
        """更新连接状态，变化时通知所有回调"""
        if self._connected == connected:
            return
        self._connected = connected
        for cb in list(self._on_state_change):
            try:
                cb(connected)
            except Exception:
                pass

    @property
    def connected(self) -> bool | None:
        return self._connected

    def check_connection(self) -> bool:
        """主动探测并更新状态（返回是否连接）"""
        ok = self.ping()
        self._set_connected(ok)
        return ok

    # ─────────────────────────────────────────
    # 底层请求
    # ─────────────────────────────────────────
    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        if not self.socket_path:
            raise PodmanError("未找到 Podman socket，请确认 Podman 正在运行")

        conn = UnixHTTPConnection(self.socket_path, timeout or self.timeout)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        payload = json.dumps(body).encode() if body is not None else None

        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
        except PodmanError:
            raise
        except (OSError, http.client.HTTPException) as e:
            # 连接层失败 → 标记断开（UI 会立即清空数据）
            self._set_connected(False)
            raise PodmanError(f"请求失败: {e}") from e
        finally:
            conn.close()

        # 收到响应 → 连接正常
        self._set_connected(True)

        if status >= 400:
            detail = data.decode(errors="replace")[:500]
            raise PodmanError(f"API 错误 (HTTP {status})", status=status, detail=detail)

        if raw:
            return data
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data.decode(errors="replace")

    # ─────────────────────────────────────────
    # 连接检测
    # ─────────────────────────────────────────
    # 连接探测专用超时（服务停止后 socket 仍存在，
    # 但连接无响应，用默认 30s 会让 UI 卡很久）
    PING_TIMEOUT = 1.5

    def ping(self, timeout: float | None = None) -> bool:
        """测试连接是否可用

        默认用短超时（1.5s），避免服务停止后长时间等待。
        """
        try:
            return self._request(
                "GET", "/v5.0.0/libpod/_ping",
                raw=True,
                timeout=timeout or self.PING_TIMEOUT,
            ) == b"OK"
        except PodmanError:
            return False
        except Exception:
            return False

    def version(self) -> dict:
        """获取版本信息"""
        return self._request("GET", "/version") or {}

    # ─────────────────────────────────────────
    # 容器
    # ─────────────────────────────────────────
    def list_containers(self, all: bool = True) -> list[dict]:
        q = "?all=true" if all else ""
        return self._request("GET", f"/v5.0.0/libpod/containers/json{q}") or []

    def inspect_container(self, cid: str) -> dict:
        return self._request("GET", f"/v5.0.0/libpod/containers/{cid}/json") or {}

    def start_container(self, cid: str) -> None:
        self._request("POST", f"/v5.0.0/libpod/containers/{cid}/start")

    def stop_container(self, cid: str, timeout: int = 10) -> None:
        self._request("POST", f"/v5.0.0/libpod/containers/{cid}/stop?t={timeout}")

    def restart_container(self, cid: str, timeout: int = 10) -> None:
        self._request("POST", f"/v5.0.0/libpod/containers/{cid}/restart?t={timeout}")

    def pause_container(self, cid: str) -> None:
        self._request("POST", f"/v5.0.0/libpod/containers/{cid}/pause")

    def unpause_container(self, cid: str) -> None:
        self._request("POST", f"/v5.0.0/libpod/containers/{cid}/unpause")

    def kill_container(self, cid: str, signal: str = "SIGKILL") -> None:
        self._request("POST", f"/v5.0.0/libpod/containers/{cid}/kill?signal={signal}")

    def remove_container(self, cid: str, force: bool = False, volumes: bool = False) -> None:
        q = f"?force={'true' if force else 'false'}&v={'true' if volumes else 'false'}"
        self._request("DELETE", f"/v5.0.0/libpod/containers/{cid}{q}")

    def container_logs(self, cid: str, tail: int = 500, timestamps: bool = False) -> str:
        q = f"?stdout=true&stderr=true&tail={tail}&timestamps={'true' if timestamps else 'false'}"
        data = self._request("GET", f"/v5.0.0/libpod/containers/{cid}/logs{q}", raw=True)
        if not data:
            return ""
        return _demux_log_stream(data)

    def container_stats(self, cid: str) -> dict:
        """单次采样容器资源使用"""
        data = self._request("GET", f"/v5.0.0/libpod/containers/{cid}/stats?stream=false")
        return data or {}

    def create_container(self, spec: dict) -> dict:
        return self._request("POST", "/v5.0.0/libpod/containers/create", body=spec) or {}

    # ─────────────────────────────────────────
    # 镜像
    # ─────────────────────────────────────────
    def list_images(self) -> list[dict]:
        return self._request("GET", "/v5.0.0/libpod/images/json") or []

    def inspect_image(self, name: str) -> dict:
        return self._request("GET", f"/v5.0.0/libpod/images/{name}/json") or {}

    def remove_image(self, name: str, force: bool = False) -> None:
        q = f"?force={'true' if force else 'false'}"
        self._request("DELETE", f"/v5.0.0/libpod/images/{name}{q}")

    def pull_image(self, reference: str) -> Any:
        """拉取镜像（流式响应，返回原始数据）"""
        return self._request(
            "POST",
            f"/v5.0.0/libpod/images/pull?reference={reference}",
            raw=True,
            timeout=600,
        )

    def prune_images(self, all: bool = False) -> dict:
        filters = '{"dangling":["false"]}' if all else '{"dangling":["true"]}'
        return self._request(
            "POST",
            f"/v5.0.0/libpod/images/prune?filters={filters}",
        ) or {}

    def image_history(self, name: str) -> list[dict]:
        return self._request("GET", f"/v5.0.0/libpod/images/{name}/history") or []

    # ─────────────────────────────────────────
    # 卷
    # ─────────────────────────────────────────
    def list_volumes(self) -> list[dict]:
        data = self._request("GET", "/v5.0.0/libpod/volumes/json") or []
        return data if isinstance(data, list) else []

    def create_volume(self, name: str, driver: str = "local") -> dict:
        return self._request(
            "POST", "/v5.0.0/libpod/volumes/create",
            body={"Name": name, "Driver": driver},
        ) or {}

    def remove_volume(self, name: str, force: bool = False) -> None:
        q = f"?force={'true' if force else 'false'}"
        self._request("DELETE", f"/v5.0.0/libpod/volumes/{name}{q}")

    # ─────────────────────────────────────────
    # 网络
    # ─────────────────────────────────────────
    def list_networks(self) -> list[dict]:
        return self._request("GET", "/v4.0.0/libpod/networks/json") or []

    def create_network(self, name: str, driver: str = "bridge") -> dict:
        return self._request(
            "POST", "/v4.0.0/libpod/networks/create",
            body={"name": name, "driver": driver},
        ) or {}

    def remove_network(self, name: str, force: bool = False) -> None:
        q = f"?force={'true' if force else 'false'}"
        self._request("DELETE", f"/v4.0.0/libpod/networks/{name}{q}")

    # ─────────────────────────────────────────
    # Pod
    # ─────────────────────────────────────────
    def list_pods(self) -> list[dict]:
        return self._request("GET", "/v5.0.0/libpod/pods/json") or []

    def create_pod(self, name: str) -> dict:
        return self._request(
            "POST", "/v5.0.0/libpod/pods/create",
            body={"name": name},
        ) or {}

    def start_pod(self, pid: str) -> None:
        self._request("POST", f"/v5.0.0/libpod/pods/{pid}/start")

    def stop_pod(self, pid: str) -> None:
        self._request("POST", f"/v5.0.0/libpod/pods/{pid}/stop")

    def restart_pod(self, pid: str) -> None:
        self._request("POST", f"/v5.0.0/libpod/pods/{pid}/restart")

    def remove_pod(self, pid: str, force: bool = False) -> None:
        q = f"?force={'true' if force else 'false'}"
        self._request("DELETE", f"/v5.0.0/libpod/pods/{pid}{q}")

    # ─────────────────────────────────────────
    # 系统
    # ─────────────────────────────────────────
    def system_info(self) -> dict:
        return self._request("GET", "/v5.0.0/libpod/info") or {}

    def system_df(self) -> dict:
        return self._request("GET", "/v5.0.0/libpod/system/df") or {}


# ─────────────────────────────────────────
# 单例
# ─────────────────────────────────────────
_client: PodmanClient | None = None


def get_client() -> PodmanClient:
    global _client
    if _client is None:
        _client = PodmanClient()
    return _client
