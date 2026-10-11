"""Entry shared by the desktop executable and its console service helper."""
from multiprocessing import freeze_support
from pathlib import Path
import sys


def main():
    freeze_support()
    # Frozen Python ignores PYTHONUTF8; the service/worker pipes use UTF-8 JSON.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", write_through=True)
    if sys.argv[1:2] == ["engine"]:
        from voice_workbench.engine_worker import main as engine_main
        from voice_workbench.runtime import external_environment
        with external_environment():
            engine_main()
        return
    if len(sys.argv) == 1 and Path(sys.executable).name == "声笺服务.exe":
        sys.argv.append("serve")
    if sys.stdout is None or sys.stderr is None:
        from voice_workbench.config import DEFAULT_DATA
        data = DEFAULT_DATA
        for index, argument in enumerate(sys.argv[:-1]):
            if argument == "--data-dir":
                data = Path(sys.argv[index + 1])
        (data / "logs").mkdir(parents=True, exist_ok=True)
        log = (data / "logs" / "startup.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    from voice_workbench.__main__ import main as app_main
    app_main()


if __name__ == "__main__":
    main()
