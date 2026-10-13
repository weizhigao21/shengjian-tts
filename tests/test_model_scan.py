"""Engine-bound discovery, immutable metadata, cache recovery and UI filtering."""
import hashlib
import json
from pathlib import Path
import threading

import httpx
import pytest

from voice_workbench.config import atomic_json
from voice_workbench.errors import VoiceError
from voice_workbench.model_management import ModelManager
from voice_workbench.model_scan import safe_remote_path
from test_asset_management import model_fixture


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])


def spec(family, paths, tasks=None, repo="audio-cpp/audio.cpp-gguf"):
    return {"family": family, "display_name": family, "status": "supported", "tasks": tasks or ["tts"],
            "package_defaults": {"download": {"kind": "huggingface_snapshot", "repo": repo}},
            "packages": [{"display_name": f"{family} Q8", "format": "gguf", "precision": "q8_0", "files": [p]} for p in paths]}


def setup_scan(tmp_path, extra=None):
    manager, _, content, entry = model_fixture(tmp_path)
    directory = Path(manager.models.runtime()).parent / "model_specs"
    directory.mkdir()
    declarations = [spec("kokoro_tts", [entry["remotePath"], "Kokoro/new.gguf"]),
                    spec("chatterbox", ["Chatterbox/model.gguf"]),
                    spec("qwen3_asr", ["ASR/model.gguf"], ["stt"])] + (extra or [])
    for i, value in enumerate(declarations):
        atomic_json(directory / f"{i}.json", value)
    files = [p for s in declarations for pkg in s["packages"] for p in pkg["files"] if p.endswith(".gguf")]
    metadata = {"sha": "a" * 40, "siblings": [{"rfilename": p, "lfs": {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}} for p in files]}
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=metadata) if "/api/models/" in request.url.path else httpx.Response(200, content=content)
    manager.models.close()
    manager.models = ModelManager(manager, [entry], lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return manager, metadata, requests, directory, entry


def finish(models):
    models.scanner.thread.join(5)
    assert not models.scanner.thread.is_alive()
    assert not models.scanner.scanning


def test_auto_scan_expands_catalog_preserves_ids_and_downloads_only_metadata(tmp_path):
    manager, _, requests, _, entry = setup_scan(tmp_path)
    try:
        manager.models.listing()
        finish(manager.models)
        value = manager.models.listing()
        assert len(requests) == 1 and requests[0].url.params["blobs"] == "true"
        assert len(value["scan"]["families"]) == 3 and len(value["catalog"]) == 4
        known = next(m for m in value["catalog"] if m["remotePath"] == entry["remotePath"])
        assert known["id"] == entry["id"] and known["revision"] == "a" * 40
        assert known["downloadable"]
        pending = next(m for m in value["catalog"] if m["family"] == "qwen3_asr")
        assert not pending["adapted"] and pending["downloadable"] and "语音识别" in pending["description"]
        assert not list(manager.models.root.rglob("*.gguf"))
    finally:
        manager.close()


def test_new_adapted_package_uses_pinned_verified_download_and_separate_directory(tmp_path):
    manager, _, requests, _, _ = setup_scan(tmp_path)
    try:
        manager.models.scanner.start("huggingface")
        finish(manager.models)
        entry = next(m for m in manager.models.listing()["catalog"] if m["remotePath"] == "Kokoro/new.gguf")
        manager.models.start_download(entry["id"], "huggingface")
        manager.models.threads[entry["id"]].join(5)
        assert manager.models.downloads[entry["id"]]["status"] == "done"
        assert f"/resolve/{'a' * 40}/Kokoro/new.gguf" in str(requests[-1].url)
        target = manager.models.download_target(entry)
        assert target.parent.name == entry["id"] and target.is_file()
        installed = next(m for m in manager.models.installed() if m["catalog_id"] == entry["id"])
        assert installed["name"] == entry["name"]
    finally:
        manager.close()


def test_cached_scan_restores_offline_and_failure_retains_catalog(tmp_path):
    manager, _, requests, _, entry = setup_scan(tmp_path)
    try:
        manager.models.scanner.start()
        finish(manager.models)
        before = {m["id"] for m in manager.models.listing()["catalog"]}
        manager.models.close()
        def offline(request):
            raise httpx.ConnectError("offline", request=request)
        manager.models = ModelManager(manager, [entry], lambda: httpx.Client(transport=httpx.MockTransport(offline)))
        value = manager.models.listing()
        assert {m["id"] for m in value["catalog"]} == before and not value["scan"]["scanning"]
        manager.models.scanner.start()
        finish(manager.models)
        assert {m["id"] for m in manager.models.listing()["catalog"]} == before
        assert "保留原有目录" in manager.models.scanner.error
    finally:
        manager.close()


@pytest.mark.parametrize("path", ["../bad.gguf", "/bad.gguf", "C:/bad.gguf", "A\\bad.gguf", "A/../bad.gguf", "A//bad.gguf", "A/bad.gguf?token=x", "A/%2e%2e/bad.gguf"])
def test_remote_paths_cannot_escape_package_or_change_url(path):
    assert not safe_remote_path(path)


def test_scan_rejects_unverified_files_and_multifile_packages_are_not_downloadable(tmp_path):
    extra = spec("voxcpm2", ["Vox/main.gguf"])
    extra["packages"][0]["files"].append("Vox/codec.gguf")
    manager, metadata, _, _, _ = setup_scan(tmp_path, [extra])
    try:
        metadata["siblings"][1]["lfs"]["sha256"] = "invalid"
        manager.models.scanner.start()
        finish(manager.models)
        catalog = manager.models.listing()["catalog"]
        assert not any(m["remotePath"] == "Kokoro/new.gguf" for m in catalog)
        multipart = [m for m in catalog if m["family"] == "voxcpm2"]
        assert multipart and all(not m["downloadable"] for m in multipart)
    finally:
        manager.close()


def test_tampered_cache_cannot_add_packages_or_override_adapter_status(tmp_path):
    manager, _, _, _, entry = setup_scan(tmp_path)
    try:
        manager.models.scanner.start()
        finish(manager.models)
        path = manager.models.scanner.path
        cached = json.loads(path.read_text(encoding="utf-8"))
        unknown = next(e for e in cached["entries"] if e["family"] == "qwen3_asr")
        unknown.update(id="fake-id", adapted=True)
        injected = {**unknown, "remotePath": "Evil/other.gguf"}
        cached["entries"].append(injected)
        atomic_json(path, cached)
        manager.models.close()
        manager.models = ModelManager(manager, [entry], lambda: pytest.fail("fresh cache must not use network"))
        catalog = manager.models.listing()["catalog"]
        assert not any(m["id"] == "fake-id" or m["remotePath"] == injected["remotePath"] for m in catalog)
        assert not next(m for m in catalog if m["family"] == "qwen3_asr")["adapted"]
    finally:
        manager.close()


def test_engine_changed_during_scan_discards_old_result_then_rescans(tmp_path):
    manager, metadata, _, _, _ = setup_scan(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def delayed(request):
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json=metadata)
    manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(delayed))
    try:
        manager.models.scanner.start()
        assert entered.wait(2)
        new = tmp_path / "new-engine"
        (new / "model_specs").mkdir(parents=True)
        exe = new / "audiocpp_server.exe"
        exe.write_bytes(b"new engine")
        atomic_json(new / "model_specs/kokoro.json", spec("kokoro_tts", ["Kokoro/new.gguf"]))
        manager.models.set_runtime(str(exe))
        release.set()
        finish(manager.models)
        assert not manager.models.scanner.path.exists()
        manager.models.listing()
        finish(manager.models)
        assert len(manager.models.listing()["scan"]["families"]) == 1
    finally:
        release.set()
        manager.close()


