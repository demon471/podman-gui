"""唤醒代理

解决的问题
──────────
VM 休眠后，用户在终端里直接 `ssh rocky.vm.local` 会连接被拒 ——
因为 QEMU 已经退出，那个 hostfwd 端口没人监听。

GUI 拦不住用户在外部终端敲命令，所以必须让**端口本身**在休眠期间
依然"活着"：接受连接 → 唤醒 VM → 把连接转交给 QEMU。

    ssh rocky.vm.local
        │
        ▼
    [唤醒代理 :2224]   ← 休眠期间由它占着端口
        │  1. 接受连接
        │  2. 启动 QEMU（等 SSH 就绪）
        │  3. 连接到 QEMU 的 127.0.0.1:2224
        ▼
    [QEMU :2224] → guest sshd
        │  4. 双向转发字节流
        ▼
      SSH 会话

端口交接
────────
代理必须先把监听端口让出来，QEMU 才能 bind 同一个端口。
流程是：
  1. 代理 accept 一个连接
  2. 立刻关闭监听 socket（释放端口）
  3. 启动 QEMU（它 bind 同一端口）
  4. 等 SSH banner 出现
  5. 新建到 QEMU 的连接，把已 accept 的客户端连接和它对接

第 3 步到第 4 步之间端口是空的（几百毫秒到十几秒），
期间如果有**第二个**客户端连进来会失败。这是可接受的：
SSH 客户端自己会重试，且并发连接同一个刚休眠的 VM 本身少见。

内存占用
────────
代理是 Python 线程 + 两个 socket，休眠的 VM 约 3–5MB，
可以忽略。它只在 VM 休眠期间存在，VM 跑起来后就退出。
"""

from __future__ import annotations

import logging
import socket
import threading
import time

log = logging.getLogger(__name__)

# 唤醒后等待 SSH 就绪的上限（秒）
WAKE_TIMEOUT = 180
# 转发缓冲区
BUF = 65536
# 连接 QEMU 的重试间隔
CONNECT_RETRY = 0.4


