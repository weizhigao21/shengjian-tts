"""在模型自己的 Python 环境中运行；标准库协议与 GUI 环境隔离。"""
from __future__ import annotations

import base64
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

_model = None
_signature = None


def windows_speech(request):
    if os.name != "nt":
        raise RuntimeError("Windows 系统声音只能在 Windows 上使用")
    # 输入通过 stdin 的 JSON 传递，不把文本或路径拼进 PowerShell 代码。
    script = """
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::InputEncoding=[System.Text.Encoding]::UTF8
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
$s=New-Object -ComObject SAPI.SpVoice
$stream=New-Object -ComObject SAPI.SpFileStream
try {
  $language=if ($p.language -eq 'zh') { '804' } else { '409' }
  $tokens=$s.GetVoices()
  $selected=$null
  for ($i=0; $i -lt $tokens.Count; $i++) {
    $token=$tokens.Item($i)
    if (($p.voice -and $token.GetDescription() -eq $p.voice) -or
        (-not $p.voice -and $token.GetAttribute('Language').Split(';') -contains $language)) {
      $selected=$token
      break
    }
  }
  if (-not $selected) { throw "Windows speech voice not installed: $language" }
  $s.Voice=$selected
  $s.Rate=[Math]::Max(-10,[Math]::Min(10,[int](($p.speed-1)*10)))
  $stream.Open($p.output,3,$false)
  $s.AudioOutputStream=$stream
  [void]$s.Speak([string]$p.text)
} catch {
  [Console]::Error.WriteLine($_.Exception.GetBaseException().Message)
  exit 1
} finally {
  $stream.Close()
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($stream)
  [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($s)
}
"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    voice = request["voice"]
    payload = {"voice": voice.get("sapi_voice", ""), "language": voice["language"],
               "speed": voice["speed"], "text": request["text"], "output": request["output"]}
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))


def index_speech(request):
    global _model, _signature
    voice = request["voice"]
    sys.path.insert(0, voice["repo_dir"])
    import torch
    version = voice["index_version"]
    if _signature != request["fingerprint"]:
        _model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if voice["device"] == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用：检查模型 Python 环境的 PyTorch 与 NVIDIA 驱动")
        model_dir = str(Path(voice["model_dir"]).resolve())
        cfg = str(Path(model_dir) / "config.yaml")
        if version == "2":
            from indextts.infer_v2 import IndexTTS2
            _model = IndexTTS2(cfg_path=cfg, model_dir=model_dir, device=voice["device"],
                               use_fp16=voice["device"] == "cuda", use_cuda_kernel=False, use_deepspeed=False)
        else:
            from indextts.infer_v2_5 import IndexTTS2
            # Turing / 2080 Ti 不默认启用 BF16。
            bf16 = voice["device"] == "cuda" and torch.cuda.is_bf16_supported()
            _model = IndexTTS2(cfg_path=cfg, model_dir=model_dir, device=voice["device"],
                               use_bf16=bf16, use_qwen_emo=bool(voice["emotion"]))
        _signature = request["fingerprint"]
    kwargs = {"spk_audio_prompt": voice["reference_audio"], "text": request["text"],
              "output_path": request["output"], "verbose": False}
    if version == "2.5":
        kwargs["lang"] = voice["language"].upper()
        kwargs["duration_factor"] = 1 / voice["speed"]
    elif voice["speed"] != 1:
        raise ValueError("IndexTTS 2 的本适配器暂不提供语速控制，请使用默认语速或 2.5")
    if voice["emotion"]:
        kwargs.update(use_emo_text=True, emo_text=voice["emotion"])
    _model.infer(**kwargs)


def main():
    for line in sys.stdin:
        request = json.loads(line)
        print(f"\n--- job={request['job_id']} segment={request['index']} ---", file=sys.stderr, flush=True)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                if request["voice"]["backend"] == "windows_sapi":
                    windows_speech(request)
                else:
                    index_speech(request)
            response = {"ok": True}
        except Exception as exc:
            details = traceback.format_exc()
            print(details, file=sys.stderr, flush=True)
            response = {"ok": False, "message": str(exc), "traceback": details}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
