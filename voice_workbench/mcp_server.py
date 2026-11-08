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
    try:
        # trust_env=False：本机请求绝不能走系统代理，否则 HTTP_PROXY 会把它变成 502。
        with httpx.Client(base_url=API, timeout=httpx.Timeout(60.0, connect=3.0), trust_env=False) as client:
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
        raise ToolError("找不到音频文件：" + str(path))
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


# ---------------------------------------------------------------- 工具清单

TOOLS = [
    {
        "name": "list_voices",
        "description": "列出声笺声音库里可用的音色预设。生成语音前先调用它拿到 voice_id；不带 voice_id 调用 speak 时会用列表里的第一个。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "run": tool_list_voices,
    },
    {
        "name": "speak",
        "description": "把一段文字转成本机 WAV 音频，返回音频文件的绝对路径。适合念台词、旁白、短段落。文字里的 markdown 会被自动清理，长文本按句自动分段，返回多条音频。",
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
        "description": "生成多角色对白：每句台词用各自的音色，返回按顺序排列的音频路径。适合广播剧、播客、有声书片段。",
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
        "description": "查询一次生成任务的进度和已完成的音频路径。speak 返回 status=generating 时用它接着查。",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "speak 返回的 job_id。"}},
            "required": ["job_id"],
        },
        "run": tool_get_job,
    },
    {
        "name": "transcribe",
        "description": "把本机音频文件转成文字，并给出每行的起止时间（秒）。需要声笺里已下载识别模型。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "音频文件的绝对路径。"},
                "language": {"type": "string", "description": "语言代码如 zh / en，默认 auto 自动判断。"},
            },
            "required": ["path"],
        },
        "run": tool_transcribe,
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
            raise ToolError("没有名为 " + str(name) + " 的工具。可用：" + "、".join(_BY_NAME))
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
                "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
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
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
