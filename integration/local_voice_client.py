"""双语工作流的本地配音适配器；仅对声明 async_jobs 的本地服务启用。"""
from __future__ import annotations

import threading
import time
import os
import tempfile
from copy import deepcopy
from urllib.parse import urlsplit, urljoin
import wave

_capabilities = {}
_lock = threading.Lock()


def workflow_base_url(link):
    link = str(link).strip()
    if "://" not in link:
        link = "http://" + link
    parsed = urlsplit(link)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip("/") not in ("", "/workflow", "/voices")):
        raise ValueError("请粘贴声笺的本机接入链接，例如 http://127.0.0.1:8765/workflow。")
    # Accessing port also validates malformed or out-of-range ports.
    parsed.port
    return f"{parsed.scheme}://{parsed.netloc}"


def discover_voices(session, link):
    base = workflow_base_url(link)
    response = session.get(base + "/workflow", timeout=(3, 7), allow_redirects=False)
    if response.status_code != 200:
        raise RuntimeError("读取音色失败：" + _error(response) + "；请确认声笺已更新并启动。")
    catalog = response.json()
    if (not isinstance(catalog, dict) or catalog.get("service") != "local-voice-workbench"
            or catalog.get("schema_version") != 1 or not isinstance(catalog.get("voices"), list)):
        raise ValueError("该链接没有返回支持的声笺音色列表，请更新并重启声笺。")
    if workflow_base_url(catalog.get("base_url", "")) != base:
        raise ValueError("返回的服务地址与接入链接不一致，请重新复制接入链接。")
    ids = set()
    for voice in catalog["voices"]:
        if (not isinstance(voice, dict) or not all(isinstance(voice.get(k), str) for k in ("id", "name", "model_name", "model_tag"))
                or not voice["id"] or voice["id"] in ids or not isinstance(voice.get("available"), bool)):
            raise ValueError("音色列表格式无效，请更新并重启声笺。")
        ids.add(voice["id"])
    return {**catalog, "base_url": base, "discovery_url": base + "/workflow"}


def bind_discovered_voice(api, catalog, voice_id):
    voice = next((v for v in catalog["voices"] if v["id"] == voice_id), None)
    if voice is None:
        raise ValueError("所选音色已删除，请刷新音色列表并重新选择。")
    if not voice["available"]:
        error = voice.get("error") or {}
        raise ValueError(f"音色「{voice['name']}」不可用：" + error.get("message", "请在声笺修复预设。")
                         + ("；建议：" + error["hint"] if error.get("hint") else ""))
    return {**api, "url": catalog["base_url"], "discovery_url": catalog["discovery_url"],
            "voice_discovery": True, "voice_id": voice["id"], "voice_name": voice["name"],
            "model": voice["model_name"], "model_tag": voice["model_tag"], "status": "success"}


def resolve_workflow_config(config, session=None):
    """Freeze selected discovered presets before output directory and cache keys are computed."""
    result = deepcopy(config)
    apis = result.get("api_configs", [])
    if result.get("tts_mode", "edge") != "api" or not apis:
        return result
    index = result.get("current_api_index", 0)
    fallback = index if 0 <= index < len(apis) else 0
    indices = [i for i, a in enumerate(apis) if a.get("status") == "success"] if result.get("use_multi_api") else []
    if not indices:
        indices = [fallback]
    discovered = [i for i in indices if apis[i].get("voice_discovery")]
    if not discovered:
        return result
    if session is None:
        import requests
        with requests.Session() as local_session:
            local_session.trust_env = False
            return resolve_workflow_config(config, local_session)
    catalogs = {}
    for i in discovered:
        api = apis[i]
        base = workflow_base_url(api["url"])
        if base not in catalogs:
            catalogs[base] = discover_voices(session, base)
        voice_id = api.get("voice_id") or api.get("model", "").partition("@")[0]
        apis[i] = bind_discovered_voice(api, catalogs[base], voice_id)
    return result


