"""MCP 接口封装：协议握手、工具清单、文本切分与错误转译。

测试不联网、不加载模型：服务调用统一走 mcp_server._call，这里整体替换。
"""
import json
from pathlib import Path

import pytest

import voice_workbench.mcp_server as mcp


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    monkeypatch.setattr(mcp, "_ROOT", None)
    monkeypatch.setattr(mcp, "_CACHE", {"at": 0.0, "usable": [], "broken": []})


def test_plain_strips_markdown_but_keeps_words():
    assert mcp._plain("**你好**，这是*强调*。") == "你好，这是强调。"
    assert "链接" in mcp._plain("看[链接](https://x.com)")
    assert "print" not in mcp._plain("代码```python\nprint(1)\n```剩")


def test_split_adds_pause_between_list_lines():
    # 列表项本来没有标点，直接拼接会被连读成一句。
    assert mcp._split("# 标题\n- 第一项\n- 第二项") == ["标题，第一项，第二项"]


def test_split_respects_chunk_budget(monkeypatch):
    monkeypatch.setattr(mcp, "CHUNK_CHARS", 10)
    chunks = mcp._split("第一句话。第二句话。第三句话。")
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert "".join(chunks).replace("，", "") == "第一句话。第二句话。第三句话。"


def test_split_truncates_runaway_input(monkeypatch):
    monkeypatch.setattr(mcp, "MAX_CHUNKS", 2)
    monkeypatch.setattr(mcp, "CHUNK_CHARS", 3)
    assert len(mcp._split("测试。" * 20)) == 2


def test_initialize_echoes_client_protocol():
    response = mcp._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}})
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"]["tools"] == {"listChanged": False}


def test_notifications_get_no_response():
    assert mcp._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_every_tool():
    names = [tool["name"] for tool in mcp._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert names == ["list_voices", "speak", "speak_dialogue", "get_job", "transcribe"]


def test_unknown_tool_is_reported_to_the_model():
    response = mcp._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": "nope", "arguments": {}}})
    assert response["result"]["isError"] is True
    assert "list_voices" in response["result"]["content"][0]["text"]


def test_connection_error_becomes_readable_hint(monkeypatch):
    import httpx

    def boom(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.Client, "request", boom)
    with pytest.raises(mcp.ToolError) as error:
        mcp._call("GET", "/health")
    assert "没有运行" in str(error.value)


def test_missing_voice_explains_why_it_is_unusable(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: [
        {"id": "v1", "name": "御姐", "error": {"message": "文件不存在：G:\\a.wav", "hint": "重新选择文件。"}},
        {"id": "model-1", "name": "默认模型设置", "is_template": True, "error": None},
    ])
    with pytest.raises(mcp.ToolError) as error:
        mcp.tool_speak({"text": "你好"})
    message = str(error.value)
    assert "御姐" in message and "G:\\a.wav" in message
    assert "新建" not in message


def test_speak_returns_absolute_paths_and_cleans_text(monkeypatch):
    root = Path("C:/tmp/data")
    submitted = {}

    def fake(method, path, **kwargs):
        if path == "/voices":
            return [{"id": "v1", "name": "御姐", "backend": "audio_cpp"}]
        if path == "/health":
            return {"data_dir": str(root)}
        if method == "POST":
            submitted["segments"] = kwargs["json"]["segments"]
            return {"job_id": "j1", "generation": 1}
        count = len(submitted["segments"])
        return {"id": "j1", "status": "done", "segments": [
            {"idx": i, "status": "done", "audio": "s%d.wav" % i, "duration": 1.5} for i in range(count)]}

    monkeypatch.setattr(mcp, "_call", fake)
    monkeypatch.setattr(mcp, "CHUNK_CHARS", 5)
    result = mcp.tool_speak({"text": "**第一句**。第二句。"})

    assert submitted["segments"][0]["text"] == "第一句。"
    assert result["audio_paths"] == [str(root / "audio" / "s0.wav"), str(root / "audio" / "s1.wav")]
    assert result["duration_seconds"] == 3.0
    assert result["voice"] == "御姐"


def test_failed_segment_surfaces_engine_message(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: (
        [{"id": "v1", "name": "御姐"}] if path == "/voices"
        else {"data_dir": "C:/tmp/data"} if path == "/health"
        else {"job_id": "j1", "generation": 1} if method == "POST"
        else {"id": "j1", "status": "failed", "segments": [
            {"idx": 2, "status": "failed", "error": {"message": "模型文件缺失"}}]}))
    with pytest.raises(mcp.ToolError) as error:
        mcp.tool_speak({"text": "你好"})
    assert "第 2 段" in str(error.value) and "模型文件缺失" in str(error.value)


def test_dialogue_groups_paths_by_speaker(monkeypatch):
    voices = [{"id": "a", "name": "姐姐"}, {"id": "b", "name": "妹妹"}]
    submitted = {}

    def fake(method, path, **kwargs):
        if path == "/voices":
            return voices
        if path == "/health":
            return {"data_dir": "C:/tmp/data"}
        if method == "POST":
            submitted["segments"] = kwargs["json"]["segments"]
            return {"job_id": "j1", "generation": 1}
        count = len(submitted["segments"])
        return {"id": "j1", "status": "done", "segments": [
            {"idx": i, "status": "done", "audio": "s%d.wav" % i, "duration": 1.0} for i in range(count)]}

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_speak_dialogue({"lines": [
        {"speaker": "姐姐", "text": "你回来了。"},
        {"speaker": "妹妹", "text": "嗯。"},
    ]})

    assert [segment["voice_id"] for segment in submitted["segments"]] == ["a", "b"]
    assert [line["speaker"] for line in result["lines"]] == ["姐姐", "妹妹"]
    assert "audio_path" not in result


def test_get_job_maps_audio_to_local_path(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: (
        {"data_dir": "C:/tmp/data"} if path == "/health"
        else {"id": "j1", "status": "running", "segments": [
            {"idx": 0, "status": "done", "audio": "s0.wav", "error": None},
            {"idx": 1, "status": "running", "audio": None, "error": None}]}))
    result = mcp.tool_get_job({"job_id": "j1"})
    assert result["status"] == "running"
    assert result["segments"][0]["audio_path"] == str(Path("C:/tmp/data") / "audio" / "s0.wav")
    assert result["segments"][1]["audio_path"] is None
