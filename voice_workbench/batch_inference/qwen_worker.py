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
        self.talker_path = None
        self.precision = ''
        self.precision_reason = ''

    # A NaN in fp16 arrives as an asynchronous CUDA error three kernels later, so
    # the message alone never tells a user what to change.
    NUMERIC_SIGNS = ('device-side assert', 'CUBLAS_STATUS', 'CUDA error', 'NaN')

    def explain(self, exc):
        message = str(exc)
        if self.precision == 'float16' and any(sign in message for sign in self.NUMERIC_SIGNS):
            return (message + '\n这个模型在 FP16 下数值溢出（0.6B Base 已知如此），'
                    '请在「批量性能」把计算精度改成 BF16 再试；BF16 数值更稳，但在这张显卡上会更慢。')
        return message

    def run(self, message, progress=None):
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
                # Turing has no bf16 tensor cores and PyTorch runs these kernels
                # through slower paths, but the results are correct; refusing the
                # choice would block the only precision that renders some models.
                self.precision_reason = '这张显卡没有原生 BF16 支持，生成会比 FP16 慢，但数值更稳定。'
            torch.set_num_threads(message.get("cpu_threads") or 8)
            self.model = Qwen3TTSModel.from_pretrained(settings["model_dir"],
                device_map="cuda:0" if device == "cuda" else "cpu", dtype=getattr(torch, precision),
                attn_implementation="sdpa", local_files_only=True)
            self.precision = precision
            try:
                from .qwen_fastpath import install
            except ImportError:  # Direct script entry in the external Python environment.
                from qwen_fastpath import install
            self.predictor_path = install(self.model, settings.get('predictor_fastpath', True), device=device,
                                          cuda_graph=settings.get('predictor_cuda_graph', False))
            try:
                from .qwen_talker_graph import install as install_talker
            except ImportError:
                from qwen_talker_graph import install as install_talker
            self.talker_path = install_talker(self.model, settings.get('talker_graph', False), device=device)
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
            talker_counts = self.talker_path.snapshot()
            # Between batches, not between frames: a batch that starts near the
            # driver's VRAM limit pays 1.2 s per frame in WDDM paging instead of
            # 0.09 s, and only handing the pooled blocks back fixes it. Runs after
            # the snapshot so this batch reports its own reclamation.
            self.predictor_path.tidy()
            self.predictor_path.progress = progress
            try:
                wavs, rate = self.model.generate_voice_clone(text=[i["text"] for i in message["items"]],
                    language=[i["language"] for i in message["items"]], voice_clone_prompt=prompt,
                    **message["options"])
            except Exception as exc:
                raise RuntimeError(self.explain(exc)) from exc
            finally:
                self.predictor_path.progress = None
            if len(wavs) != len(message["items"]):
                raise RuntimeError("批量后端返回的音频数量与请求不符")
            for item, audio in zip(message["items"], wavs):
                sf.write(item["output"], audio, rate, subtype="PCM_16", format="WAV")
        return {"ok": True, "count": len(wavs), "warm_model": warm, "prompt_cache_hit": hit,
                "precision": self.precision, "precision_reason": self.precision_reason,
                **self.predictor_path.status(counts),
                **self.talker_path.status(talker_counts),
                "load_seconds": loaded-started, "prompt_seconds": prompt_ready-loaded,
                "inference_seconds": time.perf_counter()-prompt_ready}


def main():
    protocol = sys.stdout
    engine = QwenEngine()
    while True:
        line = sys.stdin.readline(1048577)
        if not line:
            break
        frames = 0

        def report():
            # Frames arrive every 0.15-1.2 s. Reporting each one lets the parent
            # distinguish "this machine is slow" from "this batch is wedged", which
            # a total-time budget cannot: a slow GPU used to be killed and retried
            # one line at a time, which is slower than the wait it avoided.
            nonlocal frames
            frames += 1
            protocol.write(json.dumps({"progress": frames}) + "\n")
            protocol.flush()

        try:
            if len(line) > 1048576:
                raise ValueError("批量请求过大")
            with redirect_stdout(sys.stderr):
                result = engine.run(json.loads(line), progress=report)
            result["frames"] = frames
        except Exception as exc:
            result = {"ok": False, "message": str(exc), "traceback": traceback.format_exc()}
        protocol.write(json.dumps(result, ensure_ascii=False) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