def prepare_workflow_config(workflow_config, session=None):
    """Keep the persisted config and TaskQueue's shared profile consistent with the frozen run."""
    original = deepcopy(workflow_config.tts_cfg)
    resolved = resolve_workflow_config(original, session)
    current = workflow_config.tts_cfg
    # A user changing providers during discovery owns the new selection; do not overwrite it.
    if any(current.get(k) != original.get(k) for k in ("tts_mode", "current_api_index", "use_multi_api")):
        return resolved
    changed = False
    apis = current.get("api_configs", [])
    for i, (before, after) in enumerate(zip(original.get("api_configs", []), resolved.get("api_configs", []))):
        if not after.get("voice_discovery") or before == after or i >= len(apis):
            continue
        live = apis[i]
        if any(live.get(k) != before.get(k) for k in ("url", "voice_id", "model", "model_tag", "status")):
            continue
        for key in ("url", "model", "model_tag", "voice_id", "voice_name", "voice_discovery", "discovery_url", "status"):
            live[key] = after[key]
        changed = True
    if changed:
        save = getattr(workflow_config, "save", None)
        if save:
            save()
    return resolved


def is_local_service(session, api_url):
    parsed = urlsplit(api_url)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        return False
    now = time.monotonic()
    with _lock:
        cached = _capabilities.get(api_url)
        if cached and now - cached[0] < 10:
            return cached[1]
    try:
        response = session.get(api_url.rstrip("/") + "/health", timeout=3)
        body = response.json()
        supported = response.status_code == 200 and body.get("service") == "local-voice-workbench" and "async_jobs" in body.get("capabilities", [])
    except Exception:
        supported = False
    with _lock:
        _capabilities[api_url] = (now, supported)
    return supported


def _error(response):
    try:
        body = response.json()
        error = body.get("error") or {}
        return body.get("msg", str(body)) + ("；建议：" + error["hint"] if error.get("hint") else "")
    except ValueError:
        return f"HTTP {response.status_code}：{response.text[:2000]}"


