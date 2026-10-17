"""Standalone optional worker. Heavy dependencies stay outside the desktop bundle."""
from collections import OrderedDict
from contextlib import redirect_stdout
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback


class QwenEngine:
    def __init__(self):
        self.model = None
        self.prompts = OrderedDict()
        self.predictor_path = None

    def run(self, message):
        import torch
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel

        settings = message["settings"]
        started = time.perf_counter()
        warm = self.model is not None
        if self.model is None:
            device = settings["device"]
            if device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("独立 Qwen 环境的 PyTorch 不支持当前 CUDA 设备")
            precision = settings["dtype"]
            if precision == "auto":
                precision = "float16" if device == "cuda" else "float32"
            if device == "cuda" and precision == "bfloat16" and not torch.cuda.is_bf16_supported():
                raise RuntimeError("当前显卡不支持 bfloat16，请选择自动或 float16")
            torch.set_num_threads(message.get("cpu_threads") or 8)
            self.model = Qwen3TTSModel.from_pretrained(settings["model_dir"],
                device_map="cuda:0" if device == "cuda" else "cpu", dtype=getattr(torch, precision),
                attn_implementation="sdpa", local_files_only=True)
            try:
                from .qwen_fastpath import install
            except ImportError:  # Direct script entry in the external Python environment.
                from qwen_fastpath import install
            self.predictor_path = install(self.model, settings.get('predictor_fastpath', True), device=device,
                                          cuda_graph=settings.get('predictor_cuda_graph', False))
        loaded = time.perf_counter()
        voice = message["voice"]
        key = (hashlib.sha256(Path(voice["reference_audio"]).read_bytes()).hexdigest(), voice["reference_text"])
        hit = key in self.prompts
        with torch.inference_mode():
            if hit:
                prompt = self.prompts.pop(key)
            else:
                prompt = self.model.create_voice_clone_prompt(ref_audio=voice["reference_audio"],
                    ref_text=voice["reference_text"] or None, x_vector_only_mode=not bool(voice["reference_text"]))
            self.prompts[key] = prompt
            while len(self.prompts) > settings["prompt_cache_slots"]:
                self.prompts.popitem(last=False)
            prompt_ready = time.perf_counter()
            if message.get("seed", -1) >= 0:
                torch.manual_seed(message["seed"])
            counts = self.predictor_path.snapshot()
            wavs, rate = self.model.generate_voice_clone(text=[i["text"] for i in message["items"]],
                language=[i["language"] for i in message["items"]], voice_clone_prompt=prompt,
                **message["options"])
            if len(wavs) != len(message["items"]):
                raise RuntimeError("批量后端返回的音频数量与请求不符")
            for item, audio in zip(message["items"], wavs):
                sf.write(item["output"], audio, rate, subtype="PCM_16", format="WAV")
        return {"ok": True, "count": len(wavs), "warm_model": warm, "prompt_cache_hit": hit,
                **self.predictor_path.status(counts),
                "load_seconds": loaded-started, "prompt_seconds": prompt_ready-loaded,
                "inference_seconds": time.perf_counter()-prompt_ready}


def main():
    protocol = sys.stdout
    engine = QwenEngine()
    while True:
        line = sys.stdin.readline(1048577)
        if not line:
            break
        try:
            if len(line) > 1048576:
                raise ValueError("批量请求过大")
            with redirect_stdout(sys.stderr):
                result = engine.run(json.loads(line))
        except Exception as exc:
            result = {"ok": False, "message": str(exc), "traceback": traceback.format_exc()}
        protocol.write(json.dumps(result, ensure_ascii=False) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
