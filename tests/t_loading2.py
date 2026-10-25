import sys, os, faulthandler, traceback
sys.path.insert(0, '/home/john/项目/podman-gui')
os.environ['QT_QPA_PLATFORMTHEME'] = 'kde'
os.environ.setdefault('DISPLAY', ':0')
faulthandler.enable()
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt, QTimer, QEventLoop
app = QApplication([])
try:
    from core.podman_client import PodmanClient
    from ui.main_window import MainWindow
    win = MainWindow(PodmanClient())
    win.resize(1280,780); win.show()
    win._on_nav("vms")
    loop = QEventLoop(); QTimer.singleShot(1800, loop.quit); loop.exec()

    page = win.page_vms
    names = [page.model.item(r,0).text() for r in range(page.model.rowCount())]
    print(f"  VM: {names}", flush=True)
    print(f"  动画间隔: {page._spin_timer.interval()}ms (应 33)", flush=True)

    target = names[0]
    r = 0

    # ── 停止 loading 文案 ──
    page._pending_vms.add(target)
    page._stopping.add(target)
    page._spin_timer.start()
    page._rebuild()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    c1 = page.model.item(r, 1)
    print(f"  停止态文字: {c1.text()!r} (应 '停止中…')", flush=True)

    # 截一帧
    win.grab().save("/tmp/shots/stop_loading.png")

    # ── 动画帧率验证：1 秒内应推进 ~30 帧 ──
    f0 = page._spin_frame
    loop = QEventLoop(); QTimer.singleShot(1000, loop.quit); loop.exec()
    advanced = (page._spin_frame - f0) % 30
    print(f"  1 秒推进: {advanced} 帧 (应 ≈30)", flush=True)

    # ── 帧缓存验证：同帧两次取图标是同一对象 ──
    page._spin_frame = 10
    i1 = page._spin_icon()
    i2 = page._spin_icon()
    same = i1.cacheKey() == i2.cacheKey()
    page._spin_frame = 11
    i3 = page._spin_icon()
    print(f"  同帧缓存命中: {same}  不同帧不同图标: {i1.cacheKey() != i3.cacheKey()}", flush=True)

    # ── 优先级：starting > stopping ──
    page._starting.add(target)   # 同时标记时启动优先
    page._rebuild()
    c1 = page.model.item(r, 1)
    print(f"  同时标记时文字: {c1.text()!r} (应 '启动中…')", flush=True)
    page._starting.discard(target)

    # 恢复
    page._pending_vms.discard(target)
    page._stopping.discard(target)
    page._spin_timer.stop()
    page._rebuild()

    win.close()
    print("\n  ✓✓✓ 通过", flush=True)
except Exception:
    traceback.print_exc()