def save_validated_audio(download, audio_url, save_path):
    """Validate a complete PCM WAV before replacing the workflow's output."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(save_path)),
                                         prefix='.tts-', suffix='.wav', delete=False) as target:
            temporary = target.name
        download(audio_url, temporary)
        with wave.open(temporary, 'rb') as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            if not frames or not rate:
                raise ValueError('下载的音频不是有效 WAV')
            frame_bytes = audio.getnchannels() * audio.getsampwidth()
            remaining = frames
            while remaining:
                count = min(remaining, 65536)
                if len(audio.readframes(count)) != count * frame_bytes:
                    raise ValueError('下载的 WAV 音频不完整')
                remaining -= count
        os.replace(temporary, save_path)
    except (wave.Error, EOFError) as exc:
        raise ValueError('下载的音频不是有效 WAV') from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def run_local_job(session, api_url, model_name, tasks, timeout=3600, cancel_check=None, workflow_context=None,
                  on_result=None, pause_check=None):
    """返回每片段 (audio_url, error)，部分失败也保留成功片段。

    tasks 是工作流原有的 (idx, timestamp, text, save_dir, source_mtime) 列表。
    不自动重新提交超时任务，避免后台已生成时产生重复工作。
    """
    base = api_url.rstrip("/")
    results = [None] * len(tasks)

    def deliver(position, result):
        if results[position] is None:
            results[position] = result
            if on_result:
                on_result(position, result)

    def finish_remaining(message):
        for position, task in enumerate(tasks):
            deliver(position, (None, f"第 {task[0]} 段 [{task[1]}]：{message}"))
        return results

    if not tasks:
        return results

    try:
        if cancel_check and cancel_check():
            return finish_remaining('任务已取消，未提交服务任务。')
        payload = {"voice_id": model_name, "title": "双语工作流配音",
                   "segments": [{"text": task[2], "timestamp": task[1]} for task in tasks],
                   "record_only": True, "delivery_ack": True}
        health = session.get(base + "/health", timeout=3)
        health_body = health.json() if health.status_code == 200 else {}
        capabilities = health_body.get("capabilities", [])
        if "workflow_record_only" not in capabilities:
            raise RuntimeError("当前声笺服务不支持工作流临时音频，请更新并重启声笺至 v0.7.11 或以上。")
        if workflow_context:
            if "workflow_grouping" not in capabilities:
                raise RuntimeError("当前声笺服务不支持工作流批次合并，请更新并重启声笺。")
            payload.update(workflow_key=workflow_context["key"], title=workflow_context["title"])
        response = session.post(base + "/jobs", json=payload, timeout=15)
        if response.status_code != 202:
            error = _error(response)
            return finish_remaining(error)
        submitted = response.json()
        job_id = submitted["job_id"]
        indices = submitted.get("segment_indices", list(range(1, len(tasks) + 1)))
        if len(indices) != len(tasks) or len(set(indices)) != len(indices):
            raise RuntimeError('服务返回的片段编号与提交内容不一致，未重新提交。')
        generation = submitted.get("generation")
        status_url = base + "/jobs/" + job_id + (f"?generation={generation}" if generation else "")
        incremental = bool(health_body.get('workflow_incremental') and generation)
        control_url = base + f'/jobs/{job_id}/generations/{generation}'
        if incremental:
            status_url = control_url + '/progress'
        paused = False
        pause_started = None
        deadline = time.monotonic() + timeout

        def cancel_submitted():
            cancel_url = base + "/jobs/" + job_id + (f"/generations/{generation}/cancel" if generation else "/cancel")
            stopped = session.post(cancel_url, timeout=15)
            if stopped.status_code != 200:
                raise RuntimeError("取消服务任务失败：" + _error(stopped))
            return finish_remaining('任务已取消；服务保留生成记录，未接收音频已清理。')

        while paused or time.monotonic() < deadline:
            if cancel_check and cancel_check():
                return cancel_submitted()
            desired = bool(pause_check and pause_check())
            if incremental and desired != paused:
                changed = session.post(control_url + ('/pause' if desired else '/resume'), timeout=15)
                if changed.status_code != 200:
                    raise RuntimeError('暂停 / 继续服务任务失败：' + _error(changed))
                if desired:
                    pause_started = time.monotonic()
                else:
                    deadline += time.monotonic() - pause_started
                    pause_started = None
                paused = desired
            result = session.get(status_url, timeout=10)
            if result.status_code != 200:
                raise RuntimeError(_error(result))
            job = result.json()
            terminal = job['status'] in ('done', 'failed', 'cancelled')
            by_index = {s['idx']: s for s in job['segments']}
            for position, (i, task) in enumerate(zip(indices, tasks)):
                if results[position] is not None:
                    continue
                if cancel_check and cancel_check():
                    return cancel_submitted()
                item = by_index.get(i)
                if item and item['status'] == 'done' and item.get('audio_url'):
                    deliver(position, (urljoin(base + '/', item['audio_url']), None))
                elif terminal or item and item['status'] in ('failed', 'cancelled'):
                    error = (item.get("error") or {}) if item else {}
                    message = error.get("message", "任务已取消或未返回该片段")
                    hint = error.get("hint", "")
                    detail = f"第 {task[0]} 段 [{task[1]}] {task[2][:40]}：{message}"
                    if hint:
                        detail += "；建议：" + hint
                    detail += f"；服务任务 {job_id}"
                    deliver(position, (None, detail))
                if incremental and results[position] is not None:
                    # Whole subtitle runs may last hours. Only a lack of new
                    # results should time out; repeated snapshots do not count.
                    deadline = (pause_started if paused else time.monotonic()) + timeout
            if terminal:
                return results
            time.sleep(0.3)
        return finish_remaining(f'等待工作流记录 {job_id} 超过 {timeout} 秒；服务仍在生成或排队，记录可通过接口查询。')
    except Exception as exc:
        return finish_remaining(f'本地配音服务请求失败：{exc}')


def generate_local_tasks(session, api_url, model_name, tasks, audio_cache, cache_model, filename, download,
                         cancel_check=None, workflow_context=None, on_result=None, pause_check=None):
    results = [None] * len(tasks)

    def receive(position, result):
        if results[position] is not None:
            return
        audio_url, error = result
        task = tasks[position]
        index, timestamp, text, save_dir, mtime = task
        if error:
            results[position] = False, error
        else:
            try:
                save_path = filename(index, timestamp, text, save_dir)
                save_validated_audio(download, audio_url, save_path)
                # Only acknowledge after both valid output and cache are committed.
                audio_cache.save_audio_cache(text, save_path, cache_model, api_url, source_version=str(int(mtime)))
                message = f'完成：{index:04d} [{timestamp}]'
                try:
                    released = session.post(audio_url + '/release', timeout=10)
                    if released.status_code != 200 or released.json().get('errors'):
                        raise RuntimeError('服务尚未确认清理')
                except Exception:
                    message += '；临时音频将在服务端到期清理'
                results[position] = True, message
            except Exception as exc:
                results[position] = False, f'第 {index} 段 [{timestamp}] 音频下载失败：{exc}'
        if on_result:
            on_result(position, results[position])

    returned = run_local_job(session, api_url, model_name, tasks, cancel_check=cancel_check,
        workflow_context=workflow_context, on_result=receive, pause_check=pause_check)
    for position, result in enumerate(returned):
        receive(position, result)
    return results
