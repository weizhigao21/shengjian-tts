"""将兼容适配器接入指定工作流。精确替换、先备份，重复运行不会重复修改。"""
import argparse
import ast
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parent.parent


def replace_once(source, old, new):
    if new in source:
        return source
    if source.count(old) != 1:
        raise RuntimeError("工作流源码与适配器预期不一致，未修改任何文件。")
    return source.replace(old, new, 1)


def voice_picker_installed(source):
    """Recognize complete discovery wiring independently of panel layout text."""
    tree = ast.parse(source)
    imported = any(isinstance(n, ast.ImportFrom) and n.module == "local_voice_picker"
                   and any(a.name == "VoicePicker" and not a.asname for a in n.names) for n in tree.body)
    if not imported:
        return False
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        initialized = any(isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                                  and t.value.id == "self" and t.attr == "local_voice_picker" for t in n.targets)
                          and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
                          and n.value.func.id == "VoicePicker" for n in ast.walk(cls))
        synced = set()
        for name in ("_refresh_api_combo", "_on_api_changed"):
            if name not in methods:
                continue
            for n in ast.walk(methods[name]):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "sync_from_config" and isinstance(n.func.value, ast.Attribute)
                        and n.func.value.attr == "local_voice_picker" and isinstance(n.func.value.value, ast.Name)
                        and n.func.value.value.id == "self"):
                    synced.add(name)
        if initialized and synced == {"_refresh_api_combo", "_on_api_changed"}:
            return True
    return False


