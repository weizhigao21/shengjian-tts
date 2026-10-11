"""Check a real catalog transfer, pausing after one chunk; no user assets change."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from voice_workbench.manager import Manager

parser = argparse.ArgumentParser()
parser.add_argument("--source", choices=("mirror", "huggingface", "modelscope"), default="mirror")
args = parser.parse_args()
manager = Manager(ROOT / "artifacts" / "model-download-check")
models = manager.models
original = models._state
partial = models.root / "kokoro-82m-q8_0.gguf.part"
initial = partial.stat().st_size if partial.exists() else 0


def progress(identity, **changes):
    original(identity, **changes)
    if changes.get("received", 0) >= initial + 262144:
        models.cancel[identity].set()


models._state = progress
identity = "kokoro-82m-q8"
try:
    models.start_download(identity, args.source)
    models.threads[identity].join(45)
    if models.threads[identity].is_alive():
        models.pause(identity)
        raise RuntimeError("Transfer did not start within 45 seconds")
    state = models.downloads[identity]
    print({"source": args.source, "status": state["status"], "resumed_from": state.get("resumed_from"), "bytes": partial.stat().st_size if partial.exists() else 0,
           "error": state.get("error", "")})
    assert state["status"] == "paused" and partial.stat().st_size >= initial + 262144
    assert state.get("resumed_from") == initial, "Server restarted instead of resuming"
finally:
    manager.close()
