"""Version changes retain working engines, model files, presets and snapshots."""
from copy import deepcopy
import hashlib
from pathlib import Path
import threading
import time

import httpx
import pytest

from voice_workbench.config import atomic_json
from voice_workbench.engine_updates import release_catalog
from voice_workbench.errors import VoiceError
from voice_workbench.manager import Manager
from voice_workbench.model_management import ModelManager
from test_engine_download import installer, archive
from test_inline_generation import Runner
from pe_fixture import engine_files, pe_binary
from test_model_capabilities import kokoro, gguf


@pytest.fixture(autouse=True)
def no_discovery(monkeypatch):
    monkeypatch.setattr("voice_workbench.discovery.discover_yovoice", lambda _: [])


def update_engine(engines, payloads, cuda=False):
    backend = "cuda" if cuda else "cpu"
    files = engine_files(cuda)
    executable = next(name for name in files if name.endswith("audiocpp_server.exe"))
    files[executable] += b"new engine revision"
    payloads["new-engine.zip"] = archive(files)
    old_entry = engines.entries[backend]
    entry = {**old_entry, "archives": [
        {"name": "new-engine.zip", "size": len(payloads["new-engine.zip"]), "sha256": hashlib.sha256(payloads["new-engine.zip"]).hexdigest()},
        *old_entry["archives"][1:]]}
    engines.recommended = {"version": "v0.9.1", "backends": [entry]}
    engines._probe = lambda path: None


