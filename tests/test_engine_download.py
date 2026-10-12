"""Engine installation owns its files and only activates a complete, verified release."""
import hashlib
import io
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.engine_management import EngineManager
from voice_workbench.manager import Manager
from test_inline_generation import Runner, index_voice
from pe_fixture import engine_files, pe_binary
from voice_workbench.engine_validation import CUDA_DLLS


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    return stream.getvalue()


def installer(manager, handle=None, files=None, cuda=False):
    payloads = {"engine.zip": archive(files or engine_files(cuda))}
    if cuda:
        payloads["cudart.zip"] = archive({"cuda/bin/" + name: pe_binary() for name in CUDA_DLLS})
    entries = [{"name": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
               for name, content in payloads.items()]
    backend = "cuda" if cuda else "cpu"
    catalog = {"version": "v0.7.4", "backends": [{"id": backend, "name": backend, "description": "test", "archives": entries}]}
    factory = lambda: httpx.Client(transport=httpx.MockTransport(handle or (lambda r: httpx.Response(200, content=payloads[r.url.path.split('/')[-1]]))))
    manager.models.engines.close()
    manager.models.engines = engines = EngineManager(manager.models, catalog, factory)
    return engines, payloads


@pytest.mark.parametrize("cuda", [False, True])
def test_first_engine_install_updates_presets_and_templates_keeps_originals(tmp_path, cuda):
    voice = index_voice(tmp_path)
    manager = Manager(tmp_path / "data", Runner())
    try:
        manager.config.save_voice(voice)
        manager.config.delete_voice(voice["id"])
        manager.config.save_voice({**voice, "id": "other"})
        original = Path(voice["engine_executable"]).read_bytes()
        engines, _ = installer(manager, cuda=cuda)
        backend = "cuda" if cuda else "cpu"
        engines.start(backend)
        engines.threads[backend].join(4)
        assert engines.states[backend]["status"] == "done"
        configured = manager.config.read()
        path = Path(configured["native_engine"])
        assert path.is_relative_to(engines.root) and path.read_bytes() == pe_binary(["ggml.dll"])
        assert configured["native_engine_backend"] == backend
        assert all(v["engine_executable"] == str(path) and v["device"] == backend
                   for v in manager.config.voices() if v["backend"] == "audio_cpp")
        assert Path(voice["engine_executable"]).read_bytes() == original
        assert (path.parent.parent / "LICENSE").is_file()
        if cuda:
            assert (path.parent / "cublas64_12.dll").read_bytes() == pe_binary()
        assert not list(engines.download_dir.glob("*.zip"))
        engines.start(backend)
        engines.threads[backend].join(3)
        assert manager.config.read()["native_engine"] == str(path)
        assert len(list(engines.root.glob("v0.7.4-*"))) == 1
    finally:
        manager.close()


@pytest.mark.parametrize("resume", [True, False])
def test_engine_partial_transfer_resumes_or_restarts_safely(tmp_path, resume):
    manager = Manager(tmp_path / "data", Runner())
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers["range"] == "bytes=50-"
        content = payloads["engine.zip"]
        return httpx.Response(206, content=content[50:], headers={"Content-Range": f"bytes 50-{len(content)-1}/{len(content)}"}) if resume else httpx.Response(200, content=content)
    try:
        engines, payloads = installer(manager, handle)
        (engines.download_dir / "engine.zip.part").write_bytes(payloads["engine.zip"][:50])
        assert engines.listing()["options"][0]["status"] == "paused"
        engines.start("cpu")
        engines.threads["cpu"].join(3)
        assert engines.states["cpu"]["status"] == "done"
        assert engines.states["cpu"]["resumed_from"] == (50 if resume else 0)
        assert len(requests) == 1
    finally:
        manager.close()


def test_archive_progress_tracks_both_packages_and_survives_cache_cleanup(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    try:
        engines, payloads = installer(manager, cuda=True)
        program, runtime = engines.entries["cuda"]["archives"]
        (engines.download_dir / program["name"]).write_bytes(payloads[program["name"]])
        part = engines.download_dir / (runtime["name"] + ".part")
        part.write_bytes(payloads[runtime["name"]][:50])
        engines.states["cuda"] = {"status": "downloading", "archive": runtime["name"],
                                  "received": program["size"] + 50, "error": ""}
        files = engines.listing()["options"][0]["archives"]
        assert [(a["status"], a["received"]) for a in files] == [("downloaded", program["size"]), ("downloading", 50)]
        part.write_bytes(payloads[runtime["name"]])
        engines.states["cuda"]["status"] = "verifying"
        assert engines.listing()["options"][0]["archives"][1]["status"] == "verifying"
        engines.states["cuda"]["status"] = "paused"
        assert engines.listing()["options"][0]["archives"][1]["status"] == "pending_verification"
        engines.start("cuda")
        engines.threads["cuda"].join(3)
        assert engines.states["cuda"]["status"] == "done"
        assert not list(engines.download_dir.iterdir())
        files = engines.listing()["options"][0]["archives"]
        assert all(a["status"] == "done" and a["received"] == a["size"] for a in files)
        engines.states.clear()  # Restarting also retains installed component state.
        assert all(a["status"] == "done" for a in engines.listing()["options"][0]["archives"])
    finally:
        manager.close()


def test_combined_install_button_pauses_and_resumes_pending_activation(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    with TestClient(create_app(tmp_path / "data", runner=Runner())) as client:
        manager = client.app.state.manager
        engines, _ = installer(manager)
        window = Window(8789, manager.root, autostart=False)
        qtbot.addWidget(window)
        window.timer.stop()
        window.settings.timer.stop()
        window.ready = True
        monkeypatch.setattr(window, "poll", lambda: None)
        monkeypatch.setattr(window.settings, "refresh", lambda: None)
        def immediate(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)
        monkeypatch.setattr(window, "call", immediate)
        routes, original_post = [], client.post
        def recorded_post(path, **kwargs):
            routes.append(path)
            return original_post(path, **kwargs)
        monkeypatch.setattr(client, "post", recorded_post)
        def sync():
            window.settings.model_data = {"engine": engines.listing()}
            window.settings.engine_backend.setCurrentIndex(window.settings.engine_backend.findData("cpu"))
            window.settings.render_engine()
        manager.current = ("generation-in-progress", 1)
        try:
            client.post("/models/runtime/download", json={"backend": "cpu"})
            qtbot.waitUntil(lambda: engines.states["cpu"]["status"] == "waiting", timeout=3000)
            sync()
            control = window.settings.engine_download
            assert control.isEnabled() and control.text() == "暂停安装"
            control.click()
            engines.threads["cpu"].join(3)
            assert routes[-1] == "/models/runtime/pause" and engines.states["cpu"]["status"] == "paused"
            assert not manager.config.read().get("native_engine")
            sync()
            assert control.isEnabled() and control.text() == "继续安装"
            control.click()
            assert routes[-1] == "/models/runtime/download"  # Activation still waits for the current generation.
            qtbot.waitUntil(lambda: engines.states["cpu"]["status"] == "waiting", timeout=3000)
            manager.current = None
            engines.threads["cpu"].join(3)
            sync()
            assert not control.isEnabled() and control.text() == "已拥有"
            assert engines.listing()["ready"]
        finally:
            manager.current = None
            window.close()


@pytest.mark.parametrize("problem", ["hash", "missing", "escape", "invalid_executable", "network"])
def test_failed_engine_install_keeps_active_engine_and_cleans_staging(tmp_path, problem):
    manager = Manager(tmp_path / "data", Runner())
    original = tmp_path / "old.exe"
    original.write_bytes(b"old engine")
    manager.models.set_runtime(str(original))
    files = {"LICENSE": b"no engine"} if problem == "missing" else {"../escape.exe": b"MZ invalid path"} if problem == "escape" else {"audiocpp_server.exe": b"not executable"} if problem == "invalid_executable" else None
    def fail_network(request):
        raise httpx.ConnectError("network unavailable", request=request)
    try:
        engines, _ = installer(manager, fail_network if problem == "network" else None, files)
        if problem == "hash":
            engines.entries["cpu"]["archives"][0]["sha256"] = "0" * 64
        previous = manager.config.read()
        engines.start("cpu")
        engines.threads["cpu"].join(3)
        state = engines.states["cpu"]
        assert state["status"] == "failed" and state["error"]
        assert manager.config.read() == previous
        assert manager.models.runtime() == str(original) and original.read_bytes() == b"old engine"
        assert not list(engines.root.glob(".install-*")) and not (engines.root / "escape.exe").exists()
        assert not list(engines.root.glob("v0.7.4-*"))
    finally:
        manager.close()


def test_engine_pause_and_restart_keep_partial_and_stop_thread(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    yielded, release = threading.Event(), threading.Event()
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield payloads["engine.zip"][:262144]
            yielded.set()
            release.wait(3)
            yield payloads["engine.zip"][262144:]
    try:
        engines, payloads = installer(manager, lambda _: httpx.Response(200, stream=SlowStream()),
                                      {"audiocpp_server.exe": b"MZ" + b"x" * 600000})
        engines.start("cpu")
        assert yielded.wait(3)
        engines.pause("cpu")
        release.set()
        engines.threads["cpu"].join(3)
        assert engines.states["cpu"]["status"] == "paused"
        assert (engines.download_dir / "engine.zip.part").stat().st_size == 262144
        assert not manager.config.read().get("native_engine")
    finally:
        release.set()
        manager.close()
    assert not engines.threads["cpu"].is_alive()


def test_install_waits_for_generation_then_activates(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    try:
        engines, _ = installer(manager)
        manager.current = ("busy", 1)
        engines.start("cpu")
        deadline = time.monotonic() + 3
        while engines.states["cpu"]["status"] != "waiting" and time.monotonic() < deadline:
            time.sleep(.01)
        assert engines.states["cpu"]["status"] == "waiting"
        assert manager.config.read()["native_engines"]["cpu"]["path"]
        assert not manager.config.read().get("native_engine")
        with manager.lock:
            manager.current = None
        engines.threads["cpu"].join(3)
        assert engines.states["cpu"]["status"] == "done" and engines.listing()["ready"]
    finally:
        manager.current = None
        manager.close()


def test_corrupt_complete_engine_cache_can_be_retried(tmp_path):
    manager = Manager(tmp_path / "data", Runner())
    try:
        engines, payloads = installer(manager)
        cached = engines.download_dir / "engine.zip"
        cached.write_bytes(b"x" * len(payloads["engine.zip"]))
        engines.start("cpu")
        engines.threads["cpu"].join(3)
        assert engines.states["cpu"]["status"] == "failed" and not cached.exists()
        engines.start("cpu")
        engines.threads["cpu"].join(3)
        assert engines.states["cpu"]["status"] == "done"
    finally:
        manager.close()


def test_engine_api_and_inline_download_update_current_model(tmp_path, qapp, monkeypatch):
    from voice_workbench.gui import Window
    voice = index_voice(tmp_path)
    Path(voice["engine_executable"]).unlink()
    with TestClient(create_app(tmp_path / "data", runner=Runner())) as client:
        manager = client.app.state.manager
        manager.config.save_voice(voice)
        engines, _ = installer(manager)
        assert not client.get("/models").json()["engine"]["ready"]
        assert client.post("/models/runtime/download", json={"backend": "bad"}).status_code == 422
        window = Window(8789, manager.root, autostart=False)
        window.timer.stop()
        def call(action, success=None, failure=None):
            value = action(client)
            if success:
                success(value)
        monkeypatch.setattr(window, "call", call)
        window.poll()
        window.refresh_generation_models(voice["id"])
        controls = window.model_controls
        controls.device.setCurrentIndex(controls.device.findData("cpu"))
        controls.speed.setValue(1.3)
        controls.reference_text.setPlainText("下载期间填写的参考原文")
        controls.advanced_toggle.setChecked(True)
        controls.runtime_toggle.setChecked(True)
        assert not controls.engine_download_button.isHidden()
        controls.engine_download_button.click()
        engines.threads["cpu"].join(3)
        window.poll()
        assert engines.states["cpu"]["status"] == "done"
        assert window.current_voice()["id"] == voice["id"]
        assert controls.assets["engine_executable"].text() == manager.config.read()["native_engine"]
        assert controls.engine_download_button.isHidden() and controls.device.currentData() == "cpu"
        assert controls.speed.value() == 1.3 and controls.reference_text.toPlainText() == "下载期间填写的参考原文"
        assert controls.advanced_toggle.isChecked() and controls.runtime_toggle.isChecked()
        health = client.get("/health").json()
        assert "engine_download" in health["capabilities"] and health["engine"]["ready"]
        assert client.post("/models/runtime/use", json={"backend": "cpu"}).status_code == 200
        window.close()