class WakeProxy:
    """单台 VM 的唤醒代理

    用法：
        proxy = WakeProxy(vm, on_wake=callback)
        proxy.start()      # 开始监听（替代休眠的 QEMU 占住端口）
        proxy.stop()       # VM 已运行 / VM 被删除时停掉
    """

    def __init__(self, vm, on_wake=None):
        self.vm = vm
        self.name = vm.spec.name
        self.port = vm.ssh_port
        self.on_wake = on_wake          # 回调：由保活管理器提供唤醒动作

        # 该 VM 所有转发端口 —— 任何端口被访问都算「有人要用这台机器」。
        # 只盯 SSH 端口会漏掉「浏览器打开 VM 里的 Web 服务」这类访问。
        self.ports: list[int] = [
            f.host_port for f in vm.spec.forwards if f.host_port
        ]

        self._socks: dict[int, socket.socket] = {}
        self._threads: list[threading.Thread] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._waking = threading.Event()

    # ─────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────
    def start(self) -> tuple[bool, str]:
        """开始监听该 VM 的全部转发端口

        任何端口有连接进来都会触发唤醒 —— 用户可能用浏览器、
        数据库客户端或任意协议访问 VM 里的服务，不只是 SSH。
        """
        if not self.ports:
            return False, "该虚拟机没有转发端口"

        if self._socks:
            return True, ""

        self._stop.clear()
        bound: list[int] = []
        errors: list[str] = []

        for port in self.ports:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.listen(8)
                s.settimeout(1.0)
            except OSError as e:
                errors.append(f":{port} {e}")
                continue

            self._socks[port] = s
            t = threading.Thread(
                target=self._serve, args=(port,),
                name=f"wake-proxy-{self.name}-{port}", daemon=True,
            )
            t.start()
            self._threads.append(t)
            bound.append(port)

        if not bound:
            return False, "端口无法监听: " + "; ".join(errors)

        log.debug("唤醒代理已启动: %s 端口 %s", self.name, bound)
        return True, ""

    def stop(self) -> None:
        """停止代理并释放所有端口"""
        self._stop.set()

        for s in list(self._socks.values()):
            try:
                s.close()
            except OSError:
                pass
        self._socks.clear()

        cur = threading.current_thread()
        for t in self._threads:
            if t.is_alive() and t is not cur:
                t.join(timeout=3)
        self._threads.clear()
        log.debug("唤醒代理已停止: %s", self.name)

    @property
    def active(self) -> bool:
        return bool(self._socks)

    # ─────────────────────────────────────────
    # 主循环
    # ─────────────────────────────────────────
    def _serve(self, port: int) -> None:
        """单个端口的监听循环"""
        while not self._stop.is_set():
            # 确保在监听（上一轮唤醒失败后要重新占端口）
            if port not in self._socks:
                if not self._bind(port):
                    return

            sock = self._socks.get(port)
            if sock is None:
                return

            try:
                client, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            # 关闭这个监听 socket，把端口让给 QEMU
            try:
                sock.close()
            except OSError:
                pass
            self._socks.pop(port, None)

            handed_off = self._handle(client)

            if handed_off:
                # 已交给 QEMU，代理使命完成
                log.debug("唤醒代理：%s 已接管端口 :%d", self.name, port)
                return

            # 唤醒失败或客户端提前断开 —— 重新占住端口
            log.debug("唤醒代理：%s :%d 本轮未交接成功，重新监听", self.name, port)

    def _bind(self, port: int) -> bool:
        """绑定某个监听端口（内部用）"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", port))
            s.listen(8)
            s.settimeout(1.0)
        except OSError as e:
            log.debug("唤醒代理 %s 绑定 :%d 失败: %s", self.name, port, e)
            return False
        self._socks[port] = s
        return True

    def _handle(self, client: socket.socket) -> bool:
        """唤醒 VM，然后把 client 的流量转给 QEMU

        返回 True 表示已成功交接（端口现在归 QEMU）；
        False 表示失败，调用方应重新监听端口。
        """
        log.info("唤醒代理：收到连接，开始唤醒 %s", self.name)

        # 端口探测（`nc -z`、健康检查、浏览器预连接）会连上又立刻断开。
        # 这种连接不该消耗一次完整的唤醒。
        #
        # 但要注意：HTTP 客户端连上后可能先等一会儿才发请求，
        # 所以给一点宽限期，只把「已经明确断开」的判为探测。
        if self._client_gone(client, grace=0.4):
            log.debug("唤醒代理：%s 收到的是探测连接，忽略", self.name)
            _close(client)
            return False

        # ── 1. 释放全部监听端口 ──
        # QEMU 启动时要 bind 同一批端口，代理必须先让出来，
        # 否则会报 "Could not set up host forwarding rule"。
        for p in list(self._socks):
            s = self._socks.pop(p, None)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

        # 给内核一点时间真正释放（TIME_WAIT 不阻塞 bind，
        # 但刚 close 的监听 socket 需要短暂让位）
        time.sleep(0.15)

        # ── 2. 唤醒 ──
        ok = False
        if self.on_wake is not None:
            try:
                ok = bool(self.on_wake(self.vm))
            except Exception as e:
                log.warning("唤醒 %s 失败: %s", self.name, e)

        if not ok:
            log.warning("唤醒代理：%s 唤醒失败，断开连接", self.name)
            _close(client)
            # 端口已让出，重新占回来等下一次访问
            for p in self.ports:
                if p not in self._socks:
                    self._bind(p)
            return False

        # ── 2. 连到刚起来的 QEMU ──
        # 注意：QEMU 起来后会 bind 同一个端口，所以我们不能再监听。
        upstream = self._connect_upstream()
        if upstream is None:
            log.warning("唤醒代理：%s 唤醒后仍连不上 QEMU", self.name)
            _close(client)
            return False

        # ── 3. 双向转发 ──
        try:
            _pipe(client, upstream)
        finally:
            _close(client)
            _close(upstream)

        # 转发结束：VM 已经在跑，端口归 QEMU
        return True

    def _client_gone(self, client: socket.socket, grace: float = 0.0) -> bool:
        """判断客户端是否已经断开（端口探测之类）

        用 MSG_PEEK 读一个字节：返回 b"" 说明对端已关闭。
        对端还没发数据时可能抛 timeout —— 视为仍连接。

        grace 秒内轮询，给客户端一点时间发首个字节
        （HTTP 客户端可能先等再发请求）。
        """
        deadline = time.time() + max(0.0, grace)
        while True:
            try:
                client.setblocking(False)
                try:
                    data = client.recv(1, socket.MSG_PEEK)
                finally:
                    client.setblocking(True)
                return data == b""
            except BlockingIOError:
                # 没有数据，连接仍在
                if time.time() >= deadline:
                    return False
                time.sleep(0.05)
            except OSError:
                return True

    def _connect_upstream(self) -> socket.socket | None:
        """连接到 QEMU 的 hostfwd 端口，直到成功或超时"""
        deadline = time.time() + WAKE_TIMEOUT
        while time.time() < deadline and not self._stop.is_set():
            try:
                up = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                up.settimeout(None)
                return up
            except OSError:
                time.sleep(CONNECT_RETRY)
        return None


# ─────────────────────────────────────────
# 工具
# ─────────────────────────────────────────
def _close(s: socket.socket | None) -> None:
    if s is None:
        return
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _pipe(a: socket.socket, b: socket.socket) -> None:
    """在两个 socket 之间双向转发（阻塞直到任一方向结束）"""
    done = threading.Event()

    def pump(src, dst):
        try:
            while not done.is_set():
                data = src.recv(BUF)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            done.set()

    t1 = threading.Thread(target=pump, args=(a, b), daemon=True)
    t2 = threading.Thread(target=pump, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


# ─────────────────────────────────────────
# 代理池：跟着保活管理器一起用
# ─────────────────────────────────────────
class ProxyPool:
    """管理所有 VM 的唤醒代理

    规则：VM 休眠（进程没跑但意图是运行）→ 起代理占端口
          VM 运行中                              → 停代理（端口归 QEMU）
    """

    def __init__(self, on_wake=None):
        self._proxies: dict[str, WakeProxy] = {}
        self.on_wake = on_wake

    def ensure(self, vm, retries: int = 12, delay: float = 0.5) -> tuple[bool, str]:
        """确保该 VM 的代理在跑

        QEMU 刚退出时端口可能还在 TIME_WAIT，bind 会失败。
        这里重试几秒，避免「自动休眠后端口空着」的窗口期
        （那段时间外部 ssh 会连接被拒）。
        """
        name = vm.spec.name
        p = self._proxies.get(name)
        if p is not None and p.active:
            return True, ""

        # 已有实例但不活跃（上一轮交接后退出）→ 丢弃重建
        if p is not None:
            p.stop()
            self._proxies.pop(name, None)

        err = ""
        for i in range(max(1, retries)):
            p = WakeProxy(vm, on_wake=self.on_wake)
            ok, err = p.start()
            if ok:
                self._proxies[name] = p
                return True, ""
            if i < retries - 1:
                time.sleep(delay)
        return False, err

    def drop(self, name: str) -> None:
        """停掉并移除某台 VM 的代理"""
        p = self._proxies.pop(name, None)
        if p is not None:
            p.stop()

    def stop_all(self) -> None:
        for p in list(self._proxies.values()):
            p.stop()
        self._proxies.clear()

    def active_names(self) -> list[str]:
        return [n for n, p in self._proxies.items() if p.active]
