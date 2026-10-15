import argparse
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .data_paths import prepare_data_dir


def main():
    if sys.argv[1:2] == ["decode-audio"]:
        from .speech_recognition.decode_worker import main as decode_main
        decode_main(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="本地配音工作台")
    parser.add_argument("--version", action="version", version=f"声笺 {__version__}")
    parser.add_argument("mode", choices=["gui", "serve"], nargs="?", default="gui")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="指定数据目录；安装版默认使用用户目录并迁移旧版 data")
    args = parser.parse_args()
    try:
        args.data_dir = args.data_dir if args.data_dir is not None else prepare_data_dir(args.port)
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        if args.mode == "gui":
            from PyQt6.QtWidgets import QApplication, QMessageBox
            app = QApplication([])
            QMessageBox.critical(None, "数据目录准备失败", str(exc))
            raise SystemExit(1)
        parser.error(str(exc))
    if args.mode == "serve":
        import uvicorn
        from .api import create_app
        app = create_app(args.data_dir)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                             log_level="info", timeout_graceful_shutdown=3,
                                             loop="asyncio", http="h11", ws="none", lifespan="on"))
        app.state.server = server
        server.run()
    else:
        from .gui import launch
        launch(args.port, args.data_dir)


if __name__ == "__main__":
    main()
