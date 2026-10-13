from pathlib import Path
root = Path(SPECPATH)
a = Analysis([str(root / 'scripts/updater_entry.py')], pathex=[str(root)], binaries=[], datas=[],
             hiddenimports=[], hookspath=[], runtime_hooks=[],
             excludes=['PyQt6', 'PySide6', 'httpx', 'requests', 'fastapi', 'uvicorn', 'torch', 'pytest', 'numpy', 'PIL'],
             noarchive=False)
pyz = PYZ(a.pure)
EXE(pyz, a.scripts, a.binaries, a.datas, name='声笺更新器', console=False,
    icon=str(root / 'voice_workbench/assets/app.ico'), upx=False)
