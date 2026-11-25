"""Agent 翻译：术语注入、记忆复用、失败隔离与受控工具调用。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx
from fastapi.testclient import TestClient

from voice_workbench.api import create_app
from test_speech_recognition import FakeRecognition, setup_engine, wait_session
from test_audio_editing import wav


def recognize_once(tmp_path, client):
    source = wav(tmp_path / "访谈.wav", seconds=2)
    return wait_session(client, client.post("/recognition/sessions", json={"path": str(source),
                                                                           "language": "zh"}).json()["id"])


def configure(client):
    return client.post("/translation/settings", json={
        "provider": "api", "base_url": "https://api.example.com/v1", "model_name": "demo", "target": "en"})


def test_agent_uses_terms_then_memory_and_records_trace(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    calls = {"count": 0, "prompts": []}

    def handler(request):
        calls["count"] += 1
        calls["prompts"].append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": '["Download progress refreshes."]'}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.service.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        configure(client)
        term = client.post("/translation/terms", json={
            "source": "下载", "target": "Download", "source_lang": "zh", "target_lang": "en"}).json()
        assert term["target"] == "Download"
        result = client.post("/translation/agent/translate",
                             json={"session_id": session["id"], "target": "en"}).json()
        translation, run = result["translation"], result["run"]
        assert translation["lines"][0]["translated"] == "Download progress refreshes."
        assert translation["agent_run_id"] == run["id"] and translation["agent"]["terms"] == 1
        assert "Download" in calls["prompts"][0]
        assert {"search_terms", "search_memory", "translate", "review", "remember"} <= {
            step["tool"] for step in run["trace"]}
        stored = client.get(f"/recognition/sessions/{session['id']}").json()["state"]
        assert stored["translation"]["lines"][0]["translated"] == "Download progress refreshes."
        memory = client.get("/translation/memory", params={"target": "en"}).json()
        assert len(memory) == 1 and memory[0]["source"] == "下载进度刷新，保留滚动位置。"
        # A second run reuses memory and makes no model request.
        second = client.post("/translation/agent/translate",
                             json={"session_id": session["id"], "target": "en"}).json()
        assert second["translation"]["lines"][0]["translated"] == "Download progress refreshes."
        assert calls["count"] == 1
        assert second["translation"]["agent"]["memory_hits"] == 1


def test_agent_chat_can_correct_a_subtitle_line(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "correct_lines",
                    "arguments": json.dumps({"corrections": [
                        {"index": 1, "text": "Bambi 回家。", "reason": "Nambi 是识别错字"}]})}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "已修正第 1 行：Nambi → Bambi。"}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.agent.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        identity = session["id"]
        client.patch(f"/recognition/sessions/{identity}/lines", json={
            "corrections": [{"index": 1, "text": "Nambi 回家。"}]})
        configure(client)
        result = client.post("/translation/agent/chat", json={
            "message": "检查字幕，把识别错的 Nambi 改成 Bambi", "session_id": identity,
            "target": "en"}).json()
        assert "Bambi" in result["reply"]
        assert any(step["tool"] == "correct_lines" and step["status"] == "ok"
                   for step in result["trace"])
        state = client.get(f"/recognition/sessions/{identity}").json()["state"]
        assert state["lines"][0]["text"] == "Bambi 回家。"
        assert state["text"] == "Bambi 回家。"
        assert state["correction_log"][-1]["changes"][0]["old"] == "Nambi 回家。"


def test_agent_chat_can_update_a_memory_through_a_tool_call(tmp_path, monkeypatch):
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "update_memory",
                    "arguments": json.dumps({"id": memory["id"], "target": "Download finished."})}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "已更新这条记忆的译文。"}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.agent.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        configure(client)
        memory = client.post("/translation/memory", json={
            "source": "下载完成", "target": "Download complete", "target_lang": "en"}).json()
        result = client.post("/translation/agent/chat", json={
            "message": "把“下载完成”的记忆译文改成 Download finished。", "target": "en"}).json()
        assert "已更新" in result["reply"]
        assert any(step["tool"] == "update_memory" and step["status"] == "ok"
                   for step in result["trace"])
        stored = client.get("/translation/memory", params={"query": "下载完成"}).json()
        assert stored[0]["target"] == "Download finished."


def test_agent_does_not_clobber_a_session_when_the_model_breaks_line_count(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": '["a", "b"]'}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.service.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        configure(client)
        response = client.post("/translation/agent/translate",
                               json={"session_id": session["id"], "target": "en"})
        assert response.status_code == 400
        assert "行" in response.text
        assert "translation" not in client.get(f"/recognition/sessions/{session['id']}").json()["state"]
        runs = client.get("/translation/agent/runs", params={"session_id": session["id"]}).json()
        assert runs and runs[0]["status"] == "failed" and runs[0]["error"]


def test_agent_chat_can_add_a_term_through_a_tool_call(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "add_term",
                    "arguments": json.dumps({"source": "声笺", "target": "Shengjian", "target_lang": "en"})}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                  "content": "已加入术语。"}}]})

    original = httpx.Client
    monkeypatch.setattr("voice_workbench.translation.agent.httpx.Client",
                        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    with TestClient(create_app(tmp_path / "data")) as client:
        configure(client)
        result = client.post("/translation/agent/chat", json={
            "message": "把“声笺”加入术语库，译作 Shengjian", "target": "en"}).json()
        assert result["reply"] == "已加入术语。" and result["run"]["status"] == "done"
        assert result["trace"][0]["tool"] == "add_term" and result["trace"][0]["status"] == "ok"
        terms = client.get("/translation/terms", params={"query": "声笺"}).json()
        assert terms and terms[0]["target"] == "Shengjian"


def test_term_and_memory_crud_routes_validate_and_soft_delete(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as client:
        assert client.post("/translation/terms", json={"source": "", "target": "x"}).status_code == 422
        term = client.post("/translation/terms", json={
            "source": "声笺", "target": "Shengjian", "target_lang": "en"}).json()
        assert client.patch(f"/translation/terms/{term['id']}", json={"enabled": False}).json()["enabled"] is False
        assert client.get("/translation/terms").json() == []
        assert client.delete("/translation/terms/missing").status_code == 404
        memory = client.post("/translation/memory", json={"source": "甲", "target": "A", "target_lang": "en"}).json()
        assert memory["target"] == "A"
        assert client.get("/translation/memory", params={"query": "A"}).json()[0]["id"] == memory["id"]
        assert client.delete(f"/translation/memory/{memory['id']}").json()["enabled"] is False
        assert client.delete("/translation/memory/missing").status_code == 404


def test_translation_lists_support_limit_and_offset(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as client:
        first_term = client.post("/translation/terms", json={
            "source": "保留说明", "target": "Keep note", "target_lang": "en",
            "note": "原说明", "priority": 60}).json()
        again = client.post("/translation/terms", json={
            "source": "保留说明", "target": "Keep note 2", "target_lang": "en",
            "note": "", "priority": 0, "preserve_metadata": True}).json()
        assert again["id"] == first_term["id"] and again["target"] == "Keep note 2"
        assert again["note"] == "原说明" and again["priority"] == 60
        memory = client.post("/translation/memory", json={
            "source": "保留来源", "target": "Keep origin", "target_lang": "en",
            "session_id": "session-1", "provider": "api"}).json()
        memory_again = client.post("/translation/memory", json={
            "source": "保留来源", "target": "Keep origin 2", "target_lang": "en",
            "preserve_metadata": True}).json()
        assert memory_again["id"] == memory["id"] and memory_again["target"] == "Keep origin 2"
        assert memory_again["session_id"] == "session-1" and memory_again["provider"] == "api"
        updated_memory = client.patch(f"/translation/memory/{memory['id']}", json={
            "target": "Keep origin 3", "quality": 2}).json()
        assert updated_memory["target"] == "Keep origin 3" and updated_memory["quality"] == 2
        assert updated_memory["session_id"] == "session-1"
        for index in range(5):
            client.post("/translation/terms", json={
                "source": f"原文{index}", "target": f"译文{index}", "target_lang": "en"})
            client.post("/translation/memory", json={
                "source": f"句子{index}", "target": f"Sentence {index}", "target_lang": "en"})
        first = client.get("/translation/terms", params={"limit": 2, "offset": 0}).json()
        second = client.get("/translation/terms", params={"limit": 2, "offset": 2}).json()
        assert len(first) == len(second) == 2
        assert not {item["id"] for item in first} & {item["id"] for item in second}
        assert len(client.get("/translation/memory", params={"limit": 5}).json()) == 5
        first = client.get("/translation/memory", params={"limit": 2, "offset": 3}).json()
        assert len(first) == 2


def test_external_agent_can_apply_a_complete_translation(tmp_path, monkeypatch):
    monkeypatch.setattr("voice_workbench.speech_recognition.service.RecognitionRunner", FakeRecognition)
    with TestClient(create_app(tmp_path / "data")) as client:
        setup_engine(tmp_path, client)
        session = recognize_once(tmp_path, client)
        state = client.get(f"/recognition/sessions/{session['id']}").json()["state"]
        source_lines = state["lines"]
        payload = {"target": "en", "provider": "workbuddy", "model": "buddy",
                   "lines": [{"index": line["index"], "translated": f"Line {line['index']}"}
                             for line in source_lines]}
        result = client.post(f"/translation/sessions/{session['id']}/apply", json=payload)
        assert result.status_code == 200, result.text
        translation = result.json()["translation"]
        assert translation["provider"] == "workbuddy"
        assert translation["agent"]["external"] is True
        assert [line["translated"] for line in translation["lines"]] == [
            f"Line {line['index']}" for line in source_lines]
        assert [line["text"] for line in translation["lines"]] == [line["text"] for line in source_lines]
        assert translation["agent"]["memory_written"] >= 1
        memories = client.get("/translation/memory", params={"target": "en"}).json()
        assert any(item["session_id"] == session["id"] for item in memories)
        stored = client.get(f"/recognition/sessions/{session['id']}").json()["state"]
        assert stored["translation"]["lines"][0]["start"] == source_lines[0]["start"]
        runs = client.get("/translation/agent/runs", params={"session_id": session["id"]}).json()
        assert runs and runs[0]["provider"] == "workbuddy" and runs[0]["status"] == "done"

        # Extra or duplicated line numbers are rejected before the old translation is overwritten.
        bad = client.post(f"/translation/sessions/{session['id']}/apply",
                          json={"target": "en", "provider": "workbuddy",
                                "lines": payload["lines"] + [{"index": 999, "translated": "extra"}]})
        assert bad.status_code == 400 and "多出行" in bad.text, bad.text
        duplicate = client.post(f"/translation/sessions/{session['id']}/apply",
                                json={"target": "en", "provider": "workbuddy",
                                      "lines": [payload["lines"][0], payload["lines"][0]]})
        assert duplicate.status_code == 400 and "重复" in duplicate.text
        assert client.get(f"/recognition/sessions/{session['id']}").json()["state"]["translation"][
            "lines"][0]["translated"] == "Line " + str(source_lines[0]["index"])
