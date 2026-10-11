@echo off
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -e ".[test]"
if errorlevel 1 goto failed
echo 安装完成。双击 launch.bat 启动。
pause
exit /b 0
:failed
echo 安装未完成，请查看上方具体错误。
pause
exit /b 1
