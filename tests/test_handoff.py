"""主仆模式：清单取舍、断点续传、以及交接之后的路径改写。

这里不碰真实生产库，全部用 tmp_path 造一份缩小版：一个被引用的 gguf、
一个不被引用的版本目录、一份带 WAL 的数据库，足以把每条规则逼出来。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.request

import pytest

from voice_workbench.handoff import client as handoff_client
from voice_workbench.handoff import manifest as handoff_manifest
from voice_workbench.handoff import server as handoff_server


def write(path: Path, data=b"x", text=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if text is not None:
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(data)
    return path


def write_wav(path: Path, seconds=1.5, rate=16000):
    """A real silent WAV: check_voice validates the header and the duration."""
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * int(seconds * rate))
    return path


@pytest.fixture
def master(tmp_path):
    """A miniature production tree: program, models, voices, library, venv."""
    data = tmp_path / "master" / "data"
    install = tmp_path / "master" / "install"
    write(install / "声笺.exe", b"MZ" + b"\0" * 64)
    write(install / "声笺服务.exe", b"MZ" + b"\0" * 32)
    write(install / "_internal" / "python312.dll", b"\0" * 256)
    write(install / "program-files.json", text='{"schema":1,"version":"0.7.81"}')
    write(install / "data" / "tasks.sqlite3", b"legacy in-place layout")

    write_wav(data / "voices" / "ref.wav")
    write(data / "audio" / "out.wav", b"RIFF" + b"\0" * 400)
    write(data / "logs" / "noise.log", b"no")
    write(data / "runtime" / "qwen-batch" / "pip-cache" / "cache.whl", b"no")

    weight = write(data / "models" / "qwen-batch" / "1.7B" / "rev-a" / "model.safetensors", b"\0" * 500)
    write(data / "models" / "qwen-batch" / "1.7B" / "rev-a" / "config.json", text="{}")
    # 0.6B 是另一套完整权重，设置里指向 1.7B 时它不该被搬走。
    spare = write(data / "models" / "qwen-batch" / "0.6B" / "rev-b" / "model.safetensors", b"\0" * 500)
    used = write(data / "models" / "versions" / "aaaaaaaaaaaaaaaa" / "qwen3-tts.gguf", b"GGUF" + b"\0" * 90)
    unused = write(data / "models" / "versions" / "bbbbbbbbbbbbbbbb" / "other.gguf", b"GGUF" + b"\0" * 90)
    engine = write(data / "runtime" / "v0.7.4-cuda-1" / "audiocpp_server.exe", b"MZ" + b"\0" * 20)
    idle = write(data / "runtime" / "v0.9.1-cuda-2" / "audiocpp_server.exe", b"MZ" + b"\0" * 20)

    venv = data / "runtime" / "qwen-batch" / "env-cuda"
    write(venv / "pyvenv.cfg", text=(
        f"home = {data}\\runtime\\qwen-batch\\python\n"
        f"executable = {data}\\runtime\\qwen-batch\\python\\python.exe\n"
        "version = 3.12.14\n"))

    voice = {"id": "voice-1", "name": "御姐", "backend": "audio_cpp", "family": "qwen3_tts",
             "reference_audio": str(data / "voices" / "ref.wav"), "model_dir": str(used),
             "engine_executable": str(engine), "device": "cuda", "language": "zh"}
    write(data / "config.json", text=json.dumps({
        "voices": [voice], "references": [{"id": "r", "name": "参考", "reference_audio": str(data / "voices" / "ref.wav")}],
        "batch_inference": {"python": str(venv / "Scripts" / "python.exe"), "model_dir": str(weight.parent),
                            "source_model": str(used), "device": "cuda"},
    }, ensure_ascii=False, indent=2))

    with sqlite3.connect(data / "tasks.sqlite3") as db, db:
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, voice TEXT)")
        db.execute("CREATE TABLE segments (job_id TEXT, idx INTEGER, voice TEXT, provenance TEXT)")
        db.execute("CREATE TABLE cache (key TEXT PRIMARY KEY, path TEXT)")
        db.execute("INSERT INTO jobs VALUES (?,?)", ("j", json.dumps(voice, ensure_ascii=False)))
        db.execute("INSERT INTO cache VALUES (?,?)", ("k", str(data / "audio" / "out.wav")))
    return {"data": data, "install": install, "used": used, "unused": unused, "idle": idle,
            "weight": weight, "spare": spare}


def until_ready(state, limit=120):
    started = time.time()
    while time.time() - started < limit:
        status = state.status()
        if status["phase"] == "ready":
            return status
        if status["phase"] == "error":
            raise AssertionError(status["error"])
        time.sleep(0.05)
    raise AssertionError("交接准备超时")


@pytest.fixture
def running(master):
    # HandoffState snapshots the database itself while preparing.
    state = handoff_server.HandoffState(master["data"], master["install"], "0.7.81")
    state.start("batch")
    status = until_ready(state)
    yield state, status
    state.stop()


# -- 清单取舍 -------------------------------------------------------------

def test_manifest_keeps_program_and_skips_cache_and_legacy_layout(master):
    entries = handoff_manifest.build(master["data"], "batch", master["install"], snapshot=None,
                                     version="0.7.81", cache_only=True)["entries"]
    rels = {entry["rel"] for entry in entries}
    assert "声笺.exe" in rels and "_internal/python312.dll" in rels
    assert "voices/ref.wav" in rels and "audio/out.wav" in rels
    assert "logs/noise.log" not in rels
    assert "runtime/qwen-batch/pip-cache/cache.whl" not in rels
    assert "tasks.sqlite3" not in rels
    assert not any(rel.startswith("data/") for rel in rels), "安装目录里的旧 data 不该被交接"
    assert {entry["target"] for entry in entries} == {"install", "data"}


def test_batch_scope_leaves_native_engine_behind(master):
    """纯批量机：GGUF 与原生引擎都不跟过去，只带 HF 权重。"""
    rels = {entry["rel"] for entry in handoff_manifest.build(
        master["data"], "batch", master["install"], version="", cache_only=True)["entries"]}
    assert "models/qwen-batch/1.7B/rev-a/model.safetensors" in rels
    assert "models/versions/aaaaaaaaaaaaaaaa/qwen3-tts.gguf" not in rels
    assert "runtime/v0.7.4-cuda-1/audiocpp_server.exe" not in rels


def test_full_scope_still_brings_native_engine(master):
    rels = {entry["rel"] for entry in handoff_manifest.build(
        master["data"], "full", master["install"], version="", cache_only=True)["entries"]}
    assert "models/versions/aaaaaaaaaaaaaaaa/qwen3-tts.gguf" in rels
    assert "runtime/v0.7.4-cuda-1/audiocpp_server.exe" in rels


def test_batch_only_preset_passes_checks_without_gguf(master):
    """仆人机的预设指向 HF 目录、没有引擎，仍要能通过校验并进批量。"""
    from voice_workbench.model_info import model_ui
    from voice_workbench.runner import check_voice

    config = json.loads((master["data"] / "config.json").read_text(encoding="utf-8"))
    voice = dict(config["voices"][0], model_dir=str(master["weight"].parent),
                 engine_executable="",
                 # 原生引擎才有的参数：纯批量机读不到 spec，不该被它拦住。
                 request_options={"stream_chunk_policy": "grow"})
    voice["python"] = sys.executable
    check_voice(voice)
    info = model_ui(voice)
    assert info["modes"] == ["clone"] and info["family"] == "qwen3_tts"
    assert "zh" in info["languages"]


def test_diagnose_accepts_hf_directory_as_model_identity(tmp_path, monkeypatch):
    from voice_workbench.batch_inference.backend import BatchBackend
    from voice_workbench.batch_inference.schemas import BatchSettings

    model = tmp_path / "1.7B" / "rev"
    talker = {"hidden_size": 2048, "num_hidden_layers": 28, "num_attention_heads": 16}
    config = {"model_type": "qwen3_tts", "tts_model_type": "base", "talker_config": talker}
    write(model / "config.json", text=json.dumps(config))
    write(model / "model.safetensors", b"\0" * 16)
    write(model / "speech_tokenizer" / "model.safetensors", b"\0" * 16)

    backend = BatchBackend.__new__(BatchBackend)
    monkeypatch.setattr(BatchBackend, "diagnose_python", lambda self, python: ("", {}))
    settings = BatchSettings(enabled=True, python=sys.executable, model_dir=str(model),
                             source_model=str(model), device="cuda")
    assert backend.diagnose(settings)[0] == ""


def test_manifest_brings_one_weight_set_only(master):
    """设置指向 1.7B 时，0.6B 那套 2.4G 权重不该跟着走；全量镜像才照搬。"""
    batch = {entry["rel"] for entry in handoff_manifest.build(
        master["data"], "batch", master["install"], version="", cache_only=True)["entries"]}
    assert "models/qwen-batch/1.7B/rev-a/model.safetensors" in batch
    assert "models/qwen-batch/0.6B/rev-b/model.safetensors" not in batch

    full = {entry["rel"] for entry in handoff_manifest.build(
        master["data"], "full", master["install"], version="", cache_only=True)["entries"]}
    assert "models/qwen-batch/0.6B/rev-b/model.safetensors" in full


def test_full_scope_brings_everything_but_still_skips_cache(master):
    rels = {entry["rel"] for entry in handoff_manifest.build(
        master["data"], "full", master["install"], version="", cache_only=True)["entries"]}
    assert "models/versions/bbbbbbbbbbbbbbbb/other.gguf" in rels
    assert "runtime/v0.9.1-cuda-2/audiocpp_server.exe" in rels
    assert "runtime/qwen-batch/pip-cache/cache.whl" not in rels


def test_database_travels_only_as_a_consistent_snapshot(master):
    snapshot = handoff_server.snapshot_database(master["data"])
    try:
        entries = handoff_manifest.build(master["data"], "batch", master["install"],
                                         snapshot=snapshot, version="", cache_only=True)["entries"]
        database = next(entry for entry in entries if entry["rel"] == "tasks.sqlite3")
        assert Path(database["source"]) == snapshot
        assert database["source"] != str(master["data"] / "tasks.sqlite3")
    finally:
        snapshot.unlink(missing_ok=True)


def test_digest_cache_is_reused_for_unchanged_files(tmp_path):
    target = write(tmp_path / "a.bin", b"0123456789")
    cache = {}
    first = handoff_manifest.digest(target, cache)
    assert list(cache.values())[0]["sha256"] == first
    # A second read must not hash again: break the file content but keep stat.
    stat = target.stat()
    target.write_bytes(b"9876543210")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert handoff_manifest.digest(target, cache) == first


# -- 传输 -----------------------------------------------------------------

def test_range_request_returns_partial_content(running):
    state, status = running
    index = next(i for i, entry in enumerate(state.entries) if entry["rel"] == "audio/out.wav")
    url = f"http://127.0.0.1:{status['port']}/{status['code']}/file/{index}"
    request = urllib.request.Request(url, headers={"Range": "bytes=10-"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request) as response:
        assert response.status == 206
        body = response.read()
        assert response.headers["Content-Range"].endswith(f"/{state.entries[index]['size']}")
    assert body == (state.sources[index].read_bytes()[10:])


def test_wrong_code_is_refused(running):
    _state, status = running
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener.open(f"http://127.0.0.1:{status['port']}/WRONGCODE/manifest.json")
    assert caught.value.code == 403


def test_bootstrap_zip_carries_launcher_and_readme(running):
    state, status = running
    payload = handoff_server.build_bootstrap_zip("192.168.1.5", status["port"], "ABCD2345",
                                                 "batch", state.manifest)
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = set(archive.namelist())
        launcher = archive.read("开始接收.bat").decode("utf-8")
        config = json.loads(archive.read("handoff.json"))
    assert {"开始接收.bat", "handoff.json", "使用说明.txt"} <= names
    assert "handoff-receive" in launcher and "ABCD2345" in launcher
    assert config["host"] == "192.168.1.5" and config["code"] == "ABCD2345"


# -- 接收与修复 -----------------------------------------------------------

def test_receive_relocates_venv_config_and_database(running, master, tmp_path):
    _state, status = running
    target_data, target_install = tmp_path / "slave" / "data", tmp_path / "slave" / "install"
    base = f"http://127.0.0.1:{status['port']}/{status['code']}"

    result = handoff_client.receive(base, target_data, target_install)
    assert result["downloaded"] > 0
    assert (target_install / "声笺.exe").is_file()
    assert (target_data / "models" / "qwen-batch" / "1.7B" / "rev-a" / "model.safetensors").is_file()

    venv = (target_data / "runtime" / "qwen-batch" / "env-cuda" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "slave" in venv and "master" not in venv

    config = json.loads((target_data / "config.json").read_text(encoding="utf-8"))
    assert "slave" in config["voices"][0]["reference_audio"]
    assert "slave" in config["batch_inference"]["python"]
    assert "master" not in json.dumps(config, ensure_ascii=False)

    with sqlite3.connect(target_data / "tasks.sqlite3") as db:
        stored = json.loads(db.execute("SELECT voice FROM jobs").fetchone()[0])
        cached = db.execute("SELECT path FROM cache").fetchone()[0]
    assert "slave" in stored["reference_audio"] and "slave" in cached


def test_receive_points_batch_only_presets_at_local_weights(running, tmp_path):
    """只带批量资产时，预设与批量后端在目标机上必须指向同一个 HF 目录。"""
    _state, status = running
    target_data, target_install = tmp_path / "slave" / "data", tmp_path / "slave" / "install"
    base = f"http://127.0.0.1:{status['port']}/{status['code']}"
    handoff_client.receive(base, target_data, target_install)

    config = json.loads((target_data / "config.json").read_text(encoding="utf-8"))
    weight = target_data / "models" / "qwen-batch" / "1.7B" / "rev-a"
    voice = config["voices"][0]
    assert Path(voice["model_dir"]) == weight
    assert voice["engine_executable"] == ""
    assert Path(config["batch_inference"]["source_model"]) == weight
    assert not (target_data / "models" / "versions").exists(), "纯批量交接不该带 GGUF"

    # 改指之后预设仍要能过校验，否则界面上生成按钮会一直是灰的。
    from voice_workbench.model_info import model_ui
    from voice_workbench.runner import check_voice

    voice["python"] = sys.executable
    check_voice(voice)
    assert model_ui(voice)["modes"] == ["clone"]


def test_second_receive_transfers_nothing(running, tmp_path):
    _state, status = running
    target_data, target_install = tmp_path / "slave" / "data", tmp_path / "slave" / "install"
    base = f"http://127.0.0.1:{status['port']}/{status['code']}"
    handoff_client.receive(base, target_data, target_install)
    again = handoff_client.receive(base, target_data, target_install)
    # Rebasing changes sizes and digests, so this also guards the "rewritten"
    # record: without it the database and config.json would come down again.
    assert again["downloaded"] == 0


def test_receive_resumes_from_partial_file(running, tmp_path):
    state, status = running
    index = next(i for i, entry in enumerate(state.entries) if entry["rel"] == "audio/out.wav")
    entry, source = state.entries[index], state.sources[index]
    target_data, target_install = tmp_path / "slave" / "data", tmp_path / "slave" / "install"
    target = target_data / entry["rel"]
    target.parent.mkdir(parents=True)
    partial = target.with_name(target.name + ".part")
    original = source.read_bytes()
    partial.write_bytes(original[:100])

    receiver = handoff_client.Receiver(f"http://127.0.0.1:{status['port']}/{status['code']}",
                                       target_data, target_install)
    receiver.download(entry, index)
    assert target.read_bytes() == original
    assert not partial.exists()


# -- 界面 -----------------------------------------------------------------

def test_handoff_controls_render_each_phase(monkeypatch):
    """控件只负责显示状态，所以三个阶段的渲染可以直接断言。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QWidget

    from voice_workbench.desktop.settings.common import SettingsComponents
    from voice_workbench.desktop.settings.handoff import HandoffControls

    app = QApplication.instance() or QApplication([])

    class Stub(QWidget, SettingsComponents):
        def __init__(self):
            super().__init__()
            self.owner = type("Owner", (), {"url": "http://127.0.0.1:1", "data_dir": tmp_root})()
            self.owner.call = lambda *args, **kwargs: None

    tmp_root = Path(".")
    stub = Stub()
    control = HandoffControls(stub)

    control.apply({"phase": "idle"})
    assert control.detail.isHidden()
    control.apply({"phase": "preparing"})
    assert not control.stop_button.isHidden()
    control.apply({"phase": "ready", "url": "http://192.168.1.5:9000/ABCD2345/", "code": "ABCD2345",
                   "port": 9000, "bytes": 2048, "files": 3, "sent_bytes": 512,
                   "scope_label": "仅批量生成必需",
                   "pending_pairs": [{"token": "tok-1", "address": "192.168.1.9", "created": 0}]})
    assert control.address.text() == "http://192.168.1.5:9000/ABCD2345/"
    assert "ABCD2345" in control.progress.format()
    assert control.progress.value() == 25
    # 同网段的另一台声笺广播过来时，批准权留在这台电脑上。
    assert not control.pending_row.isHidden()
    assert "192.168.1.9" in control.pending_label.text()
    control.apply({"phase": "ready", "url": "http://192.168.1.5:9000/ABCD2345/", "code": "ABCD2345",
                   "port": 9000, "bytes": 2048, "files": 3, "sent_bytes": 512, "pending_pairs": []})
    assert control.pending_row.isHidden()
    control.apply({"phase": "error", "error": "清单里的文件已消失"})
    assert "已消失" in control.status.text()
    app.processEvents()