@pytest.mark.parametrize("cuda", [False, True])
def test_engine_upgrade_waits_keeps_previous_and_can_rollback(tmp_path, cuda):
    manager = Manager(tmp_path, Runner())
    try:
        engines, payloads = installer(manager, cuda=cuda)
        backend = "cuda" if cuda else "cpu"
        engines.start(backend); engines.threads[backend].join(3)
        old = manager.config.read()["native_engine"]
        original = Path(old).read_bytes()
        update_engine(engines, payloads, cuda)
        manager.current = ("active-generation", 1)
        engines.start(backend, upgrade=True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and engines.states[backend]["status"] != "waiting":
            time.sleep(.01)
        assert engines.states[backend]["status"] == "waiting"
        assert manager.config.read()["native_engine"] == old
        assert Path(old).read_bytes() == original
        if cuda:
            assert engines.states[backend]["reused_archives"] == ["cudart.zip"]
        manager.current = None
        engines.threads[backend].join(3)
        assert engines.states[backend]["status"] == "done"
        configured = manager.config.read()
        new = configured["native_engine"]
        assert new != old and configured["native_engine_version"] == "v0.9.1"
        assert Path(old).read_bytes() == original
        assert engines.listing()["options"][0]["can_rollback"]
        engines.rollback(backend)
        assert manager.config.read()["native_engine"] == old
        assert Path(new).is_file()
        assert not engines.listing()["options"][0]["can_rollback"]
        engines.start(backend, upgrade=True); engines.threads[backend].join(3)
        assert manager.config.read()["native_engine"] == new, "Reusing the installed new version must not duplicate its files"
    finally:
        manager.current = None
        manager.close()


@pytest.mark.parametrize("failure", ["checksum", "probe"])
def test_failed_engine_upgrade_preserves_active_version(tmp_path, failure):
    manager = Manager(tmp_path, Runner())
    try:
        engines, payloads = installer(manager)
        engines.start("cpu"); engines.threads["cpu"].join(3)
        original = manager.config.path.read_bytes()
        update_engine(engines, payloads)
        if failure == "checksum":
            engines.recommended["backends"][0]["archives"][0]["sha256"] = "0" * 64
        else:
            engines._probe = lambda _: (_ for _ in ()).throw(ValueError("cannot start"))
        engines.start("cpu", upgrade=True); engines.threads["cpu"].join(3)
        assert engines.states["cpu"]["status"] == "failed"
        assert manager.config.path.read_bytes() == original
        assert engines.listing()["ready"]
    finally:
        manager.close()


def release_payload():
    version = "v0.9.1"
    names = [f"audio-{version}-{suffix}" for suffix in (
        "bin-windows-x64-cpu-portable.zip", "bin-windows-x64-cuda12.4.zip", "cudart-windows-x64-cuda12.4.zip")]
    return {"tag_name": version, "assets": [{"name": name, "size": 20, "digest": "sha256:"+"a"*64,
             "browser_download_url": f"https://github.com/0xShug0/audio.cpp/releases/download/{version}/{name}"} for name in names]}


def test_release_parser_rejects_unsigned_or_unexpected_assets():
    value = release_payload()
    assert release_catalog(value)["version"] == "v0.9.1"
    for change in ({"digest": None}, {"browser_download_url": "https://example.com/engine.zip"}, {"size": -1}):
        broken = deepcopy(value)
        broken["assets"][0].update(change)
        with pytest.raises(ValueError):
            release_catalog(broken)
    with pytest.raises(ValueError):
        release_catalog({**value, "prerelease": True})
    for version in ("../v0.9.1", "latest", "v0.9.1-beta"):
        with pytest.raises(ValueError):
            release_catalog({**value, "tag_name": version})


def test_failed_update_check_retains_known_release_and_blocks_overlapping_install(tmp_path):
    manager = Manager(tmp_path, Runner())
    entered, release = threading.Event(), threading.Event()
    errors = []
    try:
        engines, payloads = installer(manager)
        known = deepcopy(engines.recommended)
        def handler(request):
            entered.set()
            assert release.wait(3)
            return httpx.Response(503)
        engines.client_factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
        def check():
            try:
                engines.check_updates()
            except VoiceError as exc:
                errors.append(exc)
        thread = threading.Thread(target=check)
        thread.start()
        assert entered.wait(3)
        with pytest.raises(VoiceError, match="已有推理引擎"):
            engines.start("cpu", upgrade=True)
        release.set(); thread.join(3)
        assert errors and not engines.checking_updates and engines.recommended == known
    finally:
        release.set()
        manager.close()


def model_setup(tmp_path, metadata_family="kokoro_tts", corrupt=False):
    manager = Manager(tmp_path / "data", Runner())
    voice = kokoro(tmp_path)
    old = Path(voice["model_dir"])
    entry = {"id": "kokoro-test", "name": "Kokoro", "precision": "Q8", "family": "kokoro_tts", "remotePath": "Kokoro/voice.gguf",
             "size": old.stat().st_size, "sha256": hashlib.sha256(old.read_bytes()).hexdigest()}
    new_file = tmp_path / "updated.gguf"
    gguf(new_file, {"family": metadata_family, "schema_version": 1, "capabilities": {"tts": ["built_in_voices"]}},
         voices=["zf_xiaobei", "af_heart"], config={"new_version": 2})
    payload = new_file.read_bytes()
    revision = "a" * 40
    metadata = {"sha": revision, "siblings": [{"rfilename": entry["remotePath"],
        "lfs": {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}}]}
    def handler(request):
        return httpx.Response(200, json=metadata) if "/api/models/" in request.url.path else httpx.Response(200,content=(payload[:-1]+b"?") if corrupt else payload)
    factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
    manager.models.close()
    manager.models = models = ModelManager(manager, [entry], factory)
    voice.update(id="preset", name="Keep this preset", python="python.exe", voice_mode="builtin", request_options={})
    manager.config.save_voice(voice)
    models.check_updates("huggingface")
    return manager, models, old, voice


def test_model_update_is_pinned_preserves_old_file_preset_and_rollback(tmp_path):
    manager, models, old, voice = model_setup(tmp_path)
    try:
        original = old.read_bytes()
        model = next(m for m in models.installed() if m["catalog_id"] == "kokoro-test")
        assert model["update_available"]
        manager.current = ("active-generation", 1)
        models.start_update(model["id"], "huggingface")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and models.downloads["kokoro-test"]["status"] != "waiting":
            time.sleep(.01)
        assert models.downloads["kokoro-test"]["status"] == "waiting"
        assert next(v for v in manager.config.read()["voices"] if v["id"] == voice["id"])["model_dir"] == str(old)
        manager.current = None
        models.threads["kokoro-test"].join(3)
        assert models.downloads["kokoro-test"]["status"] == "done"
        preset = next(v for v in manager.config.read()["voices"] if v["id"] == voice["id"])
        assert all(preset[k] == voice[k] for k in ("id", "name", "voice_mode", "native_voice", "request_options"))
        assert preset["model_dir"] != str(old) and old.read_bytes() == original
        updated = next(m for m in models.installed() if m["catalog_id"] == "kokoro-test")
        assert not updated["update_available"] and updated["can_rollback"]
        models.rollback(updated["id"])
        assert next(v for v in manager.config.read()["voices"] if v["id"] == voice["id"])["model_dir"] == str(old)
        assert Path(preset["model_dir"]).is_file()
    finally:
        manager.current = None
        manager.close()


@pytest.mark.parametrize("family,corrupt", [("voxcpm2", False), ("kokoro_tts", True)])
def test_invalid_model_update_leaves_working_configuration(tmp_path, family, corrupt):
    manager, models, old, voice = model_setup(tmp_path, family, corrupt)
    try:
        before = manager.config.path.read_bytes()
        model = next(m for m in models.installed() if m["catalog_id"] == "kokoro-test")
        models.start_update(model["id"], "huggingface")
        models.threads["kokoro-test"].join(3)
        assert models.downloads["kokoro-test"]["status"] == "failed"
        assert manager.config.path.read_bytes() == before and old.is_file()
    finally:
        manager.close()


def test_model_update_cache_and_partial_download_resume_after_restart(tmp_path):
    manager, models, old, voice = model_setup(tmp_path)
    try:
        entry = models.updates["kokoro-test"]
        catalog = list(models.catalog.values())
        content = (tmp_path / "updated.gguf").read_bytes()
        target = models.root / "versions" / entry["sha256"][:16] / old.name
        target.parent.mkdir(parents=True)
        target.with_name(target.name + ".part").write_bytes(content[:50])
        models.close()
        requests = []
        def handler(request):
            requests.append(request)
            assert request.headers["range"] == "bytes=50-"
            assert "/resolve/" + entry["revision"] + "/" in request.url.path
            return httpx.Response(206, content=content[50:], headers={"Content-Range": f"bytes 50-{len(content)-1}/{len(content)}"})
        factory = lambda: httpx.Client(transport=httpx.MockTransport(handler))
        manager.models = models = ModelManager(manager, catalog, factory)
        model = next(m for m in models.installed() if m["catalog_id"] == "kokoro-test")
        assert model["update_available"]
        models.start_update(model["id"], "huggingface")
        models.threads["kokoro-test"].join(3)
        assert models.downloads["kokoro-test"]["status"] == "done" and len(requests) == 1
        assert old.is_file() and target.read_bytes() == content
    finally:
        manager.close()
