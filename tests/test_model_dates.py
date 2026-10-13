"""Per-file dates stay tied to immutable metadata and never replace download checks."""
from urllib.parse import parse_qs

import httpx
import pytest

from voice_workbench.model_dates import installed_publication, publication_metadata, retain_publication
from voice_workbench.model_updates import repository_files
from test_model_scan import setup_scan, finish


def dated_handler(metadata, date="2026-07-23T12:52:14.000Z", unavailable=False):
    def handler(request):
        if request.method == "POST":
            if unavailable:
                return httpx.Response(503)
            paths = parse_qs(request.content.decode())["paths"]
            return httpx.Response(200, json=[{"type": "file", "path": path,
                "lastCommit": {"id": "b" * 40, "date": date}} for path in paths])
        return httpx.Response(200, json=metadata)
    return handler


def test_file_dates_use_pinned_revision_and_batches_of_at_most_50():
    requests = []
    metadata = {"sha": "a" * 40, "createdAt": "2000-01-01T00:00:00Z", "lastModified": "2030-01-01T00:00:00Z",
        "siblings": [{"rfilename": f"Model/{i}.gguf", "lfs": {"size": 100, "sha256": "c" * 64}} for i in range(102)]}
    handle = dated_handler(metadata)
    def handler(request):
        requests.append(request)
        return handle(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        revision, files = repository_files(client, "example/models", "huggingface")
    dates = [r for r in requests if r.method == "POST"]
    assert [len(parse_qs(r.content.decode())["paths"]) for r in dates] == [50, 50, 2]
    assert all(r.url.path.endswith("/" + revision) for r in dates)
    assert all(f["published_at"] == "2026-07-23T12:52:14Z" for f in files.values())
    assert all(f["publication_commit"] == "b" * 40 and f["sha256"] == "c" * 64 for f in files.values())


def test_optional_date_failure_does_not_fail_verified_model_discovery():
    metadata = {"sha": "a" * 40, "siblings": [{"rfilename": "Model/a.gguf", "lfs": {"size": 100, "sha256": "c" * 64}}]}
    with httpx.Client(transport=httpx.MockTransport(dated_handler(metadata, unavailable=True))) as client:
        _, files = repository_files(client, "example/models")
    assert files["Model/a.gguf"]["published_at"] is None and files["Model/a.gguf"]["size"] == 100


@pytest.mark.parametrize("date", [None, "2026-07-23", "2026-07-23T12:52:14", "wrong", "2026-99-99T12:00:00Z", 123])
def test_ambiguous_or_invalid_dates_remain_unknown(date):
    assert publication_metadata({"published_at": date})["published_at"] is None


def test_installed_date_describes_installed_weights_and_survives_update_and_rollback():
    old = {"sha256": "a" * 64, "published_at": "2026-01-02T10:00:00Z"}
    new = {"sha256": "b" * 64, "published_at": "2026-07-23T10:00:00Z"}
    assert installed_publication(new, old)["published_at"] == old["published_at"]
    assert installed_publication(new, {"sha256": old["sha256"]})["published_at"] is None
    assert installed_publication(new, {"sha256": new["sha256"]})["published_at"] == new["published_at"]
    missing = {**old, "published_at": None}
    assert retain_publication(missing, old)["published_at"] == old["published_at"]
    assert retain_publication({**new, "published_at": None}, old)["published_at"] is None


def test_date_cache_survives_offline_metadata_and_old_schema_refreshes_once(tmp_path, monkeypatch):
    from voice_workbench.config import atomic_json
    from voice_workbench.model_management import ModelManager
    import json
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    manager, metadata, _, _, entry = setup_scan(tmp_path)
    try:
        manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(dated_handler(metadata)))
        manager.models.scanner.start()
        finish(manager.models)
        assert all(e["published_at"] for e in manager.models.scanner.cached_entries)
        manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(dated_handler(metadata, unavailable=True)))
        manager.models.scanner.start()
        finish(manager.models)
        assert all(e["published_at"] for e in manager.models.scanner.cached_entries)
        cached = json.loads(manager.models.scanner.path.read_text(encoding="utf-8"))
        cached.pop("schema")
        atomic_json(manager.models.scanner.path, cached)
        factory = manager.models.client_factory
        manager.models.close()
        manager.models = ModelManager(manager, [entry], factory)
        manager.models.listing()
        finish(manager.models)
        assert manager.models.scanner.checked
    finally:
        manager.close()


