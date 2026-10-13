"""Vevo2's VC and S2S routes, using the shared persistent native server."""
import base64
import shutil
import tempfile
from pathlib import Path
import httpx
from ..audio_cpp import AudioCppRunner
from ..errors import VoiceError


def native_request(operation, source, reference, *, text="", original_text="", seed=-1, steps=32):
    if operation not in ("vc", "edit"):
        raise ValueError("unsupported Vevo2 operation")
    options = {"route": "style_preserved_vc" if operation == "vc" else "editing",
               "source_audio": str(source), "target_voice": str(reference), "num_inference_steps": steps}
    if seed >= 0:
        options["seed"] = seed
    if operation == "edit":
        options.update(prosody_ref=str(source), style_ref=str(source),
                       target_text=text, style_ref_text=original_text, use_prosody_code=True)
    return {"model": "speech", "request": {"options": options, **({"text": text} if operation == "edit" else {})}}


class VevoRunner(AudioCppRunner):
    def generate_edit(self, voice, parameters, source, reference, output, cancelled):
        if cancelled.is_set():
            raise VoiceError("CANCELLED", "编辑已取消")
        _, endpoint = self.start(voice, cancelled)
        try:
            # Native readers can require ASCII paths; preserve Unicode user paths.
            with tempfile.TemporaryDirectory(prefix="shengjian-edit-") as folder:
                if not folder.isascii():
                    raise VoiceError("NATIVE_PATH_ENCODING", "系统临时目录含非 ASCII 字符，请设置 TMP 为英文路径")
                staged = Path(folder)
                shutil.copyfile(source, staged / "source.wav")
                shutil.copyfile(reference, staged / "voice.wav")
                body = native_request(parameters["operation"], staged / "source.wav", staged / "voice.wav",
                                      text=parameters["state"]["target_text"],
                                      original_text=parameters["state"]["original_text"],
                                      seed=parameters["seed"], steps=parameters["steps"])
                with httpx.Client(trust_env=False, timeout=voice["timeout"]) as client:
                    response = client.post(endpoint + "/v1/tasks/run", json=body)
            if response.status_code != 200:
                raw = response.text[-12000:]
                code = "GPU_OUT_OF_MEMORY" if "out of memory" in raw.lower() or "insufficient_memory" in raw.lower() else "ENGINE_HTTP_ERROR"
                raise VoiceError(code, f"Vevo2 处理失败：{raw}", "缩短选区或参考音频后重试，原音频仍保留。", "音频编辑")
            result = response.json()
            if not result.get("audio"):
                raise VoiceError("ENGINE_NO_AUDIO", "Vevo2 没有返回音频")
            audio = base64.b64decode(result["audio"], validate=True)
            if cancelled.is_set():
                raise VoiceError("CANCELLED", "编辑已取消")
            Path(output).write_bytes(audio)
        except httpx.TimeoutException as exc:
            self.interrupt()
            raise VoiceError("INFERENCE_TIMEOUT", "Vevo2 处理超时，请缩短音频后重试") from exc
        except Exception:
            self.interrupt()
            raise
