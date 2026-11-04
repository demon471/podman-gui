"""轻机器守护进程（纯容器版）

容器不需要唤醒代理、不需要 paused 救援、不需要内存回收：
  · stop 即真休眠，start 毫秒级
  · 没有 balloon，没有独立内核可回收

守护进程只剩一件事：看门狗 —— 用户希望运行、但意外退出的机器
（lxc 进程被杀、系统重启后残留状态）重新拉起。
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

# 允许 `python -m core.vm.daemon` 与直接执行两种方式
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

log = logging.getLogger("vmd")

# 巡检间隔（秒）。容器启动是毫秒级，30 秒巡检足够
TICK_INTERVAL = 30


class Daemon:
    def __init__(self) -> None:
        self._stop = False

    # ─────────────────────────────────────────
    # 生命周期
    # ─────────────────────────────────────────
    def run(self) -> int:
        log.info("轻机器守护进程启动 (pid=%d)", os.getpid())

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

        # 首次巡检立即执行（应用刚被杀，机器可能需要拉回）
        self._tick()

        while not self._stop:
            for _ in range(TICK_INTERVAL * 10):
                if self._stop:
                    break
                time.sleep(0.1)
            if self._stop:
                break
            self._tick()

        log.info("守护进程退出")
        return 0

    def _on_signal(self, _sig, _frame) -> None:
        self._stop = True

    # ─────────────────────────────────────────
    # 巡检
    # ─────────────────────────────────────────
    def _tick(self) -> None:
        """看门狗：希望运行但没跑的机器 → 拉起"""
        try:
            from core.vm import manager

            for vm in manager.desired_running_vms():
                if vm.running:
                    continue
                ok, err = manager.start_vm(vm)
                if ok:
                    log.info("看门狗拉起机器 %s", vm.name)
                else:
                    log.warning("拉起机器 %s 失败: %s", vm.name, err)
        except Exception as e:
            log.warning("巡检异常: %s", e)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return Daemon().run()


if __name__ == "__main__":
    sys.exit(main())
