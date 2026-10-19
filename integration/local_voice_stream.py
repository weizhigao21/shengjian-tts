"""Whole-workflow intake and per-result UI delivery for compatible local services."""
import shutil
import time

from .local_voice_client import generate_local_tasks, save_validated_audio


def supports_incremental(api):
    from .tts_utils import _get_session
    from .local_voice_client import is_local_service
    session = _get_session()
    if not is_local_service(session, api['url']):
        return False
    try:
        response = session.get(api['url'].rstrip('/') + '/health', timeout=3)
        return response.status_code == 200 and response.json().get('workflow_incremental') is True
    except Exception:
        return False


def run_incremental(worker, api, tasks, completed, completed_weight, started, task_id, task_dir):
    from .tts_profile import api_cache_model
    from .tts_utils import _get_session, generate_filename, _download_audio
    total, settled, failures = len(tasks), set(), 0
    model = api_cache_model(api['url'], api['model'], api.get('model_tag', ''))
    worker.log_signal.emit(f"[{api['name']}] 整任务入队，生成结果逐条接收；GPU 小批大小由声笺控制。")

    def report(position, result):
        nonlocal completed, completed_weight, failures
        if position in settled or worker.stop_flag and not result[0]:
            return
        settled.add(position)
        success, message = result
        failures += int(not success)
        completed += 1
        completed_weight += len(tasks[position][2])
        worker.log_signal.emit(f"[{api['name']}] {message}")
        worker.progress_signal.emit(completed)
        worker.weight_progress_signal.emit(completed_weight)
        # Match the existing worker's wall-clock ETA estimate.
        remaining = (time.time()-started) / len(settled) * (total-len(settled))
        from .tts_worker import format_eta
        worker.eta_signal.emit(format_eta(remaining))

    pending, positions = [], []
    for position, task in enumerate(tasks):
        if worker.stop_flag:
            break
        while worker.is_paused() and not worker.stop_flag:
            time.sleep(.1)
        if worker.stop_flag:
            break
        index, timestamp, text, save_dir, mtime = task
        cached = worker.audio_cache.get_cached_audio(text, model, str(int(mtime)))
        if cached:
            try:
                save_validated_audio(lambda _, target: shutil.copy2(cached, target), '',
                                     generate_filename(index, timestamp, text, save_dir))
                report(position, (True, f'缓存复用：{index:04d} [{timestamp}]'))
                continue
            except (OSError, ValueError, EOFError):
                pass
        pending.append(task)
        positions.append(position)
    # The API accepts at most 10,000 segments. Normal tasks use one submission;
    # larger subtitles are split at this intake limit, not at the GPU batch size.
    for start in range(0, len(pending), 10000):
        if worker.stop_flag:
            break
        group = pending[start:start+10000]
        result = generate_local_tasks(_get_session(), api['url'], api['model'], group,
            worker.audio_cache, model, generate_filename, _download_audio,
            cancel_check=lambda: worker.stop_flag, workflow_context=worker.workflow_context,
            pause_check=worker.is_paused,
            on_result=lambda index, value: report(positions[start+index], value))
        for index, value in enumerate(result):
            report(positions[start+index], value)
    abandoned = total-len(settled)
    worker.log_signal.emit('任务已取消，已下载的音频保留。' if worker.stop_flag else '全部任务完成')
    worker.log_signal.emit(f'任务ID: {task_id}')
    worker.log_signal.emit(f'任务文件保存位置: {task_dir}')
    if failures or abandoned:
        worker.log_signal.emit(f'语音生成失败或未处理 {failures+abandoned}/{total} 个片段')
    worker._finish_generation(not (failures or abandoned or worker.stop_flag or worker._read_failures), task_dir)