def test_missing_manifest_has_actionable_error(tmp_path):
    manager, _, _, _ = model_fixture(tmp_path)
    try:
        with pytest.raises(VoiceError, match="model_specs"):
            manager.models.scanner.start()
        assert manager.models.listing()["catalog"]
    finally:
        manager.close()


def test_settings_scan_summary_filters_and_pending_actions(tmp_path, qtbot):
    from voice_workbench.gui import Window
    from PyQt6.QtWidgets import QPushButton
    manager, _, _, _, _ = setup_scan(tmp_path)
    window = Window(8799, tmp_path / "gui", autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    panel = window.settings
    panel.timer.stop()
    try:
        manager.models.scanner.start()
        finish(manager.models)
        panel.model_data = manager.models.listing()
        panel.render_models()
        assert "3 个模型家族" in panel.model_scan_notice.text()
        panel.model_filter.setCurrentIndex(panel.model_filter.findData("stt"))
        assert [m["family"] for m in panel.visible_catalog] == ["qwen3_asr"]
        action = panel.catalog_table.cellWidget(0, 4)
        assert isinstance(action, QPushButton) and action.text() == "下载" and action.isEnabled()
        assert "尚未适配" in action.toolTip()
        panel.model_filter.setCurrentIndex(panel.model_filter.findData("adapted"))
        assert len(panel.visible_catalog) == 2 and all(m["downloadable"] for m in panel.visible_catalog)
        panel.model_search.setText("new")
        # Search includes the human-friendly model labels, not hidden file paths.
        assert panel.catalog_table.rowCount() == 0
    finally:
        window.close()
        manager.close()


def test_engine_only_weights_can_be_downloaded_removed_and_restored_without_voice_preset(tmp_path):
    from test_model_capabilities import gguf
    manager, metadata, _, _, _ = setup_scan(tmp_path)
    weight = tmp_path / "asr.gguf"
    gguf(weight, {"family": "qwen3_asr", "schema_version": 1})
    payload = weight.read_bytes()
    for item in metadata["siblings"]:
        if item["rfilename"] == "ASR/model.gguf":
            item["lfs"] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    def handler(request):
        return httpx.Response(200, json=metadata) if "/api/models/" in request.url.path else httpx.Response(200, content=payload)
    manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    try:
        before = manager.config.voices()
        manager.models.scanner.start()
        finish(manager.models)
        entry = next(m for m in manager.models.listing()["catalog"] if m["family"] == "qwen3_asr")
        manager.models.start_download(entry["id"])
        manager.models.threads[entry["id"]].join(3)
        assert manager.models.downloads[entry["id"]]["status"] == "done"
        assert manager.config.voices() == before
        installed = next(m for m in manager.models.installed() if m["catalog_id"] == entry["id"])
        assert installed["preset_count"] == 0 and not installed["adapted"]
        manager.models.remove(installed["id"], False)
        assert Path(installed["path"]).exists() and not manager.models.installed()
        manager.models.start_download(entry["id"])
        manager.models.threads[entry["id"]].join(3)
        manager.models.remove(installed["id"], True)
        assert not Path(installed["path"]).exists() and not manager.models.installed()
    finally:
        manager.close()


def test_partial_repository_failure_retains_packages_in_persistent_cache(tmp_path):
    extra = spec("extra_tts", ["Extra/model.gguf"], repo="example/extra-gguf")
    manager, metadata, _, _, entry = setup_scan(tmp_path, [extra])
    try:
        manager.models.scanner.start()
        finish(manager.models)
        before = {m["id"] for m in manager.models.listing()["catalog"]}
        def partial(request):
            if "example" in request.url.path:
                raise httpx.ConnectError("temporarily offline", request=request)
            return httpx.Response(200, json=metadata)
        manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(partial))
        manager.models.scanner.start()
        finish(manager.models)
        assert manager.models.scanner.warnings and not manager.models.scanner.error
        assert {m["id"] for m in manager.models.listing()["catalog"]} == before
        manager.models.close()
        manager.models = ModelManager(manager, [entry], lambda: pytest.fail("use cache"))
        restored = manager.models.listing()
        assert {m["id"] for m in restored["catalog"]} == before and restored["scan"]["warnings"]
    finally:
        manager.close()


def test_http_scan_returns_immediately_and_status_is_polled_through_models(tmp_path):
    from fastapi.testclient import TestClient
    from voice_workbench.api import create_app
    manager, metadata, _, _, _ = setup_scan(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def delayed(request):
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json=metadata)
    manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(delayed))
    app = create_app(tmp_path / "unused")
    app.state.manager = manager
    client = TestClient(app)  # No lifespan: use the isolated manager above.
    try:
        response = client.post("/models/scan", json={"source": "huggingface"})
        assert response.status_code == 202 and response.json()["scanning"]
        assert entered.wait(2)
        assert client.get("/models").json()["scan"]["scanning"]
        release.set()
        finish(manager.models)
        assert len(client.get("/models").json()["catalog"]) == 4
    finally:
        release.set()
        client.close()
        manager.close()
