from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import shutil

from .errors import VoiceError
from .runtime import project_root
from .data_paths import default_data_dir
from .index_emotion import EMOTION_DEFAULTS, EMOTION_FIELDS, emotion_mode

PROJECT_ROOT = project_root()
DEFAULT_DATA = default_data_dir()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def file_digest(path: str) -> str:
    source = Path(path)
    if not source.is_file():
        raise VoiceError("FILE_MISSING", f"文件不存在：{source}", "选择有效文件后保存声音。", "配置检查")
    digest = hashlib.sha256()
    with source.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def voice_fingerprint(voice: dict) -> str:
    value = {k: v for k, v in voice.items() if k not in ("name", "id", *EMOTION_FIELDS)}
    mode = emotion_mode(voice)
    # Default fields must not invalidate the version tags of existing presets.
    if mode == "speaker":
        value["emotion"] = ""
    elif mode in ("reference", "vector") or voice.get("emotion_infer_from_text") or voice.get("emotion_strength", .6) != .6:
        control = {"mode": mode, "strength": voice.get("emotion_strength", .6)}
        if mode == "reference":
            control["reference_digest"] = file_digest(voice.get("emotion_reference_audio", ""))
        elif mode == "vector":
            control["vector"] = voice.get("emotion_vector", [0.] * 8)
        elif voice.get("emotion_infer_from_text"):
            control["infer_from_text"] = True
            value["emotion"] = ""
        if mode != "text":
            value["emotion"] = ""
        value["emotion_control"] = control
    if voice.get("reference_audio"):
        value["reference_digest"] = file_digest(voice["reference_audio"])
    # 记录权重文件的大小与修改时间；模型 revision 可用于显式区分同路径的替换版本。
    root = Path(voice.get("model_dir") or ".")
    if voice.get("backend") == "audio_cpp":
        assets = [root, Path(voice.get("engine_executable") or ".")]
        value["native_assets"] = [(str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                  for p in assets if p.is_file()]
    if voice.get("backend") == "index_tts" and root.is_dir():
        value["model_files"] = [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
                                for p in sorted(root.rglob("*"))
                                if p.is_file() and p.suffix in (".pth", ".pt", ".safetensors", ".yaml", ".json")]
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ConfigStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.path = self.data_dir / "config.json"
        self.lock = threading.RLock()
        if not self.path.exists():
            atomic_json(self.path, {"voices": [{
                "id": "default", "name": "Windows 系统声音（联动验证）", "backend": "windows_sapi",
                "python": sys.executable, "sapi_voice": "", "revision": "1",
                "reference_audio": "", "repo_dir": "", "model_dir": "",
                "index_version": "2", "device": "cuda", "speed": 1.0,
                "emotion": "", "language": "zh", "timeout": 600,
            }]})
            self.import_yovoice()
        self.sync_references()

    def import_yovoice(self):
        from .discovery import discover_yovoice
        imported = discover_yovoice(self.data_dir)
        with self.lock:
            value = self.read()
            existing = {v["id"] for v in value["voices"]}
            removed = set(value.get("removed_models", []))
            additions = [v for v in imported if v["id"] not in existing and os.path.normcase(str(Path(v.get("model_dir") or ".").resolve())) not in removed]
            atomic_json(self.path, {**value, "voices": value["voices"] + additions})
        self.sync_references()
        return len(additions)

    def read(self):
        with self.lock:
            return json.loads(self.path.read_text(encoding="utf-8"))

    def performance(self):
        from .schemas import PerformanceSettings
        return PerformanceSettings(**self.read().get("performance", {})).model_dump()

    def save_performance(self, settings):
        with self.lock:
            atomic_json(self.path, {**self.read(), "performance": settings})

    def voices(self):
        value = self.read()
        voices = value["voices"]
        installed = {self.model_identity(v) for v in voices}
        templates = []
        from .model_info import model_ui
        for voice in value.get("model_templates", []):
            if self.model_identity(voice) in installed:
                continue
            template = dict(voice, is_template=True)
            info = model_ui(template)
            builtin = info.get("builtin_voices", [])
            if builtin and template.get("native_voice") not in builtin:
                template["native_voice"] = builtin[0]
            if info.get("languages") and template.get("language") not in info["languages"]:
                template["language"] = info["languages"][0]
            templates.append(template)
        return voices + templates

    @staticmethod
    def model_identity(voice):
        return json.dumps([voice["backend"], voice.get("family"),
            *(os.path.normcase(str(Path(voice.get(k) or ".").resolve())) for k in ("model_dir", "engine_executable", "repo_dir", "python")),
            voice.get("index_version") if voice["backend"] == "index_tts" else None])

    def save_voice(self, voice: dict):
        from .schemas import VoiceConfig
        parsed = VoiceConfig(**voice).model_dump()
        # Keep incomplete configurations editable; readable imports belong to our data directory.
        for field in ("reference_audio", "emotion_reference_audio"):
            if parsed.get(field) and Path(parsed[field]).is_file():
                target, _ = self.copy_reference_audio(parsed[field])
                parsed[field] = str(target)
        with self.lock:
            value = self.read()
            previous = next((v for v in value["voices"] if v["id"] == parsed["id"]), None)
            if previous and self.model_identity(previous) != self.model_identity(parsed):
                self.keep_model(value, previous)
            others = [v for v in value["voices"] if v["id"] != parsed["id"]]
            atomic_json(self.path, {**value, "voices": others + [parsed]})
        self.sync_references()
        return parsed

    def keep_model(self, value, voice):
        identity = self.model_identity(voice)
        templates = value.setdefault("model_templates", [])
        if not any(self.model_identity(v) == identity for v in templates):
            templates.append({**voice, "id": "model-" + hashlib.sha256(identity.encode()).hexdigest()[:20], "name": "默认模型设置",
                              "reference_audio": "", "reference_text": "", "voice_mode": "auto", "voice_description": "", "emotion": "", "speed": 1, **EMOTION_DEFAULTS})

    def delete_voice(self, voice_id):
        with self.lock:
            value = self.read()
            voice = next((v for v in value["voices"] if v["id"] == voice_id), None)
            if voice is None:
                raise VoiceError("VOICE_NOT_FOUND", "声音预设不存在", stage="声音删除")
            self.keep_model(value, voice)
            deleted = {**value.get("deleted_voices", {}), voice_id: voice}
            atomic_json(self.path, {**value, "voices": [v for v in value["voices"] if v["id"] != voice_id],
                                   "deleted_voices": deleted})

    def restore_voice(self, voice_id):
        with self.lock:
            value = self.read()
            deleted = value.get("deleted_voices", {})
            voice = deleted.pop(voice_id, None)
            if voice is None:
                raise VoiceError("VOICE_NOT_FOUND", "找不到已删除的声音", stage="声音恢复")
            if voice.get("model_dir") and os.path.normcase(str(Path(voice["model_dir"]).resolve())) in value.get("removed_models", []):
                raise VoiceError("MODEL_REMOVED", "此预设的模型已移除", "先在模型管理中重新添加模型，再恢复预设。", "声音恢复")
            value["voices"] = [v for v in value["voices"] if v["id"] != voice_id] + [voice]
            atomic_json(self.path, value)
        return voice

    @staticmethod
    def reference_id(path):
        return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()[:24]

    def sync_references(self):
        with self.lock:
            value = self.read()
            references = list(value.get("references", []))
            existing = {r["id"] for r in references} | set(value.get("hidden_references", []))
            for voice in value["voices"]:
                for field in ("reference_audio", "emotion_reference_audio"):
                    path = voice.get(field)
                    if path and (ref_id := self.reference_id(path)) not in existing:
                        references.append({"id": ref_id, "name": ("演绎参考 " if field == "emotion_reference_audio" else "参考声音 ") + str(len(references) + 1),
                                           "reference_audio": path, "reference_text": voice.get("reference_text", "") if field == "reference_audio" else ""})
                        existing.add(ref_id)
            if references != value.get("references"):
                atomic_json(self.path, {**value, "references": references})

    def references(self):
        from .runner import validate_wave
        result = []
        for item in self.read().get("references", []):
            try:
                result.append({**item, **validate_wave(Path(item["reference_audio"])), "error": None})
            except VoiceError as exc:
                result.append({**item, "duration": 0, "sample_rate": 0, "error": exc.as_dict()})
        return result

    def copy_reference_audio(self, path):
        """Retain a validated WAV beside config.json, using its content as the file name."""
        from .runner import validate_wave
        source = Path(path).resolve()
        folder = self.data_dir / "voices"
        staged = None
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if source.parent == folder:
                target = source
            else:
                if not source.is_file():
                    raise VoiceError("FILE_MISSING", f"参考音频不存在：{source}", "重新选择参考 WAV。", "声音导入")
                with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temporary:
                    staged = Path(temporary.name)
                shutil.copyfile(source, staged)
                target = folder / (file_digest(str(staged)) + ".wav")
            data = validate_wave(staged or target)
            if not 1 <= data["duration"] <= 60:
                raise VoiceError("REFERENCE_DURATION", "参考声音需要 1–60 秒的 WAV", stage="声音导入")
            if staged:
                os.replace(staged, target)
            return target, data
        except OSError as exc:
            raise VoiceError("REFERENCE_COPY_FAILED", f"参考音频保存失败：{exc}",
                             f"检查程序数据目录的写入权限和剩余空间：{folder}", "声音导入") from exc
        finally:
            if staged:
                staged.unlink(missing_ok=True)

    def import_reference(self, item):
        if not item["name"].strip():
            raise VoiceError("EMPTY_NAME", "声音名称不能为空", stage="声音导入")
        target, data = self.copy_reference_audio(item["reference_audio"])
        ref_id = self.reference_id(target)
        parsed = {"id": ref_id, "name": item["name"].strip(), "reference_audio": str(target),
                  "reference_text": item.get("reference_text", "").strip()}
        with self.lock:
            value = self.read()
            references = [r for r in value.get("references", []) if r["id"] != ref_id] + [parsed]
            atomic_json(self.path, {**value, "references": references,
                                   "hidden_references": [r for r in value.get("hidden_references", []) if r != ref_id]})
        return {**parsed, **data, "error": None}

    def edit_reference(self, ref_id, item):
        if not item["name"].strip():
            raise VoiceError("EMPTY_NAME", "声音名称不能为空", stage="声音编辑")
        with self.lock:
            value = self.read()
            reference = next((r for r in value.get("references", []) if r["id"] == ref_id), None)
            if reference is None:
                raise VoiceError("REFERENCE_NOT_FOUND", "参考声音不存在", stage="声音编辑")
            reference.update(name=item["name"].strip(), reference_text=item.get("reference_text", "").strip())
            atomic_json(self.path, value)
        return reference

    def delete_reference(self, ref_id):
        with self.lock:
            value = self.read()
            reference = next((r for r in value.get("references", []) if r["id"] == ref_id), None)
            if reference is None:
                raise VoiceError("REFERENCE_NOT_FOUND", "参考声音不存在", stage="声音删除")
            atomic_json(self.path, {**value, "references": [r for r in value.get("references", []) if r["id"] != ref_id],
                                   "deleted_references": {**value.get("deleted_references", {}), ref_id: reference},
                                   "hidden_references": list(set(value.get("hidden_references", [])) | {ref_id})})

    def restore_reference(self, ref_id):
        with self.lock:
            value = self.read()
            reference = value.get("deleted_references", {}).pop(ref_id, None)
            if reference is None:
                raise VoiceError("REFERENCE_NOT_FOUND", "找不到已删除的参考声音", stage="声音恢复")
            value["references"] = [r for r in value.get("references", []) if r["id"] != ref_id] + [reference]
            value["hidden_references"] = [r for r in value.get("hidden_references", []) if r != ref_id]
            atomic_json(self.path, value)
        return reference

    def resolve(self, model_name: str, allow_missing_reference=False):
        voice_id, _, requested_version = model_name.partition("@")
        voice = next((v for v in self.voices() if v["id"] == voice_id), None)
        if voice is None:
            raise VoiceError("VOICE_NOT_FOUND", f"声音不存在：{voice_id}", "从声音库复制正确的模型名称。", "配置检查")
        try:
            digest = voice_fingerprint(voice)
        except VoiceError as exc:
            # An unversioned preset can be repaired by replacing its missing reference at submission.
            if not requested_version and allow_missing_reference and exc.code == "FILE_MISSING":
                digest = ""
            else:
                raise
        if requested_version and requested_version != digest[:12]:
            raise VoiceError("VOICE_VERSION_CHANGED", "声音配置已变化，工作流仍使用旧版本", "重新复制模型名称和模型标签到双语工作流。", "配置检查")
        return voice, digest
