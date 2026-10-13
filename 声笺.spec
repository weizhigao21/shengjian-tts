# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
assets = root / "voice_workbench" / "assets"
a = Analysis(
    [str(root / "scripts" / "frozen_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(assets), "voice_workbench/assets"),
           (str(root / "voice_workbench" / "engine_worker.py"), "voice_workbench"),
           (str(root / "voice_workbench" / "index_emotion.py"), "voice_workbench")],
    hiddenimports=["uvicorn.logging", "uvicorn.loops.asyncio",
                   "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "indextts", "pytest", "IPython", "notebook", "jupyter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
desktop = EXE(pyz, a.scripts, [], exclude_binaries=True, name="声笺", console=False,
              icon=str(assets / "app.ico"), upx=False)
service = EXE(pyz, a.scripts, [], exclude_binaries=True, name="声笺服务", console=True,
              icon=str(assets / "app.ico"), upx=False)
COLLECT(desktop, service, a.binaries, a.datas, name="声笺", upx=False)
