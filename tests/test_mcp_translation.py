"""MCP 翻译工具：只做 HTTP 契约映射，不启动服务、不碰 SQLite。"""
from __future__ import annotations

import pytest

import voice_workbench.mcp_server as mcp


def test_get_subtitle_compacts_and_pages(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: {
        "id": "s1",
        "state": {
            "name": "demo", "status": "done", "recognized_language": "zh", "duration": 3.2,
            "lines": [
                {"index": 0, "start": 0.0, "end": 1.2, "text": "下载进度刷新。"},
                {"index": 1, "start": 1.2, "end": 3.2, "text": "保留滚动位置。"},
                {"index": 2, "start": 3.2, "end": 4.0, "text": "结尾。"},
            ],
            "translation": {"lines": [{"index": 0, "translated": "Download refreshes."}],
                            "agent": {"terms": 1, "memory_hits": 0}},
        },
    })
    first = mcp.tool_get_subtitle({"session_id": "s1", "limit": 2})
    assert first["session_id"] == "s1"
    assert first["line_count"] == 3 and first["translated_lines"] == 1
    assert first["columns"] == ["i", "text"]
    assert first["lines"] == [[0, "下载进度刷新。"], [1, "保留滚动位置。"]]
    assert first["cursor"] == 0 and first["next_cursor"] == 2 and first["done"] is False
    assert "translation" not in first and "translation_meta" not in first

    second = mcp.tool_get_subtitle({"session_id": "s1", "cursor": 2, "limit": 2})
    assert second["lines"] == [[2, "结尾。"]]
    assert second["next_cursor"] is None and second["done"] is True

    timecoded = mcp.tool_get_subtitle({"session_id": "s1", "limit": 1, "include_timecodes": True})
    assert timecoded["columns"] == ["i", "start", "end", "text"]
    assert timecoded["lines"] == [[0, 0.0, 1.2, "下载进度刷新。"]]


def test_correct_subtitle_maps_to_the_line_correction_route(monkeypatch):
    seen = {}

    def fake(method, path, **kwargs):
        seen.update({"method": method, "path": path, "json": kwargs.get("json")})
        return {"session_id": "s1", "corrected": [{"index": 1, "text": "Bambi"}],
                "translation_cleared": True}

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_correct_subtitle({
        "session_id": "s1",
        "corrections": [{"index": 1, "text": "  Bambi  ", "reason": "Nambi 是错字"}]})
    assert seen["method"] == "PATCH"
    assert seen["path"] == "/recognition/sessions/s1/lines"
    assert seen["json"] == {"corrections": [{"index": 1, "text": "Bambi", "reason": "Nambi 是错字"}]}
    assert result["translation_cleared"] is True


def test_update_translation_memory_maps_to_patch(monkeypatch):
    seen = {}

    def fake(method, path, **kwargs):
        seen.update({"method": method, "path": path, "json": kwargs.get("json")})
        return {"id": "m1", "target": "Download finished."}

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_update_translation_memory({
        "memory_id": "m1", "translation": "Download finished.", "quality": 2})
    assert seen["method"] == "PATCH" and seen["path"] == "/translation/memory/m1"
    assert seen["json"] == {"target": "Download finished.", "quality": 2}
    assert result["memory"]["id"] == "m1"


def test_get_subtitle_objects_mode_keeps_translation_for_old_clients(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: {
        "id": "s1",
        "state": {
            "name": "demo", "status": "done", "recognized_language": "zh", "duration": 3.2,
            "lines": [
                {"index": 0, "start": 0.0, "end": 1.2, "text": "下载进度刷新。"},
                {"index": 1, "start": 1.2, "end": 3.2, "text": "保留滚动位置。"},
            ],
            "translation": {"lines": [{"index": 0, "translated": "Download refreshes."}],
                            "agent": {"terms": 1, "memory_hits": 0}},
        },
    })
    result = mcp.tool_get_subtitle({"session_id": "s1", "format": "objects"})
    assert result["translation"]["agent"]["terms"] == 1
    assert result["lines"][0]["translated"] == "Download refreshes."
    assert result["lines"][1]["translated"] is None


def test_get_subtitle_known_revision_returns_only_unchanged(monkeypatch):
    lines = [{"index": 0, "start": 0.0, "end": 1.0, "text": "甲"}]
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: {
        "id": "s1", "state": {"lines": lines, "translation": {"lines": []}}})

    fresh = mcp.tool_get_subtitle({"session_id": "s1"})
    revision = fresh["revision"]
    again = mcp.tool_get_subtitle({"session_id": "s1", "known_revision": revision})
    assert again["unchanged"] is True and again["revision"] == revision
    assert "lines" not in again and again["line_count"] == 1



