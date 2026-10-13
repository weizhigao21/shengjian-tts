import errno
import json
import mmap
import os
from pathlib import Path
import sys
import threading

import pytest

from voice_workbench.audio_cpp import AudioCppRunner
from voice_workbench.native_paths import NativePaths
from voice_workbench.manager import Manager
from test_model_capabilities import kokoro
from voice_workbench.schemas import VoiceConfig

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows path compatibility")


def test_unicode_model_and_sidecars_use_hard_links_cleanup_preserves_original(tmp_path):
    folder = tmp_path / "中文模型"
    folder.mkdir()
    model = folder / "权重.gguf"
    model.write_bytes(b"GGUF original weights")
    (folder / "config.json").write_text('{"source":"original"}')
    (folder / "speech_tokenizer").mkdir()
    sidecar = folder / "speech_tokenizer/config.json"
    sidecar.write_text('{"speech":true}')
    paths = NativePaths()
    alias = Path(paths.model(model))
    assert str(alias).isascii() and os.path.samefile(alias, model)
    assert os.path.samefile(alias.parent / "speech_tokenizer/config.json", sidecar)
    config = paths.config({"models": [{"path": str(alias)}]})
    assert str(config).isascii() and json.loads(config.read_text())["models"][0]["path"] == str(alias)
    root = paths.root
    paths.close()
    assert not root.exists()
    assert model.read_bytes() == b"GGUF original weights" and sidecar.is_file()
    paths.close()  # A repeated shutdown is harmless.


def test_cross_volume_alias_uses_ascii_model_ancestor_then_removes_its_own_directory(tmp_path, monkeypatch):
    folder = tmp_path / "中文模型"
    folder.mkdir()
    model = folder / "weights.gguf"
    model.write_bytes(b"GGUF original")
    original_link, attempts = os.link, []
    def cross_drive_once(source, target):
        attempts.append(target)
        if len(attempts) == 1:
            raise OSError(errno.EXDEV, "cross volume")
        original_link(source, target)
    monkeypatch.setattr(os, "link", cross_drive_once)
    paths = NativePaths()
    alias = Path(paths.model(model))
    assert alias.parent.parent == tmp_path and str(alias).isascii()
    assert os.path.samefile(alias, model) and len(paths.roots) == 2
    paths.close()
    assert not alias.parent.exists() and model.is_file()
    assert list(tmp_path.iterdir()) == [folder]


def test_ascii_model_does_not_create_a_link(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    monkeypatch.setattr(os, "link", lambda *args: pytest.fail("ASCII paths should remain unchanged"))
    paths = NativePaths()
    assert paths.model(model) == str(model)
    paths.close()


def test_runner_reuses_native_model_path_and_removes_it_after_stopping_process(tmp_path, monkeypatch):
    folder = tmp_path / "中文配置"
    folder.mkdir()
    voice = VoiceConfig(**{**kokoro(folder), "id": "kokoro", "name": "test", "python": sys.executable,
                          "engine_executable": sys.executable, "device": "cpu"}).model_dump()
    launches = []
    class Process:
        returncode = None
        def poll(self):return self.returncode
        def kill(self):self.returncode = 0
        def wait(self, timeout=None):return 0
    def launch(command, **kwargs):
        config = Path(command[2])
        launches.append((config, json.loads(config.read_text())))
        return Process()
    class Client:
        def __init__(self, **kwargs):pass
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def get(self, url):
            class Response:status_code = 200
            return Response()
    monkeypatch.setattr("voice_workbench.audio_cpp.subprocess.Popen", launch)
    monkeypatch.setattr("voice_workbench.audio_cpp.httpx.Client", Client)
    runner = AudioCppRunner(tmp_path / "data")
    runner.start(voice, threading.Event())
    native_config, config = launches[0]
    alias = Path(config["models"][0]["path"])
    assert str(native_config).isascii() and os.path.samefile(alias, voice["model_dir"])
    runner.start({**voice, "native_voice": "af_heart"}, threading.Event())
    assert len(launches) == 1
    runner.interrupt()
    assert not alias.exists() and not native_config.exists()
    assert Path(voice["model_dir"]).is_file()


def test_process_launch_failure_releases_aliases_and_log_handle(tmp_path, monkeypatch):
    folder = tmp_path / "中文配置"
    folder.mkdir()
    voice = VoiceConfig(**{**kokoro(folder), "id": "kokoro", "name": "test", "python": sys.executable,
                          "engine_executable": sys.executable, "device": "cpu"}).model_dump()
    roots = []
    def fail(command, **kwargs):
        roots.append(Path(command[2]).parent)
        raise OSError("missing executable dependency")
    monkeypatch.setattr("voice_workbench.audio_cpp.subprocess.Popen", fail)
    runner = AudioCppRunner(tmp_path / "data")
    with pytest.raises(OSError, match="missing executable"):
        runner.start(voice, threading.Event())
    assert runner.native_paths is None and runner.log_handle is None
    assert roots and not roots[0].exists()
    assert Path(voice["model_dir"]).is_file()


def test_shared_mapping_defers_only_owned_aliases_and_retries_after_all_runners_stop(tmp_path):
    folder = tmp_path / "中文模型"
    folder.mkdir()
    model = folder / "权重.gguf"
    original = b"GGUF original weights"
    model.write_bytes(original)
    first, second = AudioCppRunner(tmp_path / "one"), AudioCppRunner(tmp_path / "two")
    first.native_paths, second.native_paths = NativePaths(), NativePaths()
    first_root, second_root = first.native_paths.root, second.native_paths.root
    alias = Path(first.native_paths.model(model))
    other = Path(second.native_paths.model(model))
    stream = other.open("rb")
    mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    class MappedProcess:
        stopped = False
        def poll(self): return 0 if self.stopped else None
        def kill(self):
            mapping.close()
            stream.close()
            self.stopped = True
        def wait(self, timeout=None): return 0
    process = second.process = MappedProcess()
    try:
        first.interrupt()
        assert not process.stopped  # Cancelling one lane must not stop another.
        assert alias.exists() and first.pending_paths
        assert model.read_bytes() == original
        manager = Manager.__new__(Manager)
        manager.runners = {0: first, 1: second}
        manager.release_runners()
        assert process.stopped and second.process is None
        assert not first.pending_paths and not second.pending_paths
        assert not first_root.exists() and not second_root.exists()
        assert model.read_bytes() == original
    finally:
        if not process.stopped:
            process.kill()
        first.interrupt()
        second.interrupt()