def prepare(target):
    steps = target / "src" / "steps"
    utils_path = steps / "tts_utils.py"
    bridge_path = steps / "step2_tts.py"
    worker_path = steps / "tts_worker.py"
    original_utils = utils_path.read_text(encoding="utf-8-sig")
    original_utils = original_utils.replace(', workflow_context=None', '').replace(', workflow_context=workflow_context', '')
    original_bridge = bridge_path.read_text(encoding="utf-8-sig")
    # Upgrade the first discovery adapter without duplicating its import or cancellation guard.
    original_bridge = original_bridge.replace('from .local_voice_client import resolve_workflow_config\n', '')
    original_bridge = original_bridge.replace('cfg = resolve_workflow_config(self.config.tts_cfg)', 'cfg = prepare_workflow_config(self.config)')
    original_bridge = original_bridge.replace('cfg = prepare_workflow_config(self.config.tts_cfg)', 'cfg = prepare_workflow_config(self.config)')
    original_worker = worker_path.read_text(encoding="utf-8-sig")
    original_worker = original_worker.replace('                                workflow_context=self.workflow_context,\n', '')
    utils = replace_once(original_utils, "from .tts_profile import api_cache_model", "from .tts_profile import api_cache_model\nfrom .local_voice_client import is_local_service, generate_local_tasks")
    utils = replace_once(utils, 'def tts_task(index, timestamp, text, api_url, model_name, save_dir, audio_cache, source_mtime=0, model_tag=""):',
                         'def tts_task(index, timestamp, text, api_url, model_name, save_dir, audio_cache, source_mtime=0, model_tag="", cancel_check=None):')
    utils = replace_once(utils, 'def tts_bulk_task(tasks, api_url, model_name, audio_cache, model_tag=""):',
                         'def tts_bulk_task(tasks, api_url, model_name, audio_cache, model_tag="", cancel_check=None):')
    single_marker = '    api_base_url = api_url.rstrip("/")\n    api_endpoint = f"{api_base_url}/infer_single"'
    single_new = '''    if is_local_service(_get_session(), api_url):
        task = (index, timestamp, text, save_dir, source_mtime)
        return generate_local_tasks(_get_session(), api_url, model_name, [task],
                                    audio_cache, cache_model, generate_filename, _download_audio,
                                    cancel_check=cancel_check)[0]

''' + single_marker
    utils = replace_once(utils, single_marker, single_new)
    bulk_marker = '    api_base_url = api_url.rstrip("/")\n    api_endpoint = f"{api_base_url}/infer_bulk"'
    bulk_new = '''    if is_local_service(_get_session(), api_url):
        local_results = generate_local_tasks(_get_session(), api_url, model_name, uncached_tasks,
                                             audio_cache, cache_model, generate_filename, _download_audio,
                                             cancel_check=cancel_check)
        for index, result in zip(uncached_indices, local_results):
            results[index] = result
        return results

''' + bulk_marker
    utils = replace_once(utils, bulk_marker, bulk_new)
    # Upgrade the installed adapter as well as an untouched workflow source.
    if utils.count('model_tag="", cancel_check=None):') != 2:
        raise RuntimeError("工作流语音函数签名与预期不一致，未修改任何文件。")
    utils = utils.replace('model_tag="", cancel_check=None):', 'model_tag="", cancel_check=None, workflow_context=None):')
    utils = utils.replace('cancel_check=cancel_check)[0]', 'cancel_check=cancel_check, workflow_context=workflow_context)[0]')
    utils = utils.replace('cancel_check=cancel_check)\n        for index', 'cancel_check=cancel_check, workflow_context=workflow_context)\n        for index')
    bridge = replace_once(original_bridge, '        self.result = (False, "未执行")', '        self.result = (False, "未执行")\n        self._last_tts_error = ""')
    bridge = replace_once(bridge, 'from .tts_profile import dir_name',
                         'from .tts_profile import dir_name\nfrom .local_voice_client import prepare_workflow_config')
    bridge = replace_once(bridge, '        cfg = self.config.tts_cfg\n',
                         '        cfg = prepare_workflow_config(self.config)\n')
    bridge = replace_once(bridge, '    def stop(self):\n        if self._tts_worker:',
                         '    def stop(self):\n        self.requestInterruption()\n        if self._tts_worker:')
    marker = '        cfg = prepare_workflow_config(self.config)\n'
    bridge = replace_once(bridge, marker, marker + '        if self.isInterruptionRequested():\n            self._emit_finished(False, "语音生成已取消")\n            return\n')
    bridge = replace_once(bridge, '        self._tts_worker.log_signal.connect(\n            self.log_signal.emit, Qt.ConnectionType.DirectConnection\n        )',
                         '        self._tts_worker.log_signal.connect(\n            self._on_tts_log, Qt.ConnectionType.DirectConnection\n        )')
    marker = '    def _on_finished(self, success: bool):'
    replacement = '''    def _on_tts_log(self, message: str):
        # 保留有片段位置的原始错误，让任务失败信息直接可定位。
        if any(word in message for word in ("失败", "错误", "异常", "超时", "连接失败", "：本地配音")):
            if "第 " in message or not self._last_tts_error:
                self._last_tts_error = message
        self.log_signal.emit(message)

''' + marker
    bridge = replace_once(bridge, marker, replacement)
    bridge = replace_once(bridge, '            self._emit_finished(False, "TTS 合成失败，详见日志")',
                         '            self._emit_finished(False, self._last_tts_error or "TTS 合成失败，详见日志")')
    call_marker = '                                api_config.get("model_tag", ""),\n                            )'
    updated_call = '                                api_config.get("model_tag", ""),\n                                cancel_check=lambda: self.stop_flag,\n                            )'
    worker = original_worker
    if updated_call not in worker:
        if worker.count(call_marker) != 2:
            raise RuntimeError("工作流调用位置与预期不一致，未修改任何文件。")
        worker = worker.replace(call_marker, updated_call)
    worker = replace_once(worker, 'import time\n', 'import time\nimport uuid\n')
    marker = '        self.pause_lock = threading.Lock()\n'
    worker = replace_once(worker, marker, marker + '        self.workflow_context = {"key": uuid.uuid4().hex,\n                                 "title": "双语配音 · " + os.path.splitext(os.path.basename(config.get("source_name") or (config.get("lrc_files") or ["字幕"])[0]))[0][:150]}\n')
    worker = worker.replace('cancel_check=lambda: self.stop_flag,\n                            )', 'cancel_check=lambda: self.stop_flag,\n                                workflow_context=self.workflow_context,\n                            )')
    panel_path = target / "src" / "widgets" / "tts_panel.py"
    panel = panel_path.read_text(encoding="utf-8-sig")
    if not voice_picker_installed(panel):
        panel = replace_once(panel, 'from .common import EDGE_TTS_VOICES, RATE_PRESETS, VOLUME_PRESETS',
                             'from .common import EDGE_TTS_VOICES, RATE_PRESETS, VOLUME_PRESETS\nfrom .local_voice_picker import VoicePicker')
        marker = '        # 用 QStackedWidget 切换 edge/api 面板，避免窗口被撑大后不缩小'
        panel = replace_once(panel, marker, '        self.local_voice_picker = VoicePicker(self)\n        api_layout.addWidget(self.local_voice_picker, 4, 0, 1, 4)\n\n' + marker)
        marker = '        self.api_combo.blockSignals(False)\n\n    def _on_mode_changed(self):'
        panel = replace_once(panel, marker,
                             '        self.api_combo.blockSignals(False)\n        self.local_voice_picker.sync_from_config()\n\n    def _on_mode_changed(self):')
        marker = '        self.config.tts_cfg["current_api_index"] = max(0, idx)\n        self._save()\n'
        panel = replace_once(panel, marker, marker + '        self.local_voice_picker.sync_from_config()\n')
    modifications = [(utils_path, utils), (bridge_path, bridge), (worker_path, worker), (panel_path, panel),
                     (steps / "local_voice_client.py", (ROOT / "integration" / "local_voice_client.py").read_text(encoding="utf-8")),
                     (target / "src" / "widgets" / "local_voice_picker.py", (ROOT / "integration" / "local_voice_picker.py").read_text(encoding="utf-8"))]
    for path, source in modifications:
        compile(source, str(path), "exec")
    return modifications


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=Path(r"G:\code\双语音声工作流"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    modifications = prepare(args.target)
    if args.prepare_only:
        for path, source in modifications:
            target = ROOT / "integration" / "prepared" / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")
        print("适配源码已准备并通过语法检查。")
        return
    backup = ROOT / "integration" / "backups" / time.strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for path, source in modifications:
        if path.exists():
            shutil.copy2(path, backup / path.name)
        path.write_text(source, encoding="utf-8", newline="\n")
    print("工作流适配已安装；备份：", backup)


if __name__ == "__main__":
    main()
