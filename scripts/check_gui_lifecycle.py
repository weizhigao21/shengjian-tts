"""验证 GUI 自动启动服务与关闭后的清理；不操作桌面。"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
from pathlib import Path
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.gui import Window, configure_font
from voice_workbench.store import Store

data = ROOT / "artifacts" / "gui-lifecycle-data"
store = Store(data)
existing = next((j for j in store.list() if j["title"] == "启动列表验证"), None)
saved = existing["id"] if existing else store.create("启动列表验证", {}, "", [], "")
store.close()

app = QApplication([])
configure_font(app)
window = Window(8766, data)
assert any(window.jobs_table.item(i, 0).data(256) == saved for i in range(window.jobs_table.rowCount())), "启动时未显示已保存作品"
window.show()
deadline = time.monotonic() + 25
success = [False]
timer = QTimer()


def check():
    if window.ready:
        assert window.owns_service
        assert window.process.poll() is None
        assert any(window.jobs_table.item(i, 0).data(256) == saved for i in range(window.jobs_table.rowCount()))
        success[0] = True
        timer.stop()
        window.close()
        app.quit()
    elif time.monotonic() > deadline:
        print("banner:", window.banner.text(), "owns:", window.owns_service,
              "process:", window.process.poll() if window.process else None,
              "workers:", len(window.workers), flush=True)
        print(window.service_log.toPlainText(), flush=True)
        timer.stop()
        window.close()
        app.quit()


timer.timeout.connect(check)
timer.start(200)
app.exec()
assert success[0], "GUI 服务未能自动启动"
assert window.process.poll() is not None
print("Saved works appeared at startup; GUI started its service and shut it down cleanly.")
