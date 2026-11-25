"""MCP stdio 服务：让桌面 AI 客户端（Claude Desktop、Cursor、Cline 等）调用本机声笺。

设计约束：

- 不 import Manager / create_app，只通过本机 HTTP 复用已在运行的服务。因此不会触发
  ServiceLock（同一数据目录只允许一个推理服务，见 services/service_lock.py）。
- 不依赖 MCP SDK。stdio 传输就是逐行 JSON-RPC 2.0，这里只实现 initialize /
  tools/list / tools/call 和最少的通知处理，避免把 starlette、anyio 拖进打包体积。
- stdout 只写协议消息，所有日志走 stderr；往 stdout 打一行日志客户端就会断开。
- 生成的音频一律返回本机绝对路径，不返回 http 地址：AI 客户端常在沙箱里，
  抓不到 127.0.0.1，但能直接读本地文件。

客户端配置示例：

    {"command": "python", "args": ["-m", "voice_workbench", "mcp"]}

服务地址可用环境变量 SHENGJIAN_API 覆盖，默认 http://127.0.0.1:8765。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

from . import __version__

API = os.environ.get("SHENGJIAN_API", "http://127.0.0.1:8765").rstrip("/")
PROTOCOL = "2024-11-05"
WAIT_SECONDS = float(os.environ.get("SHENGJIAN_MCP_WAIT", "120"))
CHUNK_CHARS = 180
MAX_CHUNKS = 300

_ROOT: Path | None = None
_CACHE = {"at": 0.0, "usable": [], "broken": []}


class ToolError(Exception):
    """直接展示给模型的错误说明，不带堆栈。"""


# ---------------------------------------------------------------- 服务调用


def _call(method, path, **kwargs):
    timeout = kwargs.pop("timeout", 60.0)
    try:
        # trust_env=False：本机请求绝不能走系统代理，否则 HTTP_PROXY 会把它变成 502。
        with httpx.Client(base_url=API, timeout=httpx.Timeout(timeout, connect=3.0), trust_env=False) as client:
            response = client.request(method, path, **kwargs)
    except httpx.ConnectError:
        raise ToolError(
            f"声笺服务没有运行（{API}）。请先打开声笺桌面端，或在项目目录执行 "
            "python -m voice_workbench serve --port 8765 后重试。"
        ) from None
    except httpx.HTTPError:
        raise ToolError("声笺服务连接中断或响应超时，可能正在处理其他生成任务，请稍后重试。") from None
    if response.status_code >= 400:
        raise ToolError(_describe(response))
    return response.json() if response.content else {}


def _describe(response):
    try:
        payload = response.json()
    except ValueError:
        return f"声笺服务返回 HTTP {response.status_code}。"
    error = payload.get("error") or {}
    message = payload.get("msg") or error.get("message") or f"HTTP {response.status_code}"
    hint = error.get("hint")
    return f"{message}。{hint}" if hint else str(message)


def _audio_path(filename):
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(_call("GET", "/health")["data_dir"])
    return str(_ROOT / "audio" / filename)


# ---------------------------------------------------------------- 音色


def _voice_list(force=False):
    if force or time.monotonic() - _CACHE["at"] > 30:
        # 模板不是真音色；带 error 的预设模型或参考音频失效，不能用来生成。
        raw = [voice for voice in _call("GET", "/voices") if not voice.get("is_template")]
        _CACHE.update(at=time.monotonic(),
                      usable=[v for v in raw if not v.get("error")],
                      broken=[v for v in raw if v.get("error")])
    return _CACHE["usable"]


def _empty_reason():
    """区分「一个预设都没有」和「有预设但都不可用」——AI 的下一步动作完全不同。"""
    broken = _CACHE.get("broken") or []
    if not broken:
        return "声音库里还没有声音预设。请先在声笺里新建一个音色再调用。"
    first = broken[0]
    error = first.get("error") or {}
    return ("有 " + str(len(broken)) + " 个预设当前不可用，例如「" + str(first.get("name") or first.get("id"))
            + "」：" + str(error.get("message") or "配置无效") + "。"
            + str(error.get("hint") or "请在声笺里检查模型与参考音频路径后重试。"))


def _resolve_voice(voice_id=None, name=None):
    items = _voice_list()
    if not items:
        raise ToolError(_empty_reason())
    if voice_id:
        for item in items:
            if item.get("id") == voice_id:
                return item
        raise ToolError("找不到 voice_id=" + voice_id + "。可用 id：" + "、".join(v["id"] for v in items[:10]))
    if name:
        matched = [v for v in items if v.get("name") == name]
        matched = matched or [v for v in items if name in (v.get("name") or "")]
        if matched:
            return matched[0]
        raise ToolError("找不到名为「" + name + "」的声音。可用：" + "、".join(str(v.get("name") or v["id"]) for v in items[:10]))
    return items[0]


# ---------------------------------------------------------------- 文本处理

_MARKUP = (
    (re.compile(r"```[\s\S]*?```"), " "),
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), " "),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"https?://\S+"), " "),
    (re.compile(r"[`*_~]+"), ""),
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M), ""),
    (re.compile(r"^[ \t]{0,3}[-*+>][ \t]+", re.M), ""),
    (re.compile(r"[ \t]+"), " "),
)

# 句末切分；模型输出的长段落没有换行，靠标点断比按字符硬切自然。
_SENTENCE = re.compile(r"(?<=[。！？；…!?;])|\n+")


def _plain(text):
    for pattern, replacement in _MARKUP:
        text = pattern.sub(replacement, text)
    return text.strip()


def _split(text):
    """清理 markdown 后按句合成不超过 CHUNK_CHARS 字的片段。"""
    sentences = [part.strip() for part in _SENTENCE.split(_plain(text)) if part and part.strip()]
    chunks, current = [], ""
    for sentence in sentences:
        while len(sentence) > CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:CHUNK_CHARS])
            sentence = sentence[CHUNK_CHARS:]
        if current and len(current) + len(sentence) > CHUNK_CHARS:
            chunks.append(current)
            current = sentence
        else:
            # 换行分开的短句（列表、小标题）原本没有标点，补一个逗号才有停顿。
            if current and current[-1] not in "。！？；…，、,!?;":
                current += "，"
            current += sentence
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()][:MAX_CHUNKS]


# ---------------------------------------------------------------- 生成


def _submit(segments, voice, title, overrides=None):
    body = {"title": title or ("AI：" + segments[0]["text"][:20]), "voice_id": voice["id"], "segments": segments}
    if overrides:
        body["overrides"] = overrides
    return _call("POST", "/jobs", json=body)


def _settled(job):
    segments = job.get("segments") or []
    return bool(segments) and all(s.get("status") in ("done", "failed", "cancelled") for s in segments)


def _wait(job_id, generation, seconds=WAIT_SECONDS):
    deadline = time.monotonic() + seconds
    while True:
        job = _call("GET", "/jobs/" + job_id, params={"generation": generation})
        if _settled(job):
            return job
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.4)


def _collect(job, job_id, voice):
    failed = [s for s in job["segments"] if s.get("status") == "failed"]
    if failed:
        detail = failed[0].get("error") or {}
        raise ToolError("第 " + str(failed[0].get("idx", "?")) + " 段生成失败：" + str(detail.get("message") or "未知原因"))
    done = [s for s in job["segments"] if s.get("status") == "done" and s.get("audio")]
    if not done:
        raise ToolError("生成已结束但没有产出音频，请检查声笺里的模型与引擎状态。")
    paths = [_audio_path(s["audio"]) for s in done]
    return {
        "audio_path": paths[0],
        "audio_paths": paths,
        "segment_count": len(paths),
        "duration_seconds": round(sum(float(s.get("duration") or 0) for s in done), 2),
        "voice": voice.get("name") or voice["id"],
        "job_id": job_id,
    }


def _overrides(arguments):
    overrides = {}
    speed = arguments.get("speed")
    if speed is not None:
        if not 0.5 <= float(speed) <= 2:
            raise ToolError("speed 需要在 0.5 到 2 之间，1 是原速。")
        overrides["speed"] = float(speed)
    if arguments.get("emotion"):
        overrides["emotion"] = str(arguments["emotion"])
    return overrides


# ---------------------------------------------------------------- 工具实现


def tool_list_voices(arguments):
    items = _voice_list(force=True)
    if not items:
        return {"count": 0, "voices": [], "note": _empty_reason()}
    return {"count": len(items), "voices": [
        {"voice_id": v["id"], "name": v.get("name") or v["id"], "backend": v.get("backend")}
        for v in items
    ]}


def tool_speak(arguments):
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ToolError("text 不能为空。")
    chunks = _split(text)
    if not chunks:
        raise ToolError("清理 markdown 之后没有剩下可朗读的文字。")
    voice = _resolve_voice(arguments.get("voice_id"), arguments.get("speaker"))
    submitted = _submit([{"text": chunk} for chunk in chunks], voice,
                        arguments.get("title"), _overrides(arguments))
    job = _wait(submitted["job_id"], submitted["generation"])
    if job is None:
        return {"status": "generating", "job_id": submitted["job_id"], "segment_count": len(chunks),
                "message": "仍在生成，已等待 " + str(int(WAIT_SECONDS)) + " 秒。可用 get_job 查询进度。"}
    return _collect(job, submitted["job_id"], voice)


def tool_speak_dialogue(arguments):
    lines = arguments.get("lines") or []
    if not lines:
        raise ToolError("lines 不能为空，每项需要 speaker（或 voice_id）与 text。")
    segments, groups = [], []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        voice = _resolve_voice(line.get("voice_id"), line.get("speaker"))
        chunks = _split(text)
        if not chunks:
            continue
        for chunk in chunks:
            segments.append({"text": chunk, "voice_id": voice["id"]})
        groups.append({"voice": voice, "speaker": voice.get("name") or voice["id"],
                       "chunk_count": len(chunks), "segments": len(segments)})
    if not segments:
        raise ToolError("所有台词都是空的，没有可生成的内容。")
    submitted = _submit(segments, groups[0]["voice"], arguments.get("title"), _overrides(arguments))
    job = _wait(submitted["job_id"], submitted["generation"])
    if job is None:
        return {"status": "generating", "job_id": submitted["job_id"], "segment_count": len(segments),
                "message": "仍在生成，已等待 " + str(int(WAIT_SECONDS)) + " 秒。可用 get_job 查询进度。"}
    result = _collect(job, submitted["job_id"], groups[0]["voice"])
    cursor = 0
    performed = []
    for group in groups:
        paths = result["audio_paths"][cursor:cursor + group["chunk_count"]]
        cursor += group["chunk_count"]
        performed.append({"speaker": group["speaker"], "audio_paths": paths})
    result["lines"] = performed
    result.pop("audio_path", None)
    return result


def tool_get_job(arguments):
    job_id = str(arguments.get("job_id") or "").strip()
    if not job_id:
        raise ToolError("job_id 不能为空。")
    job = _call("GET", "/jobs/" + job_id)
    segments = [{
        "index": s.get("idx"),
        "status": s.get("status"),
        "audio_path": _audio_path(s["audio"]) if s.get("audio") else None,
        "error": (s.get("error") or {}).get("message"),
    } for s in job.get("segments") or []]
    return {"job_id": job_id, "status": job.get("status"), "segments": segments}


def tool_transcribe(arguments):
    path = Path(str(arguments.get("path") or "")).expanduser()
    if not path.is_file():
        raise ToolError("找不到音频或视频文件：" + str(path))
    session = _call("POST", "/recognition/sessions", json={
        "path": str(path), "name": path.stem, "language": arguments.get("language") or "auto"})
    identity = session.get("id") or (session.get("state") or {}).get("id")
    if not identity:
        raise ToolError("识别会话创建失败，服务没有返回会话 ID。")
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        record = _call("GET", "/recognition/sessions/" + identity)
        state = record.get("state") or {}
        if state.get("status") in ("done", "failed", "cancelled"):
            if state["status"] != "done":
                raise ToolError("语音识别" + ("失败" if state["status"] == "failed" else "已取消") + "：" + str(state.get("warning") or "未给出原因"))
            return {"text": state.get("text") or "", "duration_seconds": state.get("duration"),
                    "language": state.get("recognized_language"),
                    "lines": [{"index": line.get("index"), "start": line.get("start"), "end": line.get("end"), "text": line.get("text")}
                              for line in state.get("lines") or []],
                    "session_id": identity,
                    "warning": state.get("warning")}
        time.sleep(0.5)
    raise ToolError("识别仍在进行，会话 ID 是 " + identity + "，稍后可以在声笺里查看结果。")


# ---------------------------------------------------------------- 翻译知识库


def _revision(lines):
    """Stable hash of the source lines: text or line order changes invalidate a read."""
    payload = [[line.get("index"), line.get("text") or ""] for line in lines]
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    return digest[:12]


def _translated_count(translation):
    return sum(1 for line in (translation or {}).get("lines") or []
               if str(line.get("translated") or "").strip())


def _translation_meta(translation):
    translation = translation or {}
    agent = translation.get("agent") or {}
    return {"target": translation.get("target") or "",
            "provider": translation.get("provider") or "",
            "model": translation.get("model") or "",
            "run_id": translation.get("agent_run_id") or agent.get("run_id") or ""}


def _translation_summary(identity, translation, target=""):
    """MCP 只回写回执，不回显整段原文和译文；需要全文时再读一次。"""
    translation = translation or {}
    lines = translation.get("lines") or []
    agent = translation.get("agent") or {}
    return {"session_id": identity,
            "target": translation.get("target") or target or "",
            "line_count": len(lines),
            "translated_lines": _translated_count(translation),
            "revision": _revision(lines),
            "run_id": translation.get("agent_run_id") or agent.get("run_id") or "",
            "issues": len(agent.get("issues") or []),
            "memory_written": agent.get("memory_written", 0)}


def _bounded_int(arguments, name, default, low, high):
    try:
        value = int(arguments.get(name) or default)
    except (TypeError, ValueError):
        raise ToolError(name + " 必须是整数。") from None
    return max(low, min(high, value))



def tool_list_translation_sessions(arguments):
    records = _call("GET", "/recognition/sessions")
    batch_id = str(arguments.get("batch_id") or "").strip()
    status = str(arguments.get("status") or "").strip()
    if batch_id:
        records = [record for record in records if str(record.get("batch_id") or "") == batch_id]
    if status:
        records = [record for record in records if str(record.get("status") or "") == status]
    total = len(records)
    limit = _bounded_int(arguments, "limit", 20, 1, 200)
    fields = ("id", "name", "status", "batch_id", "batch_index",
              "line_count", "translated_lines", "target")
    sessions = [{key: record.get(key) for key in fields} for record in records[:limit]]
    return {"count": len(sessions), "total": total, "has_more": total > limit,
            "batch_id": batch_id or None, "sessions": sessions}


def tool_get_subtitle(arguments):
    identity = str(arguments.get("session_id") or "").strip()
    if not identity:
        raise ToolError("session_id 不能为空。")
    record = _call("GET", "/recognition/sessions/" + identity)
    state = record.get("state") or {}
    lines = state.get("lines") or []
    translation = state.get("translation") or {}
    translated = {line.get("index"): line.get("translated") for line in translation.get("lines") or []}
    translated_lines = _translated_count(translation)
    revision = _revision(lines)
    known = str(arguments.get("known_revision") or "").strip()
    if known and known == revision:
        return {"session_id": identity, "revision": revision, "unchanged": True,
                "line_count": len(lines), "translated_lines": translated_lines,
                "target": translation.get("target") or ""}

    cursor = _bounded_int(arguments, "cursor", 0, 0, len(lines))
    limit = _bounded_int(arguments, "limit", 50, 1, 200)
    page = lines[cursor:cursor + limit]
    end = cursor + len(page)
    fmt = str(arguments.get("format") or "compact").strip().lower()
    if fmt not in ("compact", "objects"):
        raise ToolError('format 只支持 "compact" 或 "objects"。')
    include_timecodes = bool(arguments.get("include_timecodes", False))
    include_translation = arguments.get("include_translation")
    if include_translation is None:
        # 旧客户端显式用 objects 时保留 0.9.8 的合并译文形状。
        include_translation = fmt == "objects"
    include_translation = bool(include_translation)

    result = {
        "session_id": identity,
        "name": state.get("name"),
        "status": state.get("status"),
        "language": state.get("recognized_language"),
        "duration_seconds": state.get("duration"),
        "batch_id": state.get("batch_id"),
        "batch_index": state.get("batch_index"),
        "line_count": len(lines),
        "translated_lines": translated_lines,
        "target": translation.get("target") or "",
        "revision": revision,
        "cursor": cursor,
        "next_cursor": end if end < len(lines) else None,
        "done": end >= len(lines),
        "has_translation": bool(translation),
    }
    if state.get("warning"):
        result["warning"] = state["warning"]
    if fmt == "compact":
        columns = ["i"]
        if include_timecodes:
            columns += ["start", "end"]
        columns.append("text")
        if include_translation:
            columns.append("translated")
        rows = []
        for line in page:
            row = [line.get("index")]
            if include_timecodes:
                row += [line.get("start"), line.get("end")]
            row.append(line.get("text") or "")
            if include_translation:
                row.append(translated.get(line.get("index")))
            rows.append(row)
        result["columns"] = columns
        result["lines"] = rows
        if include_translation:
            result["translation_meta"] = _translation_meta(translation)
    else:
        rows = []
        for line in page:
            item = {"index": line.get("index"), "start": line.get("start"),
                    "end": line.get("end"), "text": line.get("text")}
            if include_translation:
                item["translated"] = translated.get(line.get("index"))
            rows.append(item)
        result["lines"] = rows
        if include_translation:
            result["translation"] = translation
    return result



def tool_correct_subtitle(arguments):
    identity = str(arguments.get("session_id") or "").strip()
    if not identity:
        raise ToolError("session_id 不能为空。")
    corrections = arguments.get("corrections") or []
    if not isinstance(corrections, list) or not corrections:
        raise ToolError("corrections 不能为空，每项需要 index 和 text。")
    clean = []
    for item in corrections:
        if not isinstance(item, dict):
            raise ToolError("corrections 的每一项都必须是对象。")
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            raise ToolError("corrections[].index 必须是整数。") from None
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            raise ToolError("corrections[].text 不能为空。")
        clean.append({"index": index, "text": text,
                      "reason": str(item.get("reason") or "")[:200]})
    return _call("PATCH", "/recognition/sessions/" + identity + "/lines",
                 json={"corrections": clean})


def tool_list_translation_terms(arguments):
    params = {"enabled_only": bool(arguments.get("enabled_only", True))}
    if arguments.get("query"):
        params["query"] = str(arguments["query"])
    if arguments.get("target_lang"):
        params["target"] = str(arguments["target_lang"])
    return {"terms": _call("GET", "/translation/terms", params=params)}


def tool_add_translation_term(arguments):
    source = str(arguments.get("source") or "").strip()
    translation = str(arguments.get("translation") or "").strip()
    if not source or not translation:
        raise ToolError("source 和 translation 都不能为空。")
    body = {"source": source, "target": translation,
            "source_lang": str(arguments.get("source_lang") or ""),
            "target_lang": str(arguments.get("target_lang") or ""),
            "note": str(arguments.get("note") or ""),
            "priority": int(arguments.get("priority") or 0)}
    return {"term": _call("POST", "/translation/terms", json=body)}


def tool_update_translation_term(arguments):
    identity = str(arguments.get("term_id") or "").strip()
    if not identity:
        raise ToolError("term_id 不能为空。")
    body = {}
    if "translation" in arguments:
        body["target"] = str(arguments["translation"] or "")
    if "note" in arguments:
        body["note"] = str(arguments["note"] or "")
    if "priority" in arguments:
        body["priority"] = int(arguments["priority"])
    if "enabled" in arguments:
        body["enabled"] = bool(arguments["enabled"])
    if not body:
        raise ToolError("至少提供 translation、note、priority 或 enabled 中的一项。")
    return {"term": _call("PATCH", "/translation/terms/" + identity, json=body)}


def tool_disable_translation_term(arguments):
    identity = str(arguments.get("term_id") or "").strip()
    if not identity:
        raise ToolError("term_id 不能为空。")
    return {"term": _call("DELETE", "/translation/terms/" + identity)}


def tool_list_translation_memory(arguments):
    params = {"enabled_only": bool(arguments.get("enabled_only", True))}
    if arguments.get("query"):
        params["query"] = str(arguments["query"])
    if arguments.get("target_lang"):
        params["target"] = str(arguments["target_lang"])
    return {"memories": _call("GET", "/translation/memory", params=params)}


def tool_add_translation_memory(arguments):
    source = str(arguments.get("source") or "").strip()
    translation = str(arguments.get("translation") or "").strip()
    if not source or not translation:
        raise ToolError("source 和 translation 都不能为空。")
    body = {"source": source, "target": translation,
            "source_lang": str(arguments.get("source_lang") or ""),
            "target_lang": str(arguments.get("target_lang") or ""),
            "quality": max(1, min(2, int(arguments.get("quality") or 1)))}
    return {"memory": _call("POST", "/translation/memory", json=body)}


def tool_update_translation_memory(arguments):
    identity = str(arguments.get("memory_id") or "").strip()
    if not identity:
        raise ToolError("memory_id 不能为空。")
    body = {}
    if "translation" in arguments:
        body["target"] = str(arguments["translation"] or "").strip()
        if not body["target"]:
            raise ToolError("translation 不能为空。")
    if "quality" in arguments:
        body["quality"] = max(1, min(2, int(arguments["quality"])))
    if "enabled" in arguments:
        body["enabled"] = bool(arguments["enabled"])
    if not body:
        raise ToolError("至少提供 translation、quality 或 enabled 中的一项。")
    return {"memory": _call("PATCH", "/translation/memory/" + identity, json=body)}


def tool_delete_translation_memory(arguments):
    identity = str(arguments.get("memory_id") or "").strip()
    if not identity:
        raise ToolError("memory_id 不能为空。")
    return {"memory": _call("DELETE", "/translation/memory/" + identity)}


def tool_translate_subtitle(arguments):
    identity = str(arguments.get("session_id") or "").strip()
    if not identity:
        raise ToolError("session_id 不能为空。")
    body = {"session_id": identity, "use_memory": bool(arguments.get("use_memory", True))}
    if arguments.get("target_lang"):
        body["target"] = str(arguments["target_lang"])
    if arguments.get("provider"):
        body["provider"] = str(arguments["provider"])
    # Agent 翻译可能逐行重试，给本机 HTTP 留出比普通生成更长的等待。
    response = _call("POST", "/translation/agent/translate", json=body, timeout=600.0)
    summary = _translation_summary(identity, response.get("translation") or {},
                                   body.get("target") or "")
    run = response.get("run") or {}
    summary["status"] = run.get("status") or ""
    summary["run_id"] = run.get("id") or summary["run_id"]
    if arguments.get("include_translation"):
        summary["translation"] = response.get("translation") or {}
        summary["run"] = run
    return summary


def tool_save_translation(arguments):
    identity = str(arguments.get("session_id") or "").strip()
    if not identity:
        raise ToolError("session_id 不能为空。")
    target = str(arguments.get("target_lang") or "").strip()
    if not target:
        raise ToolError("target_lang 不能为空，例如 zh、en。")
    lines = arguments.get("lines") or []
    if not isinstance(lines, list) or not lines:
        raise ToolError("lines 不能为空，每项需要 index 和 translated。")
    clean = []
    for item in lines:
        if not isinstance(item, dict):
            raise ToolError("lines 的每一项都必须是对象。")
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            raise ToolError("lines[].index 必须是整数。") from None
        clean.append({"index": index, "translated": str(item.get("translated") or "")})
    body = {"target": target,
            "provider": str(arguments.get("provider") or "external"),
            "model": str(arguments.get("model") or ""),
            "lines": clean}
    response = _call("POST", "/translation/sessions/" + identity + "/apply", json=body)
    summary = _translation_summary(identity, response.get("translation") or {}, target)
    summary["run_id"] = (response.get("run") or {}).get("id") or summary["run_id"]
    if arguments.get("include_translation"):
        summary["translation"] = response.get("translation") or {}
        summary["run"] = response.get("run") or {}
    return summary


def tool_list_translation_runs(arguments):
    params = {}
    if arguments.get("session_id"):
        params["session_id"] = str(arguments["session_id"])
    return {"runs": _call("GET", "/translation/agent/runs", params=params)}


def tool_get_translation_run(arguments):
    identity = str(arguments.get("run_id") or "").strip()
    if not identity:
        raise ToolError("run_id 不能为空。")
    return {"run": _call("GET", "/translation/agent/runs/" + identity)}


# ---------------------------------------------------------------- 工具清单

TOOLS = [
    {
        "name": "list_voices",
        "description": "列出可用音色预设。生成前先取 voice_id；省略时用第一个。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "run": tool_list_voices,
    },
    {
        "name": "speak",
        "description": "把文字转成本机 WAV，返回绝对路径。自动清 markdown、按句分段。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要朗读的文字。会清理 markdown，按句切分。"},
                "voice_id": {"type": "string", "description": "音色 ID，来自 list_voices。省略时用第一个可用音色。"},
                "speaker": {"type": "string", "description": "音色名称，作为 voice_id 的替代写法，例如「御姐」。"},
                "speed": {"type": "number", "description": "语速 0.5–2，1 为原速。仅部分模型支持。"},
                "emotion": {"type": "string", "description": "情绪描述（如「低沉、克制」）。仅 IndexTTS 等支持文字引导的模型生效。"},
                "title": {"type": "string", "description": "作品标题，省略时自动取正文前 20 字并加 AI 前缀。"},
            },
            "required": ["text"],
        },
        "run": tool_speak,
    },
    {
        "name": "speak_dialogue",
        "description": "多角色对白：每句用各自音色，返回按顺序的音频路径。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "array",
                    "description": "台词列表，按播放顺序。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "speaker": {"type": "string", "description": "音色名称，例如「姐姐」。"},
                            "voice_id": {"type": "string", "description": "音色 ID，优先于 speaker。"},
                            "text": {"type": "string", "description": "这句台词。"},
                        },
                        "required": ["text"],
                    },
                },
                "title": {"type": "string", "description": "作品标题。"},
                "speed": {"type": "number", "description": "整批语速 0.5–2。"},
            },
            "required": ["lines"],
        },
        "run": tool_speak_dialogue,
    },
    {
        "name": "get_job",
        "description": "查生成任务进度；speak 返回 generating 时继续查。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "speak 返回的 job_id。"}},
            "required": ["job_id"],
        },
        "run": tool_get_job,
    },
    {
        "name": "transcribe",
        "description": "把本机音频/视频转文字，返回逐行起止秒；视频只读音轨，不改原文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "音频或视频文件的绝对路径。"},
                "language": {"type": "string", "description": "语言代码如 zh / en，默认 auto 自动判断。"},
            },
            "required": ["path"],
        },
        "run": tool_transcribe,
    },
    {
        "name": "list_translation_sessions",
        "description": "列出识别记录摘要，先拿 session_id。可按 batch_id/status 筛选；默认 20 条。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回多少条，默认 20，最大 200。"},
                "batch_id": {"type": "string", "description": "只返回某个批量识别批次中的文件。"},
                "status": {"type": "string", "description": "按识别状态筛选，如 done、running、failed。"},
            },
            "additionalProperties": False,
        },
        "run": tool_list_translation_sessions,
    },
    {
        "name": "get_subtitle",
        "description": "按块读取字幕：默认 compact、不含时间码；known_revision 未变时只回 unchanged。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "识别会话 ID，来自 list_translation_sessions。"},
                "cursor": {"type": "integer", "description": "起始行位置，默认 0。"},
                "limit": {"type": "integer", "description": "本次最多返回多少行，默认 50，最大 200。"},
                "include_timecodes": {"type": "boolean", "description": "是否返回 start/end，默认 false。"},
                "include_translation": {"type": "boolean", "description": "是否返回已有译文；objects 模式默认 true。"},
                "format": {"type": "string", "enum": ["compact", "objects"],
                           "description": "compact 返回 columns+数组（默认）；objects 兼容旧客户端。"},
                "known_revision": {"type": "string", "description": "上次读取的 revision；未变只回 unchanged。"},
            },
            "required": ["session_id"],
        },
        "run": tool_get_subtitle,
    },
    {
        "name": "correct_subtitle",
        "description": "修正字幕行文本，只改文字不改时间码；旧译文会被清除，需重新翻译。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "识别会话 ID。"},
                "corrections": {"type": "array", "minItems": 1, "maxItems": 100,
                                "items": {"type": "object", "properties": {
                                    "index": {"type": "integer", "description": "字幕行号，来自 get_subtitle。"},
                                    "text": {"type": "string", "description": "修正后的整行文本。"},
                                    "reason": {"type": "string", "description": "修正理由，简短。"}},
                                    "required": ["index", "text"]}},
            },
            "required": ["session_id", "corrections"],
        },
        "run": tool_correct_subtitle,
    },
    {
        "name": "list_translation_terms",
        "description": "查术语库；翻译前先查，固定译法优先。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "按原文或译文搜索。"},
                "target_lang": {"type": "string", "description": "目标语言筛选，如 zh、en。"},
                "enabled_only": {"type": "boolean", "description": "只返回启用词条，默认 true。"},
            },
            "additionalProperties": False,
        },
        "run": tool_list_translation_terms,
    },
    {
        "name": "add_translation_term",
        "description": "新增或更新固定译法；同原文+语言对会更新，不重复建。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "原文术语。"},
                "translation": {"type": "string", "description": "固定译文。"},
                "source_lang": {"type": "string", "description": "源语言代码，可留空。"},
                "target_lang": {"type": "string", "description": "目标语言代码，如 zh；留空表示所有目标。"},
                "note": {"type": "string", "description": "使用说明。"},
                "priority": {"type": "integer", "description": "优先级 0–100，越大越优先。"},
            },
            "required": ["source", "translation"],
        },
        "run": tool_add_translation_term,
    },
    {
        "name": "update_translation_term",
        "description": "修改现有术语的译文、说明、优先级或启用状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "term_id": {"type": "string", "description": "术语 ID，来自 list_translation_terms。"},
                "translation": {"type": "string", "description": "新的固定译文。"},
                "note": {"type": "string", "description": "新的说明。"},
                "priority": {"type": "integer", "description": "优先级 0–100。"},
                "enabled": {"type": "boolean", "description": "是否启用。"},
            },
            "required": ["term_id"],
        },
        "run": tool_update_translation_term,
    },
    {
        "name": "disable_translation_term",
        "description": "停用术语（软删除）。",
        "inputSchema": {
            "type": "object",
            "properties": {"term_id": {"type": "string", "description": "术语 ID。"}},
            "required": ["term_id"],
        },
        "run": tool_disable_translation_term,
    },
    {
        "name": "list_translation_memory",
        "description": "查翻译记忆；精确命中可直接复用译文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "按原文或译文搜索。"},
                "target_lang": {"type": "string", "description": "目标语言筛选。"},
                "enabled_only": {"type": "boolean", "description": "只返回启用记忆，默认 true。"},
            },
            "additionalProperties": False,
        },
        "run": tool_list_translation_memory,
    },
    {
        "name": "add_translation_memory",
        "description": "写入或更新翻译记忆；同归一原文+语言对会更新。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "原文。"},
                "translation": {"type": "string", "description": "译文。"},
                "source_lang": {"type": "string", "description": "源语言代码。"},
                "target_lang": {"type": "string", "description": "目标语言代码。"},
                "quality": {"type": "integer", "description": "1 普通，2 已确认；默认 1。"},
            },
            "required": ["source", "translation"],
        },
        "run": tool_add_translation_memory,
    },
    {
        "name": "update_translation_memory",
        "description": "修改已有翻译记忆的译文、质量或启用状态。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "记忆 ID，来自 list_translation_memory。"},
                "translation": {"type": "string", "description": "新的译文。"},
                "quality": {"type": "integer", "description": "1 普通，2 已确认。"},
                "enabled": {"type": "boolean", "description": "是否启用。"},
            },
            "required": ["memory_id"],
        },
        "run": tool_update_translation_memory,
    },
    {
        "name": "delete_translation_memory",
        "description": "停用记忆（软删除）。",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string", "description": "记忆 ID。"}},
            "required": ["memory_id"],
        },
        "run": tool_delete_translation_memory,
    },
    {
        "name": "translate_subtitle",
        "description": "用声笺翻译 Agent 翻整条记录：查术语、复用记忆、质检、写记忆。需先配置翻译模型。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "识别会话 ID。"},
                "target_lang": {"type": "string", "description": "目标语言代码，如 zh、en；省略时用设置。"},
                "provider": {"type": "string", "description": "提供方 id，如 api、local；省略时用设置。"},
                "use_memory": {"type": "boolean", "description": "是否读取和写入翻译记忆，默认 true。"},
                "include_translation": {"type": "boolean", "description": "默认 false，只回行数与 run_id；true 才回显完整译文，占大量 token。"},
            },
            "required": ["session_id"],
        },
        "run": tool_translate_subtitle,
    },
    {
        "name": "save_translation",
        "description": "外部 Agent 回写整条译文；lines 必须覆盖全部原始行号，不改原文和时间码。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "识别会话 ID。"},
                "target_lang": {"type": "string", "description": "目标语言代码，如 zh、en；必填。"},
                "provider": {"type": "string", "description": "外部来源标记，例如 workbuddy、doubao。"},
                "model": {"type": "string", "description": "外部 Agent 使用的模型名，可留空。"},
                "lines": {
                    "type": "array",
                    "description": "完整译文，按原始字幕行号。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "index": {"type": "integer", "description": "原始字幕行号。"},
                            "translated": {"type": "string", "description": "这一行的译文。"},
                        },
                        "required": ["index", "translated"],
                    },
                },
                "include_translation": {"type": "boolean", "description": "默认 false，只回写回执；true 才回显完整译文，占大量 token。"},
            },
            "required": ["session_id", "target_lang", "lines"],
        },
        "run": tool_save_translation,
    },
    {
        "name": "list_translation_runs",
        "description": "查翻译 Agent 运行记录，可按 session_id 筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "只查看这条识别记录的运行。"}},
            "additionalProperties": False,
        },
        "run": tool_list_translation_runs,
    },
    {
        "name": "get_translation_run",
        "description": "读取一次运行记录：trace、结果摘要和错误。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "运行 ID。"}},
            "required": ["run_id"],
        },
        "run": tool_get_translation_run,
    },
]

_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# ---------------------------------------------------------------- JSON-RPC


def _log(message):
    print("[shengjian-mcp] " + str(message), file=sys.stderr, flush=True)


def _dispatch(method, params):
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "shengjian-tts", "version": __version__},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{key: tool[key] for key in ("name", "description", "inputSchema")} for tool in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if tool is None:
            raise ToolError("没有名为 " + str(name) + " 的工具，请先 tools/list 查看。")
        return tool["run"](params.get("arguments") or {})
    if method in ("resources/list", "prompts/list"):
        return {"resources": []} if method == "resources/list" else {"prompts": []}
    raise ToolError("未实现的方法：" + str(method))


def _handle(message):
    method = message.get("method")
    identifier = message.get("id")
    if identifier is None:
        return None
    try:
        result = _dispatch(method, message.get("params") or {})
    except ToolError as exc:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": identifier,
                    "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True}}
        return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32602, "message": str(exc)}}
    except Exception as exc:
        _log("处理 " + str(method) + " 时异常：" + repr(exc))
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {"jsonrpc": "2.0", "id": identifier,
                "result": {"content": [{"type": "text", "text": "内部错误：" + str(exc)}], "isError": True}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": identifier,
                "result": {"content": [{"type": "text",
                                        "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}],
                           "isError": False}}
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def main():
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", newline="\n")
    _log("启动，服务地址 " + API)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _log("收到非 JSON 内容，已忽略")
            continue
        response = _handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
