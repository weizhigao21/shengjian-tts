"""Backend configuration, readiness, batch selection and observable fallback."""
from collections import OrderedDict
import json
import os
from pathlib import Path
import subprocess
import struct
import time

from ..config import atomic_json
from ..errors import VoiceError
from ..runtime import external_environment
from ..model_info import _read_package
from .policy import group_key, model_signature, unsupported
from .process import BatchProcess
from .schemas import BatchSettings


class BatchBackend:
    def __init__(self, manager):
        self.manager = manager
        self.settings = BatchSettings(**manager.config.read().get("batch_inference", {}))
        self.process = BatchProcess(manager.root)
        self.failures = OrderedDict()
        self.last_result = None
        self.reason, self.versions = self.diagnose()
        from .provisioning import BatchSetup
        self.setup = BatchSetup(self)

    def diagnose(self, settings=None):
        settings = settings or self.settings
        if not settings.enabled:
            return "自动批量推理已关闭。", {}
        if not all((settings.python, settings.model_dir, settings.source_model)):
            return "点击「一键安装并关联」自动准备环境与模型，也可在高级配置中选择已有文件。", {}
        python = Path(settings.python)
        if not python.is_file() or python.name.lower() in ("声笺.exe", "声笺服务.exe"):
            return "独立 Python 不可用，可点击「下载运行环境」自动准备，或在高级配置中重新选择。", {}
        model = Path(settings.model_dir)
        try:
            info = json.loads((model / "config.json").read_text(encoding="utf-8"))
            if info.get("model_type") != "qwen3_tts" or info.get("tts_model_type") != "base":
                return "批量模型目录需要 Qwen3-TTS Base 原始权重，不支持 GGUF 或其他变体。", {}
            if not any(model.glob("*.safetensors")) or not any((model / "speech_tokenizer").glob("*.safetensors")):
                return "原始模型或 speech_tokenizer 权重不完整，请完成模型安装。", {}
            if not Path(settings.source_model).is_file():
                return "关联的 GGUF 文件不存在。", {}
            source = Path(settings.source_model)
            stat = source.stat()
            package = _read_package(str(source.resolve()), stat.st_size, stat.st_mtime_ns)
            native = package.get('files', {}).get('config.json', {})
            if package.get('family') != 'qwen3_tts' or native.get('tts_model_type') != 'base':
                return '关联的 GGUF 需要是 Qwen3-TTS Base 模型。', {}
            for key in ('hidden_size', 'num_hidden_layers', 'num_attention_heads'):
                expected = info.get('talker_config', {}).get(key)
                actual = native.get('talker_config', {}).get(key)
                if expected is not None and actual is not None and expected != actual:
                    return '原始模型与 GGUF 的架构规格不同，请关联相同规格的 Base 模型。', {}
            return self.diagnose_python(str(python))
        except (OSError, ValueError, struct.error, subprocess.TimeoutExpired) as exc:
            return "批量环境尚未就绪：" + str(exc), {}

    def diagnose_python(self, python):
        if not python or not Path(python).is_file() or Path(python).name.lower() in ('声笺.exe', '声笺服务.exe'):
            return '独立 Python 环境尚未安装。', {}
        code = ("import importlib.util,importlib.metadata as m,json; "
                "names=('torch','qwen_tts','soundfile','transformers'); "
                "missing=[n for n in names if importlib.util.find_spec(n) is None]; "
                "versions={n:m.version('qwen-tts' if n=='qwen_tts' else n) for n in names if n not in missing}; "
                "print(json.dumps({'missing':missing,'versions':versions}))")
        try:
            with external_environment() as environment:
                result = subprocess.run([python, '-c', code], capture_output=True, text=True,
                    encoding='utf-8', timeout=10, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            details = json.loads(result.stdout) if result.returncode == 0 else {'missing': ['环境无法启动']}
            missing = details['missing']
            return ('独立环境缺少：' + ', '.join(missing) if missing else ''), details.get('versions', {})
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return '独立环境检查失败：' + str(exc), {}

    def configure(self, settings, from_setup=False):
        # Validate dependencies outside the scheduler lock; never load weights here.
        with self.manager.lock:
            if self.setup.busy and not from_setup:
                raise VoiceError('DOWNLOAD_BUSY', '请先完成或暂停批量安装，再修改高级配置', stage='批量设置')
            self.manager.require_idle_engine("批量推理设置")
        reason, versions = self.diagnose(settings)
        with self.manager.lock:
            if from_setup:
                self.setup.check_cancel()
            self.manager.require_idle_engine("批量推理设置")
            self.manager.release_runners()
            with self.manager.config.lock:
                atomic_json(self.manager.config.path, {**self.manager.config.read(), "batch_inference": settings.model_dump()})
            self.settings, self.reason = settings, reason
            self.versions = versions
            self.failures.clear()
            self.last_result = None
        return self.status()

    def status(self, compact=False):
        process = self.process.process
        reason = '批量环境正在检测或安装，暂时使用原生引擎。' if self.setup.busy else self.reason
        result = {"backend": "qwen3_batch", "ready": not bool(reason),
                "reason": reason, "loaded": bool(process and process.poll() is None and self.process.loaded),
                "last_result": self.last_result}
        if not compact:
            result.update(settings=self.settings.model_dump(), versions=self.versions,
                fallbacks=[{"job_id": key[0], "generation": key[1], "reason": value} for key, value in self.failures.items()])
        return result

    def fallback(self, job, item, reason):
        key = (job["id"], item["generation"])
        self.failures[key] = reason
        while len(self.failures) > 128:
            self.failures.popitem(last=False)

    def native_metadata(self, job_id, item):
        reason = self.failures.get((job_id, item["generation"]))
        if not reason and item.get('inference_mode') == 'batch':
            reason = self.status()['reason'] or unsupported(item['voice'], item['text'], self.settings) or '没有足够的兼容文本可合批。'
        return {"backend": "native", "fallback_reason": reason} if reason else {"backend": "native"}

    def select(self, store, job, item):
        if (self.reason or self.setup.busy or item.get("inference_mode") == "native"
                or (job["id"], item["generation"]) in self.failures):
            return []
        try:
            if unsupported(item["voice"], item["text"], self.settings):
                return []
            key = group_key(item["voice"])
            def compatible(candidate):
                return (candidate.get("inference_mode") != "native"
                        and not unsupported(candidate["voice"], candidate["text"], self.settings)
                        and group_key(candidate["voice"]) == key)
            batch = store.claim_batch(job, item, compatible, self.settings.max_batch_size, self.settings.max_batch_chars)
            # Finish a trailing row on the already loaded model, avoiding a costly
            # switch to GGUF solely because a micro-batch has an odd row count.
            warm = self.status()['loaded'] and self.process.key == model_signature(self.settings, self.versions)
            return batch or ([item] if warm else [])
        except (OSError, ValueError, VoiceError) as exc:
            self.fallback(job, item, str(exc))
            return []

    def expire(self):
        if time.monotonic() - self.process.last_used >= self.settings.idle_seconds:
            self.process.interrupt()

    def plan(self, voice, texts):
        reason = self.status()['reason'] or next((r for text in texts if (r := unsupported(voice, text, self.settings))), "")
        if len(texts) < 2 and not self.status()['loaded']:
            reason = "单条文本使用现有引擎。"
        sizes, size, chars = [], 0, 0
        if not reason:
            for text in texts:
                if size >= self.settings.max_batch_size or chars + len(text) > self.settings.max_batch_chars:
                    sizes.append(size)
                    size, chars = 0, 0
                size += 1
                chars += len(text)
            if size:
                sizes.append(size)
            if max(sizes) < 2 and not self.status()['loaded']:
                reason = '当前长度限制下无法合并两条文本，使用现有引擎。'
        return {"backend": "native" if reason else "qwen3_batch", "reason": reason,
                "batch_sizes": sizes, "single_count": sizes.count(1) if not reason else len(texts),
                "model_loaded": self.status()["loaded"], "requires_setup": bool(self.reason) or self.setup.busy}
