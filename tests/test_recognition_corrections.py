"""字幕行修正的存储、接口与译文失效行为。"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from test_audio_editing import wav
from test_speech_recognition import FakeRecognition, setup_engine, wait_session


def test_line_correction_changes_text_but_not_timing_and_clears_stale_translation(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        source = wav(tmp_path / "访谈.wav", seconds=2)
        session = wait_session(client, client.post(
            "/recognition/sessions", json={"path": str(source), "language": "zh"}).json()["id"])
        identity = session["id"]
        state = session["state"]
        line = state["lines"][0]
        old_start, old_end = line["start"], line["end"]
        manager = client.app.state.manager
        manager.recognition.store.update(identity, {
            "translation": {"target": "en", "lines": [{"index": line["index"], "translated": "Old"}]}})

        response = client.patch(f"/recognition/sessions/{identity}/lines", json={
            "corrections": [{"index": line["index"], "text": "Bambi 回家。", "reason": "Nambi 是识别错字"}]})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["corrected"] == [{"index": line["index"],
                                        "old": "下载进度刷新，保留滚动位置。",
                                        "text": "Bambi 回家。",
                                        "reason": "Nambi 是识别错字"}]
        assert result["translation_cleared"] is True

        updated = client.get(f"/recognition/sessions/{identity}").json()["state"]
        assert updated["lines"][0]["text"] == "Bambi 回家。"
        assert updated["lines"][0]["start"] == old_start and updated["lines"][0]["end"] == old_end
        assert updated["text"] == "Bambi 回家。"
        assert updated["translation"] is None
        assert updated["correction_log"][-1]["changes"][0]["old"] == "下载进度刷新，保留滚动位置。"


def test_line_correction_rejects_unknown_index_and_empty_text(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        source = wav(tmp_path / "访谈.wav", seconds=2)
        identity = wait_session(client, client.post(
            "/recognition/sessions", json={"path": str(source), "language": "zh"}).json()["id"])["id"]
        unknown = client.patch(f"/recognition/sessions/{identity}/lines", json={
            "corrections": [{"index": 999, "text": "不存在"}]})
        assert unknown.status_code == 400
        empty = client.patch(f"/recognition/sessions/{identity}/lines", json={
            "corrections": [{"index": 1, "text": "   "}]})
        assert empty.status_code in (400, 422)
