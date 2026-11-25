"""批量识别：顺序执行、自动导出、失败隔离、取消与中断恢复。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.speech_recognition.store import RecognitionStore
from test_speech_recognition import FakeRecognition, setup_engine
from test_audio_editing import wav


class SlowRecognition(FakeRecognition):
    """第一段识别要等人放行，用来观察取消和引擎互斥。"""

    def __init__(self, root):
        super().__init__(root)
        self.release = threading.Event()

    def transcribe(self, voices, audio, language, cancelled):
        self.started.set()
        self.release.wait(25)
        return super().transcribe(voices, audio, language, cancelled)

    def interrupt(self):
        self.release.set()
        super().interrupt()


class FailOnceRecognition(FakeRecognition):
    """首次识别失败，第二次成功；用来验证重试复用已导入的音频。"""

    def __init__(self, root):
        super().__init__(root)
        self.fail_next = True

    def transcribe(self, voices, audio, language, cancelled):
        if self.fail_next:
            self.fail_next = False
            self.started.set()
            raise RuntimeError("first attempt failed")
        return super().transcribe(voices, audio, language, cancelled)


def wait_batch(client, identity, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        batch = client.get(f"/recognition/batches/{identity}").json()
        if batch["status"] not in ("queued", "running", "cancelling"):
            return batch
        time.sleep(0.05)
    raise AssertionError("batch did not finish")


def test_batch_recognises_in_order_and_exports_srt_beside_each_source(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    first_folder, second_folder = tmp_path / "课程一", tmp_path / "课程二"
    first_folder.mkdir()
    second_folder.mkdir()
    first = wav(first_folder / "第一段.wav", seconds=2)
    second = wav(second_folder / "Second.wav", seconds=2)
    original = {path: path.read_bytes() for path in (first, second)}
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        response = client.post("/recognition/batches", json={"paths": [str(first), str(second)],
                                                             "language": "zh", "format": "srt"})
        assert response.status_code == 201, response.text
        batch = response.json()
        assert batch["total"] == 2 and batch["status"] == "running"
        state = wait_batch(client, batch["id"])
        assert state["status"] == "done" and state["done"] == 2 and state["failed"] == 0
        assert [item["status"] for item in state["items"]] == ["done", "done"]
        assert [Path(item["exported"][0]).name for item in state["items"]] == ["第一段.srt", "Second.srt"]
        assert Path(state["items"][0]["exported"][0]).parent == first_folder
        assert Path(state["items"][1]["exported"][0]).parent == second_folder
        content = Path(state["items"][0]["exported"][0]).read_text(encoding="utf-8")
        assert "下载进度刷新，保留滚动位置。" in content and "-->" in content
        assert original[first] == first.read_bytes() and original[second] == second.read_bytes()
        # Every queue item is an ordinary recognition record, tagged with its batch.
        session = client.get(f"/recognition/sessions/{state['items'][0]['session_id']}").json()
        assert session["state"]["batch_id"] == batch["id"] and session["state"]["status"] == "done"
        listed = client.get("/recognition/sessions").json()
        row = next(item for item in listed if item["id"] == state["items"][0]["session_id"])
        assert row["batch_id"] == batch["id"] and row["batch_index"] == 0
        assert row["line_count"] == 1 and row["translated_lines"] == 0
        assert client.get("/health").json()["recognition"] is None
        assert client.get(f"/recognition/batches/{batch['id']}").json()["exported"] == 2


def test_batch_never_overwrites_a_user_srt_with_the_same_name(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    source = wav(tmp_path / "访谈.wav", seconds=2)
    existing = tmp_path / "访谈.srt"
    existing.write_text("用户自己改过的字幕", encoding="utf-8")
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        response = client.post("/recognition/batches", json={"paths": [str(source)], "format": "srt+txt"})
        state = wait_batch(client, response.json()["id"])
        item = state["items"][0]
        assert item["status"] == "done"
        assert existing.read_text(encoding="utf-8") == "用户自己改过的字幕"
        names = [Path(value).name for value in item["exported"]]
        assert names == ["访谈.识别字幕.srt", "访谈.txt"]
        assert "下载进度" in (tmp_path / "访谈.识别字幕.srt").read_text(encoding="utf-8")
        # Running the same batch again reuses the files instead of making (2) copies.
        again = client.post("/recognition/batches", json={"paths": [str(source)], "format": "srt+txt"})
        state = wait_batch(client, again.json()["id"])
        assert [Path(value).name for value in state["items"][0]["exported"]] == names
        assert existing.read_text(encoding="utf-8") == "用户自己改过的字幕"


def test_batch_keeps_going_after_one_unreadable_file(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    good = wav(tmp_path / "好.wav", seconds=1)
    broken = tmp_path / "坏.wav"
    broken.write_bytes(b"not a wave file")
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        response = client.post("/recognition/batches", json={"paths": [str(broken), str(good)]})
        assert response.status_code == 201
        state = wait_batch(client, response.json()["id"])
        assert state["status"] == "done" and state["done"] == 1 and state["failed"] == 1
        assert state["items"][0]["status"] == "failed" and state["items"][0]["error"]
        assert state["items"][1]["status"] == "done" and state["items"][1]["exported"]


def test_batch_cancel_stops_current_and_remaining_files(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", SlowRecognition)
    first = wav(tmp_path / "一.wav", seconds=1)
    second = wav(tmp_path / "二.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        recognition = client.app.state.manager.recognition
        response = client.post("/recognition/batches", json={"paths": [str(first), str(second)]})
        batch = response.json()
        deadline = time.monotonic() + 10
        while not recognition.runner.started.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert recognition.runner.started.is_set(), "the first file should be running"
        assert client.post(f"/recognition/batches/{batch['id']}/cancel").json()["status"] in ("running", "cancelling")
        state = wait_batch(client, batch["id"])
        assert state["status"] == "cancelled"
        assert [item["status"] for item in state["items"]] == ["cancelled", "cancelled"]
        assert state["items"][0]["error"] and state["items"][1]["error"]
        assert not (tmp_path / "一.srt").exists() and not (tmp_path / "二.srt").exists()
        assert not client.app.state.manager.editing_busy


def test_batch_and_single_import_are_mutually_exclusive(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", SlowRecognition)
    first = wav(tmp_path / "甲.wav", seconds=1)
    second = wav(tmp_path / "乙.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        manager = client.app.state.manager
        recognition = manager.recognition
        response = client.post("/recognition/batches", json={"paths": [str(first), str(second)]})
        batch = response.json()
        deadline = time.monotonic() + 10
        while not recognition.runner.started.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        rejected = client.post("/recognition/sessions", json={"path": str(second)})
        assert rejected.status_code == 400 and "批量识别" in rejected.text
        assert client.post(f"/recognition/batches/{batch['id']}/retry").status_code == 400
        client.post(f"/recognition/batches/{batch['id']}/cancel")
        wait_batch(client, batch["id"])
        # The engine reservation is released once the queue is stopped.
        assert not manager.editing_busy


def test_batch_needs_an_asr_model_and_valid_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    source = wav(tmp_path / "原文.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client, families=("qwen3_forced_aligner",))
        response = client.post("/recognition/batches", json={"paths": [str(source)]})
        assert response.status_code == 400 and "ASR" in response.text
        assert client.get("/recognition/batches").json() == []
        setup_engine(tmp_path, client)
        assert client.post("/recognition/batches", json={"paths": [], "format": "srt"}).status_code == 422
        assert client.post("/recognition/batches", json={"paths": [str(source)], "format": "lrc"}).status_code == 422
        assert client.post("/recognition/batches", json={"paths": [str(source)], "unknown": 1}).status_code == 422
        assert client.post("/recognition/batches", json={"paths": [str(tmp_path / "missing.wav")]}).status_code == 400
        document = tmp_path / "note.txt"
        document.write_text("不是音频", encoding="utf-8")
        assert client.post("/recognition/batches", json={"paths": [str(document)]}).status_code == 400


def test_interrupted_batch_is_recoverable_after_restart(tmp_path):
    folder = tmp_path / "recognition"
    folder.mkdir()
    store = RecognitionStore(folder)
    state = {"name": "批量识别 · 2 个文件", "status": "running", "language": "zh", "format": "srt",
             "current": 0, "error": "", "items": [
                 {"path": str(tmp_path / "a.wav"), "name": "a", "status": "running", "session_id": "s1",
                  "exported": [], "export_error": ""},
                 {"path": str(tmp_path / "b.wav"), "name": "b", "status": "pending", "session_id": None,
                  "exported": [], "export_error": ""}]}
    identity = store.create_batch(state)
    store.close()

    reopened = RecognitionStore(folder)
    recovered = reopened.get_batch(identity)["state"]
    assert recovered["status"] == "interrupted" and "中断" in recovered["error"]
    assert [item["status"] for item in recovered["items"]] == ["pending", "pending"]
    assert recovered["items"][0]["session_id"] is None
    reopened.close()


def test_store_prunes_unreferenced_audio_at_startup(tmp_path):
    folder = tmp_path / "recognition"
    folder.mkdir()
    referenced = "a" * 32 + ".wav"
    orphan = "b" * 32 + ".wav"
    store = RecognitionStore(folder)
    store.create({"audio": referenced, "name": "保留", "status": "done"})
    (folder / referenced).write_bytes(b"kept")
    (folder / orphan).write_bytes(b"orphan")
    store.close()

    reopened = RecognitionStore(folder)
    assert (folder / referenced).is_file()
    assert not (folder / orphan).exists()
    reopened.close()


def test_batch_keeps_one_engine_until_the_queue_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    first = wav(tmp_path / "一.wav", seconds=1)
    second = wav(tmp_path / "二.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        manager = client.app.state.manager
        recognition = manager.recognition
        batch = client.post("/recognition/batches", json={"paths": [str(first), str(second)]}).json()
        state = wait_batch(client, batch["id"])
        assert state["status"] == "done" and state["done"] == 2
        deadline = time.monotonic() + 5
        while manager.editing_busy and time.monotonic() < deadline:
            time.sleep(0.02)
        assert recognition.runner.interrupts == 1
        assert len(recognition.runner.calls) == 2


def test_batch_retry_reuses_the_audio_copy_instead_of_importing_again(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FailOnceRecognition)
    source = wav(tmp_path / "访谈.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        recognition = client.app.state.manager.recognition
        batch = client.post("/recognition/batches", json={"paths": [str(source)]}).json()
        state = wait_batch(client, batch["id"])
        item = state["items"][0]
        assert item["status"] == "failed" and item["session_id"]
        session_id = item["session_id"]
        audio_folder = recognition.root
        assert len(list(audio_folder.glob("*.wav"))) == 1

        response = client.post(f"/recognition/batches/{batch['id']}/retry")
        assert response.status_code == 200
        state = wait_batch(client, batch["id"])
        assert state["items"][0]["status"] == "done"
        assert state["items"][0]["session_id"] == session_id
        assert len(list(audio_folder.glob("*.wav"))) == 1


def test_removing_batch_deletes_child_sessions_and_audio_copies(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    source = wav(tmp_path / "一段.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        recognition = client.app.state.manager.recognition
        batch = client.post("/recognition/batches", json={"paths": [str(source)]}).json()
        state = wait_batch(client, batch["id"])
        session_id = state["items"][0]["session_id"]
        session = client.get(f"/recognition/sessions/{session_id}").json()
        audio = recognition.root / session["state"]["audio"]
        assert audio.is_file()

        assert client.delete(f"/recognition/batches/{batch['id']}").status_code == 200
        assert client.get(f"/recognition/sessions/{session_id}").status_code == 404
        assert not audio.exists()
        assert all(row["id"] != session_id for row in client.get("/recognition/sessions").json())
        assert client.get(f"/recognition/batches/{batch['id']}").status_code == 404


class LoopRecognition(FakeRecognition):
    """Every request returns a decoder loop; the aligner faithfully places each oh."""

    text = "Before. " + "oh, " * 40 + "After."

    def transcribe(self, voices, audio, language, cancelled):
        self.started.set()
        return {"text": self.text, "language": "English"}

    def align(self, voices, audio, text, language, cancelled):
        words = text.split()
        return {"words": [{"word": word, "start_sample": index * 1600,
                           "end_sample": (index + 1) * 1600}
                          for index, word in enumerate(words)]}


def test_batch_dedupe_folds_a_decoder_loop(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", LoopRecognition)
    source = wav(tmp_path / "重复.wav", seconds=6)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        batch = client.post("/recognition/batches", json={"paths": [str(source)]}).json()
        assert batch["dedupe"] is True
        state = wait_batch(client, batch["id"])
        item = state["items"][0]
        assert item["status"] == "done" and item["exported"]
        subtitle = Path(item["exported"][0]).read_text(encoding="utf-8")
        assert subtitle.lower().count("oh") == 1
        assert "Before." in subtitle and "After." in subtitle
        session = client.get(f"/recognition/sessions/{item['session_id']}").json()
        assert session["state"]["dedupe"] is True
        assert session["state"]["dedupe_removed"] == 39


def test_batch_dedupe_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", LoopRecognition)
    source = wav(tmp_path / "原样.wav", seconds=6)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        batch = client.post("/recognition/batches", json={"paths": [str(source)],
                                                          "dedupe": False}).json()
        assert batch["dedupe"] is False
        state = wait_batch(client, batch["id"])
        subtitle = Path(state["items"][0]["exported"][0]).read_text(encoding="utf-8")
        assert subtitle.lower().count("oh") >= 12
        session = client.get(f"/recognition/sessions/{state['items'][0]['session_id']}").json()
        assert session["state"]["dedupe"] is False
        assert session["state"]["dedupe_removed"] == 0


def test_batch_poll_reads_cached_line_counts_while_running(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", SlowRecognition)
    first = wav(tmp_path / "一.wav", seconds=1)
    second = wav(tmp_path / "二.wav", seconds=1)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        recognition = client.app.state.manager.recognition
        calls = []
        monkeypatch.setattr(recognition.store, "translation_summary",
                            lambda identities: calls.append(list(identities)) or {})
        batch = client.post("/recognition/batches", json={"paths": [str(first), str(second)]}).json()
        deadline = time.monotonic() + 10
        while not recognition.runner.started.is_set() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert recognition.runner.started.is_set()
        running = client.get(f"/recognition/batches/{batch['id']}").json()
        assert running["status"] == "running"
        assert calls == []
        assert running["items"][0]["line_count"] == 0

        recognition.runner.release.set()
        state = wait_batch(client, batch["id"])
        assert state["status"] == "done" and len(calls) == 1
