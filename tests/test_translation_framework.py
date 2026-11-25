"""翻译框架：设置、OpenAI 兼容调用、本地模型占位与错误边界。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from voice_workbench.translation.service import endpoint, parse_array
from test_speech_recognition import FakeRecognition, setup_engine, wait_session
from test_audio_editing import wav


def recognize_once(tmp_path, client):
    source = wav(tmp_path / "访谈.wav", seconds=2)
    session = wait_session(client, client.post("/recognition/sessions", json={"path": str(source),
                                                                              "language": "zh"}).json()["id"])
    assert session["state"]["status"] == "done"
    return session


def test_translation_settings_keep_the_key_private(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    with TestClient(create_app(tmp_path / "data")) as client:
        options = client.get("/translation/options").json()
        assert {item["id"] for item in options["providers"]} == {"api", "local"}
        assert options["settings"]["target"] == "en" and not options["settings"]["has_key"]
        saved = client.post("/translation/settings", json={
            "provider": "api", "base_url": "https://api.example.com/v1", "model_name": "demo",
            "api_key": "sk-secret", "target": "ja"}).json()
        assert saved["has_key"] and saved["model"] == "demo" and saved["target"] == "ja"
        assert "api_key" not in saved and "sk-secret" not in json.dumps(client.get("/translation/options").json())
        # A null / omitted key leaves the saved secret in place.
        saved = client.post("/translation/settings", json={"model_name": "demo-2"}).json()
        assert saved["has_key"] and saved["model"] == "demo-2"
        assert client.post("/translation/settings", json={"target": "xx"}).status_code == 400
        assert client.post("/translation/settings", json={"target": "zh-CN"}).json()["target"] == "zh"
        assert client.post("/translation/settings", json={"unknown": 1}).status_code == 422


def test_openai_compatible_translation_keeps_the_original_lines(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '["The download progress refreshes."]'}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.service.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        client.post("/translation/settings", json={
            "provider": "api", "base_url": "https://api.example.com/v1", "model_name": "demo",
            "api_key": "sk-secret", "target": "en"})
        result = client.post(f"/recognition/sessions/{session['id']}/translation", json={"target": "en"})
        assert result.status_code == 200, result.text
        payload = result.json()
        assert payload["target"] == "en" and payload["provider"] == "api" and payload["lines"][0]["translated"] == "The download progress refreshes."
        assert payload["lines"][0]["text"] == session["state"]["lines"][0]["text"]
        assert seen["url"] == "https://api.example.com/v1/chat/completions"
        assert seen["auth"] == "Bearer sk-secret"
        assert seen["body"]["model"] == "demo" and seen["body"]["temperature"] == 0
        stored = client.get(f"/recognition/sessions/{session['id']}").json()["state"]
        assert stored["translation"]["lines"][0]["translated"] == "The download progress refreshes."
        assert stored["lines"] == session["state"]["lines"]


def test_external_translation_accepts_locale_target_codes(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        lines = [{"index": line["index"], "translated": "X"}
                 for line in session["state"]["lines"]]
        response = client.post(f"/translation/sessions/{session['id']}/apply", json={
            "target": "zh-CN", "provider": "check", "lines": lines})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["translation"]["target"] == "zh"
        memories = client.get("/translation/memory", params={"target": "zh-CN"}).json()
        assert memories and memories[0]["target_lang"] == "zh"
        assert client.get("/translation/memory", params={"target": "zh"}).json()[0]["target_lang"] == "zh"


def test_local_provider_is_an_explicit_framework_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        response = client.post(f"/recognition/sessions/{session['id']}/translation",
                               json={"provider": "local", "target": "en"})
        assert response.status_code == 400
        assert "预留" in response.text and "尚未接入" in response.text
        assert "translation" not in client.get(f"/recognition/sessions/{session['id']}").json()["state"]


def test_translation_rejects_a_wrong_line_count_without_clobbering_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '["a", "b"]'}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.service.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        client.post("/translation/settings", json={"provider": "api", "base_url": "https://api.example.com/v1",
                                                  "model_name": "demo"})
        response = client.post(f"/recognition/sessions/{session['id']}/translation", json={"target": "en"})
        assert response.status_code == 400 and "2 行" in response.text
        assert "translation" not in client.get(f"/recognition/sessions/{session['id']}").json()["state"]


def test_translation_payload_parsing_and_endpoint_normalisation():
    assert endpoint("https://api.openai.com/v1") == "https://api.openai.com/v1/chat/completions"
    assert endpoint("http://127.0.0.1:1234") == "http://127.0.0.1:1234/v1/chat/completions"
    assert endpoint("https://api.deepseek.com/chat/completions") == "https://api.deepseek.com/chat/completions"
    assert parse_array('```json\n["甲", "乙"]\n```') == ["甲", "乙"]
    assert parse_array('好的，结果如下：\n[{"translated": "甲"}, {"translated": "乙"}]') == ["甲", "乙"]
    assert parse_array('{"translations": ["甲"]}') == ["甲"]
