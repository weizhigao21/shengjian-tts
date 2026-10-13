import time
import httpx

from voice_workbench.gui import Window
from voice_workbench.manager import Manager
from voice_workbench.store import Store


def test_health_counts_all_active_jobs_without_loading_history(tmp_path, monkeypatch):
    store = Store(tmp_path)
    try:
        with store.db:
            store.db.executemany("INSERT INTO jobs (id,title,voice,status,created,deleted,record_only) VALUES (?,?,?,?,?,?,?)",
                [(str(i), "任务", "{}", "queued" if i % 2 else "running", time.time(), 0, i % 2) for i in range(10005)])
            store.db.execute("INSERT INTO jobs (id,status,deleted) VALUES ('deleted','queued',1)")
            store.db.execute("INSERT INTO jobs (id,status,deleted) VALUES ('finished','done',0)")
        manager = Manager.__new__(Manager)
        manager.store, manager.lock = store, store.lock
        manager.active, manager.current, manager.performance_notice = {}, None, ""
        class Config:
            def performance(self): return {}
        manager.config = Config()
        monkeypatch.setattr(store, "list", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("loaded full histories")))
        assert manager.status()["queue"] == 10005
    finally:
        store.close()


def test_settings_only_requests_the_visible_section(tmp_path, qtbot, monkeypatch):
    window = Window(8793, tmp_path, autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    window.settings.timer.stop()
    window.ready = True
    window.set_page(3)
    window.show()
    panel, paths = window.settings, []
    monkeypatch.setattr(panel, "render_models", lambda: None)
    monkeypatch.setattr(panel, "render_audio", lambda: None)
    class Client:
        def get(self, path):
            paths.append(path)
            value = [] if path == "/generated-audio" else {"workflow_workers": 1, "cpu_threads": 0}
            return httpx.Response(200, json=value)
    def call(action, success=None, failure=None):
        value = action(Client())
        if success:
            success(value)
    monkeypatch.setattr(window, "call", call)
    for section, expected in [(0, "/models"), (1, "/generated-audio"), (2, "/settings/performance"), (3, None), (4, "/models")]:
        paths.clear()
        panel.set_section(section)
        assert paths == ([expected] if expected else [])
        assert not panel.loading
    window.close()
