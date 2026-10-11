@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m voice_workbench gui
) else (
  python -m voice_workbench gui
)
if errorlevel 1 (
  echo.
  echo 启动失败。请先运行 install.bat，或查看上方错误信息。
  pause
)
