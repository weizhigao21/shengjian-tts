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
        self.digests = OrderedDict()
        self.predictor_path = None
        self.talker_path = None
        self.precision = ''
        self.precision_reason = ''
        # Why the last prompt cache write failed, if it did. The write is best
        # effort -- a full disk must not fail a batch -- but swallowing the reason
        # entirely would make "the cache never works" impossible to diagnose.
        self.prompt_error = ''

    # A NaN in fp16 arrives as an asynchronous CUDA error three kernels later, so
    # the message alone never tells a user what to change.
    NUMERIC_SIGNS = ('device-side assert', 'CUBLAS_STATUS', 'CUDA error', 'NaN')

    def explain(self, exc):
        message = str(exc)
        if self.precision == 'float16' and any(sign in message for sign in self.NUMERIC_SIGNS):
            return (message + '\n这个模型在 FP16 下数值溢出（0.6B Base 已知如此），'
                    '请在「批量性能」把计算精度改成 BF16 再试；BF16 数值更稳，但在这张显卡上会更慢。')
        return message

    def digest(self, path):
        """sha256 of the reference clip, keyed by what would change it.

        Every micro-batch re-read the same reference WAV to rebuild the prompt
        cache key. Reading and hashing it again says nothing new -- the bytes
        cannot change without the size or the mtime changing first -- so the
        cost is kept per process instead of per batch.
        """
        source = Path(path)
        try:
            info = source.stat()
        except OSError:
            return ''
        key = (str(source), info.st_size, info.st_mtime_ns)
        hit = self.digests.get(key)
        if hit is None:
            hit = hashlib.sha256(source.read_bytes()).hexdigest()
            self.digests[key] = hit
            while len(self.digests) > 8:
                self.digests.popitem(last=False)
        return hit

    # A prompt item is two tensors and three scalars: the reference clip already
    # encoded. Building it costs 1.2 s on this machine and answers identically
    # every time, yet it lived only in memory, so every unload and every process
    # restart paid it again. On disk it survives both.
    PROMPT_VERSION = 1
    PROMPT_FILES = 24

    def prompt_path(self, message, key):
        root = message.get("root")
        if not root:
            return None
        name = hashlib.sha256("|".join(str(part) for part in key).encode("utf-8")).hexdigest()[:32]
        return Path(root) / "runtime" / "prompt-cache" / f"v{self.PROMPT_VERSION}-{name}.pt"

    def load_prompt(self, path):
        """Rebuild the items from disk, or None if anything at all is off."""
        if path is None or not path.is_file():
            return None
        try:
            import torch
            from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem
            saved = torch.load(path, map_location="cpu", weights_only=True)
            if saved.get("version") != self.PROMPT_VERSION:
                return None
            items = []
            for raw in saved["items"]:
                code, embedding = raw.get("ref_code"), raw["ref_spk_embedding"]
                items.append(VoiceClonePromptItem(
                    # Restore the exact device the model produced: the tokenizer
                    # may hand codes back on the card while the speaker encoder
                    # answers on the host, and a guess here changes behaviour.
                    ref_code=code.to(raw.get("ref_code_device") or "cpu") if code is not None else None,
                    ref_spk_embedding=embedding.to(raw.get("ref_embedding_device") or "cpu"),
                    x_vector_only_mode=bool(raw["x_vector_only_mode"]),
                    icl_mode=bool(raw["icl_mode"]),
                    ref_text=raw.get("ref_text")))
            return items
        except Exception:
            # A file from another torch build must never fail a batch: rebuilding
            # the prompt is always a valid answer.
            return None

    def save_prompt(self, path, prompt):
        if path is None:
            return
        try:
            import torch
            path.parent.mkdir(parents=True, exist_ok=True)
            torch.save({"version": self.PROMPT_VERSION, "items": [{
                "ref_code": item.ref_code.detach().cpu() if item.ref_code is not None else None,
                "ref_code_device": str(item.ref_code.device) if item.ref_code is not None else None,
                "ref_spk_embedding": item.ref_spk_embedding.detach().cpu(),
                "ref_embedding_device": str(item.ref_spk_embedding.device),
                "x_vector_only_mode": item.x_vector_only_mode, "icl_mode": item.icl_mode,
                "ref_text": item.ref_text} for item in prompt]}, path)
            kept = sorted(path.parent.glob(f"v{self.PROMPT_VERSION}-*.pt"),
                          key=lambda file: file.stat().st_mtime, reverse=True)
            for stale in kept[self.PROMPT_FILES:]:
                stale.unlink(missing_ok=True)
            self.prompt_error = ''
        except Exception as exc:
            self.prompt_error = f'{type(exc).__name__}: {exc}'

    def unload(self):
        """Free the weights and keep the interpreter with its loaded libraries.

        A cold worker pays 8-50 s of interpreter and library import before it can
        read a request at all, then 5-8 s loading weights. Between two jobs only
        the second half is worth paying again, so an idle worker drops the model
        and the CUDA blocks it holds and stays resident for the next batch.
        """
        if self.model is None:
            return {"ok": True, "unloaded": False}
        self.model = None
        self.prompts.clear()
        # The fast paths hold the modules they patched; keeping them would keep
        # the weights alive and defeat the whole point.
        self.predictor_path = self.talker_path = None
        try:
            import gc
            import torch
            gc.collect()
            empty = getattr(torch.cuda, 'empty_cache', None)
            if empty:
                empty()
        except Exception:
            pass
        return {"ok": True, "unloaded": True}

    def run(self, message, progress=None):
        entered = time.perf_counter()
        # Wall clock beside the monotonic one: only a clock both processes share
        # can be compared across the pipe, and this is the moment the parent's
        # write reached us -- the far side of the IPC it is trying to measure.
        entered_at = time.time()
        import torch
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel

        imported = time.perf_counter()
        settings = message["settings"]
        started = imported
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
        key = (self.digest(voice["reference_audio"]), voice["reference_text"])
        hit = key in self.prompts
        with torch.inference_mode():
            if hit:
                prompt = self.prompts.pop(key)
                from_disk = False
            else:
                # The key carries everything that changes the answer: the clip,
                # its transcript, whether ICL is in play, the weights, and the
                # precision they were encoded at.
                path = self.prompt_path(message, (key[0], key[1], not bool(voice["reference_text"]),
                                                  settings.get("model_dir", ""), self.precision))
                prompt = self.load_prompt(path)
                from_disk = prompt is not None
                if prompt is None:
                    prompt = self.model.create_voice_clone_prompt(ref_audio=voice["reference_audio"],
                        ref_text=voice["reference_text"] or None, x_vector_only_mode=not bool(voice["reference_text"]))
                    self.save_prompt(path, prompt)
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
            # Both graphs are retried here, where no activations are held yet and
            # "how much room is left" means something. A downgrade lasts only as
            # long as the paging that caused it, not as long as the worker.
            self.talker_path.rearm()
            self.predictor_path.progress = progress
            try:
                wavs, rate = self.model.generate_voice_clone(text=[i["text"] for i in message["items"]],
                    language=[i["language"] for i in message["items"]], voice_clone_prompt=prompt,
                    **message["options"])
            except Exception as exc:
                raise RuntimeError(self.explain(exc)) from exc
            finally:
                self.predictor_path.progress = None
            # A batch that stayed on the graph clears the retry backoff, so a
            # one-off slow batch does not make the next retry wait longer.
            self.predictor_path.settle()
            self.talker_path.settle()
            generated = time.perf_counter()
            if len(wavs) != len(message["items"]):
                raise RuntimeError("批量后端返回的音频数量与请求不符")
            for item, audio in zip(message["items"], wavs):
                sf.write(item["output"], audio, rate, subtype="PCM_16", format="WAV")
            written = time.perf_counter()
        return {"ok": True, "count": len(wavs), "warm_model": warm, "prompt_cache_hit": hit,
                "prompt_disk_hit": from_disk,
                "precision": self.precision, "precision_reason": self.precision_reason,
                **self.predictor_path.status(counts),
                **self.talker_path.status(talker_counts),
                "load_seconds": loaded-started, "prompt_seconds": prompt_ready-loaded,
                # The parent times the whole round trip; these split it into the
                # pieces the worker can see, so whatever is left over is the IPC.
                "import_seconds": imported-entered, "server_seconds": written-entered,
                "generate_seconds": generated-prompt_ready, "write_seconds": written-generated,
                "inference_seconds": written-prompt_ready, "entered_at": entered_at}


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
            payload = json.loads(line)
            if payload.get("unload"):
                # Answered on the same pipe as a request, so the parent marks it
                # and never mistakes it for the answer to a batch.
                result = engine.unload()
            else:
                with redirect_stdout(sys.stderr):
                    result = engine.run(payload, progress=report)
                result["frames"] = frames
        except Exception as exc:
            result = {"ok": False, "message": str(exc), "traceback": traceback.format_exc()}
        protocol.write(json.dumps(result, ensure_ascii=False) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