def test_publication_cells_show_date_and_source_for_downloaded_and_catalog_models(tmp_path, qtbot, monkeypatch):
    from voice_workbench.gui import Window
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    manager, metadata, _, _, _ = setup_scan(tmp_path)
    window = Window(8799, tmp_path / "gui", autostart=False)
    qtbot.addWidget(window)
    window.timer.stop()
    panel = window.settings
    panel.timer.stop()
    try:
        manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(dated_handler(metadata)))
        manager.models.scanner.start()
        finish(manager.models)
        panel.model_data = manager.models.listing()
        first = panel.model_data["catalog"][0]
        panel.model_data["installed"] = [{**first, "path": str(tmp_path/'model.gguf'), "exists": True, "preset_count": 0}]
        panel.render_models()
        for cell in (panel.catalog_table.item(0, 3), panel.installed_table.item(0, 2)):
            assert "2026-07-23" in cell.text() and "发布时间" in cell.text()
            assert "仓库提交日期" in cell.toolTip() and "b" * 40 in cell.toolTip()
        panel.model_data["catalog"][0]["published_at"] = None
        panel.render_models()
        assert "未知" in panel.catalog_table.item(0, 3).text()
    finally:
        window.close()
        manager.close()


def test_download_update_and_rollback_keep_each_file_publication_date(tmp_path, monkeypatch):
    from test_model_capabilities import gguf
    import hashlib
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])
    manager, metadata, _, _, _ = setup_scan(tmp_path)
    payload = (tmp_path/'small.gguf').read_bytes()
    date = "2026-07-23T12:52:14Z"
    def handler(request):
        if "/api/models/" in request.url.path:
            return dated_handler(metadata, date)(request)
        return httpx.Response(200, content=payload)
    manager.models.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    try:
        manager.models.scanner.start()
        finish(manager.models)
        entry = next(m for m in manager.models.listing()["catalog"] if m["remotePath"] == "Kokoro/new.gguf")
        manager.models.start_download(entry["id"])
        manager.models.threads[entry["id"]].join(5)
        installed = next(m for m in manager.models.installed() if m["catalog_id"] == entry["id"])
        assert installed["published_at"] == date
        new_file = tmp_path/'updated.gguf'
        gguf(new_file, {"family": "kokoro_tts", "schema_version": 1, "languages": ["zh"],
                       "options": {"request": []}}, voices=["zf_xiaobei"], config={"new_version": 2})
        payload = new_file.read_bytes()
        metadata["sha"] = "c" * 40
        date = "2026-08-25T08:00:00Z"
        file = next(f for f in metadata["siblings"] if f["rfilename"] == entry["remotePath"])
        file["lfs"] = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        manager.models.check_updates("huggingface")
        assert next(m for m in manager.models.installed() if m["id"] == installed["id"])["published_at"] == "2026-07-23T12:52:14Z"
        manager.models.start_update(installed["id"])
        manager.models.threads[entry["id"]].join(5)
        updated = next(m for m in manager.models.installed() if m["catalog_id"] == entry["id"])
        assert updated["published_at"] == date and updated["can_rollback"]
        manager.models.rollback(updated["id"])
        assert next(m for m in manager.models.installed() if m["catalog_id"] == entry["id"])["published_at"] == "2026-07-23T12:52:14Z"
    finally:
        manager.close()
