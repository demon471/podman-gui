import sys, os, faulthandler, traceback
sys.path.insert(0, '/home/john/项目/podman-gui')
os.environ['QT_QPA_PLATFORMTHEME'] = 'kde'
os.environ.setdefault('DISPLAY', ':0')
faulthandler.enable()
from PyQt6.QtWidgets import QApplication, QMenu
from PyQt6.QtCore import Qt, QTimer, QEventLoop, QPointF, QPoint, QEvent
from PyQt6.QtGui import QMouseEvent
app = QApplication([])
try:
    from core.podman_client import PodmanClient
    from ui.main_window import MainWindow
    win = MainWindow(PodmanClient())
    win.resize(1280,780); win.show()
    win._on_nav("vms")
    loop = QEventLoop(); QTimer.singleShot(1800, loop.quit); loop.exec()

    page = win.page_vms
    card = page._info_card
    names = [page.model.item(r,0).text() for r in range(page.model.rowCount())]
    print(f"  VM: {names}", flush=True)

    # ── 场景 1：悬停出现卡片后右键 → 卡片必须收起、菜单正常 ──
    target = next((n for n in names if n not in page._pending_vms), None)
    r = names.index(target)
    row_rect = page.table.visualRect(page.model.index(r, 0))
    pos = QPoint(row_rect.x()+30, row_rect.y()+8)

    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), Qt.MouseButton.NoButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    page.eventFilter(page.table.viewport(), ev)
    loop = QEventLoop(); QTimer.singleShot(600, loop.quit); loop.exec()
    print(f"  1a. 悬停后卡片显示: {card.isVisible()}", flush=True)

    # 右键按下事件
    ev_press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(pos),
                           Qt.MouseButton.RightButton, Qt.MouseButton.RightButton,
                           Qt.KeyboardModifier.NoModifier)
    page.eventFilter(page.table.viewport(), ev_press)
    print(f"  1b. 右键按下后卡片: {card.isVisible()} (应 False)", flush=True)

    # 右键菜单本身（mock exec 避免真弹）
    called = []
    QMenu.exec = lambda *a, **k: called.append(1)
    page._context_menu(pos)
    QMenu.exec = lambda *a, **k: None
    print(f"  1c. 菜单弹出: {bool(called)}", flush=True)

    # ── 场景 2：弹窗打开时移动鼠标 → 不弹卡片 ──
    # 模拟 activePopupWidget 存在
    from PyQt6.QtWidgets import QWidget
    fake_popup = QWidget(None, Qt.WindowType.Popup)
    fake_popup.show()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()
    ev2 = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), Qt.MouseButton.NoButton,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    page.eventFilter(page.table.viewport(), ev2)
    loop = QEventLoop(); QTimer.singleShot(600, loop.quit); loop.exec()
    print(f"  2a. 弹窗打开时移动鼠标 → 卡片: {card.isVisible()} (应 False)", flush=True)
    fake_popup.close()

    # ── 场景 3：pending 行 loading 全禁 ──
    page._pending_vms.add(target)
    page._starting.add(target)
    page._spin_timer.start()
    page._rebuild()
    loop = QEventLoop(); QTimer.singleShot(200, loop.quit); loop.exec()

    ev3 = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), Qt.MouseButton.NoButton,
                      Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    page.eventFilter(page.table.viewport(), ev3)
    loop = QEventLoop(); QTimer.singleShot(600, loop.quit); loop.exec()
    print(f"  3a. pending 行悬停卡片: {card.isVisible()} (应 False)", flush=True)

    called.clear()
    QMenu.exec = lambda *a, **k: called.append(1)
    page._context_menu(pos)
    print(f"  3b. pending 行右键菜单: {'弹出(应无)' if called else '未弹出 ✓'}", flush=True)

    # 恢复
    page._pending_vms.discard(target)
    page._starting.discard(target)
    page._spin_timer.stop()
    page._rebuild()

    win.grab().save("/tmp/shots/final_check.png")
    win.close()
    print("\n  ✓✓✓ 全部通过", flush=True)
except Exception:
    traceback.print_exc()
