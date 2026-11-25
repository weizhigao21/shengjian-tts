"""Backfill translation memory from whole translations written before 0.9.2.

This does not call a model and never touches recognition text or translations.
It only reads finished sessions and writes their existing line pairs to memory.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

API = os.environ.get("SHENGJIAN_API", "http://127.0.0.1:8765").rstrip("/")


def call(method, path, payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        API + path, data=body, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None


def backfill(session_id=""):
    sessions = call("GET", "/recognition/sessions")
    written = 0
    skipped = 0
    for record in sessions:
        identity = record.get("id") or ""
        if not identity or (session_id and identity != session_id):
            continue
        state = (call("GET", "/recognition/sessions/" + identity) or {}).get("state") or {}
        translation = state.get("translation") or {}
        lines = translation.get("lines") or []
        if not lines:
            continue
        source_lang = state.get("recognized_language") or state.get("language") or ""
        target_lang = translation.get("target") or ""
        provider = translation.get("provider") or "external"
        model = translation.get("model") or ""
        for line in lines:
            source = str(line.get("text") or "").strip()
            target = str(line.get("translated") or "").strip()
            if not source or not target:
                skipped += 1
                continue
            call("POST", "/translation/memory", {
                "source": source, "target": target,
                "source_lang": source_lang, "target_lang": target_lang,
                "session_id": identity, "provider": provider,
                "model": model, "quality": 1,
            })
            written += 1
    return written, skipped


def main():
    global API
    parser = argparse.ArgumentParser(description="把已有完整译文补写进翻译记忆")
    parser.add_argument("--session-id", default="", help="只补写指定识别记录；默认处理全部")
    parser.add_argument("--api", default=API, help="声笺服务地址")
    args = parser.parse_args()
    API = args.api.rstrip("/")
    written, skipped = backfill(args.session_id)
    print(f"已补写 {written} 行翻译记忆，跳过 {skipped} 行空原文或空译文。")


if __name__ == "__main__":
    main()