def test_status_reports_discovery_and_pending_pairs(running):
    """界面靠这两个字段决定要不要显示「允许接入」。"""
    state, status = running
    assert isinstance(status["discovery"], bool)
    assert status["pending_pairs"] == []
    state.pairs.register("tok-1", "192.168.1.9")
    assert state.status()["pending_pairs"][0]["address"] == "192.168.1.9"


def test_allow_posts_the_token_of_the_request_on_screen(monkeypatch):
    """「允许」必须带界面上那一台的令牌，否则批准的是别人。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QWidget

    from voice_workbench.desktop.settings import handoff as handoff_ui
    from voice_workbench.desktop.settings.common import SettingsComponents

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(handoff_ui, "checked", lambda response: response)
    called = []

    class FakeClient:
        def post(self, path, json=None):
            return path

    class Stub(QWidget, SettingsComponents):
        def __init__(self):
            super().__init__()
            self.owner = type("Owner", (), {"url": "http://127.0.0.1:1", "data_dir": Path(".")})()
            self.owner.call = lambda action, success=None, failure=None: called.append(action(FakeClient()))

    control = handoff_ui.HandoffControls(Stub())
    control.apply({"phase": "ready", "url": "", "code": "ABCD2345", "port": 1, "bytes": 1, "files": 1,
                   "sent_bytes": 0,
                   "pending_pairs": [{"token": "tok-9", "address": "10.0.0.2", "created": 0}]})
    control.allow()
    assert called == ["/handoff/pair/tok-9/approve"]
    control.deny()
    assert called[-1] == "/handoff/pair/tok-9/deny"
    app.processEvents()
