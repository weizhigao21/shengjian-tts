"""Short-lived UI validation cache; generation still validates on the service."""
from collections import OrderedDict
import json
from pathlib import Path
from time import monotonic


class VoiceCheckCache:
    def __init__(self, ttl=2, limit=64):
        self.ttl, self.limit = ttl, limit
        self.results = OrderedDict()

    @staticmethod
    def key(voice):
        paths = {Path(voice[k]) for k in ("python", "engine_executable", "model_dir", "reference_audio",
                 "emotion_reference_audio") if voice.get(k)}
        if voice.get("engine_executable") and voice.get("family"):
            paths.add(Path(voice["engine_executable"]).parent / "model_specs" / (voice["family"] + ".json"))
        if voice.get("backend") == "index_tts":
            module = "infer_v2.py" if voice.get("index_version") == "2" else "infer_v2_5.py"
            paths.add(Path(voice.get("repo_dir") or "") / "indextts" / module)
            paths.add(Path(voice.get("model_dir") or "") / "config.yaml")
        versions = []
        for path in sorted(paths):
            try:
                stat = path.stat()
                version = stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
            except OSError:
                version = None
            versions.append((str(path), version))
        return json.dumps(voice, sort_keys=True, ensure_ascii=False), tuple(versions)

    def problem(self, voice, check):
        key, now = self.key(voice), monotonic()
        cached = self.results.get(key)
        if cached and now - cached[0] < self.ttl:
            self.results.move_to_end(key)
            return cached[1]
        try:
            check(voice)
            problem = None
        except Exception as exc:
            problem = str(exc)
        self.results[key] = now, problem
        self.results.move_to_end(key)
        while len(self.results) > self.limit:
            self.results.popitem(last=False)
        return problem