def test_add_term_maps_translation_to_target(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update({"method": method, "path": path, "json": kw.get("json")})
        return {"id": "t1", "source": "下载", "target": "Download"}

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_add_translation_term({
        "source": "下载", "translation": "Download", "source_lang": "zh",
        "target_lang": "en", "note": "按钮", "priority": 20,
    })
    assert result["term"]["id"] == "t1"
    assert seen["method"] == "POST" and seen["path"] == "/translation/terms"
    assert seen["json"]["target"] == "Download" and seen["json"]["target_lang"] == "en"
    assert seen["json"]["priority"] == 20


def test_update_and_disable_term_keep_paths_explicit(monkeypatch):
    calls = []

    def fake(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return {"id": "t1"}

    monkeypatch.setattr(mcp, "_call", fake)
    mcp.tool_update_translation_term({"term_id": "t1", "translation": "Download", "enabled": False})
    mcp.tool_disable_translation_term({"term_id": "t1"})
    assert calls[0][0] == "PATCH" and calls[0][1] == "/translation/terms/t1"
    assert calls[0][2] == {"target": "Download", "enabled": False}
    assert calls[1][0] == "DELETE" and calls[1][1] == "/translation/terms/t1"


def test_list_terms_maps_target_and_enabled_filter(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update({"method": method, "path": path, "params": kw.get("params")})
        return [{"id": "t1"}]

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_list_translation_terms({"target_lang": "en", "query": "下载", "enabled_only": False})
    assert result["terms"] == [{"id": "t1"}]
    assert seen["params"] == {"target": "en", "query": "下载", "enabled_only": False}


def test_translate_subtitle_uses_agent_endpoint_and_long_timeout(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update({"method": method, "path": path, "json": kw.get("json"), "timeout": kw.get("timeout")})
        return {"translation": {"lines": []}, "run": {"id": "r1", "status": "done"}}

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_translate_subtitle({
        "session_id": "s1", "target_lang": "en", "provider": "api", "use_memory": False,
    })
    assert result["run_id"] == "r1" and result["status"] == "done"
    assert "translation" not in result and "run" not in result
    assert seen["method"] == "POST" and seen["path"] == "/translation/agent/translate"
    assert seen["json"] == {"session_id": "s1", "target": "en", "provider": "api", "use_memory": False}
    assert seen["timeout"] == 600.0


def test_list_sessions_caps_the_result(monkeypatch):
    records = [{"id": str(i), "name": "文件" + str(i), "status": "done", "line_count": 3,
                "translated_lines": 0, "batch_id": None, "batch_index": None, "target": ""}
               for i in range(60)]
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: records)
    result = mcp.tool_list_translation_sessions({"limit": 2})
    assert result["count"] == 2 and result["total"] == 60 and result["has_more"] is True
    assert result["sessions"][0]["id"] == "0" and result["sessions"][0]["line_count"] == 3


def test_list_sessions_defaults_to_twenty(monkeypatch):
    records = [{"id": str(i)} for i in range(60)]
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: records)
    result = mcp.tool_list_translation_sessions({})
    assert result["count"] == 20 and result["has_more"] is True


def test_list_sessions_filters_batch_and_exposes_batch_metadata(monkeypatch):
    records = [
        {"id": "s1", "batch_id": "b1", "batch_index": 0, "status": "done", "line_count": 3,
         "translated_lines": 0, "name": "一"},
        {"id": "s2", "batch_id": "b1", "batch_index": 1, "status": "failed", "line_count": 0,
         "translated_lines": 0, "name": "二"},
        {"id": "s3", "batch_id": None, "batch_index": None, "status": "done", "line_count": 2,
         "translated_lines": 2, "name": "单条"},
    ]
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: records)
    result = mcp.tool_list_translation_sessions({"batch_id": "b1", "status": "done"})
    assert result["batch_id"] == "b1" and result["count"] == 1
    assert result["sessions"][0]["line_count"] == 3
    assert result["sessions"][0]["translated_lines"] == 0


def test_get_subtitle_reports_batch_membership(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda method, path, **kw: {
        "id": "s1",
        "state": {"name": "批量文件", "status": "done", "batch_id": "b1", "batch_index": 2,
                  "lines": [{"index": 0, "start": 0.0, "end": 1.0, "text": "甲"}],
                  "translation": {"lines": []}},
    })
    result = mcp.tool_get_subtitle({"session_id": "s1"})
    assert result["batch_id"] == "b1" and result["batch_index"] == 2
    assert result["line_count"] == 1 and result["translated_lines"] == 0
    assert result["lines"] == [[0, "甲"]]


def test_save_translation_requires_a_target(monkeypatch):
    monkeypatch.setattr(mcp, "_call", lambda *args, **kw: pytest.fail("缺目标语言时不应调用服务"))
    with pytest.raises(mcp.ToolError) as error:
        mcp.tool_save_translation({"session_id": "s1", "lines": [{"index": 0, "translated": "x"}]})
    assert "target_lang" in str(error.value)


def test_save_translation_maps_external_agent_lines(monkeypatch):
    seen = {}

    def fake(method, path, **kw):
        seen.update({"method": method, "path": path, "json": kw.get("json")})
        return {"translation": {"lines": [{"index": 0, "translated": "Hello"}]}, "run": {"id": "r1"}}

    monkeypatch.setattr(mcp, "_call", fake)
    result = mcp.tool_save_translation({
        "session_id": "s1", "target_lang": "en", "provider": "workbuddy", "model": "buddy",
        "lines": [{"index": 0, "translated": "Hello"}, {"index": 1, "translated": "World"}],
    })
    assert result["run_id"] == "r1" and result["line_count"] == 1
    assert result["translated_lines"] == 1 and len(result["revision"]) == 12
    assert "translation" not in result
    assert seen["method"] == "POST" and seen["path"] == "/translation/sessions/s1/apply"
    assert seen["json"]["provider"] == "workbuddy"
    assert seen["json"]["lines"] == [{"index": 0, "translated": "Hello"},
                                     {"index": 1, "translated": "World"}]


def test_save_translation_can_return_full_translation_when_asked(monkeypatch):
    full = {"target": "en", "lines": [{"index": 0, "text": "甲", "translated": "A"}],
            "agent": {"run_id": "r1", "issues": []}}
    monkeypatch.setattr(mcp, "_call", lambda *args, **kw: {"translation": full, "run": {"id": "r1"}})
    result = mcp.tool_save_translation({
        "session_id": "s1", "target_lang": "en", "include_translation": True,
        "lines": [{"index": 0, "translated": "A"}],
    })
    assert result["translation"] == full and result["run"]["id"] == "r1"
