import argparse
from pathlib import Path

from .config import DEFAULT_DATA
from . import __version__


def main():
    parser = argparse.ArgumentParser(description="本地配音工作台")
    parser.add_argument("--version", action="version", version=f"声笺 {__version__}")
    parser.add_argument("mode", choices=["gui", "serve"], nargs="?", default="gui")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
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
