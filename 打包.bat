@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
title 声笺 一键打包
cd /d "%~dp0"
echo ============================================
echo    声笺 一键打包 ^(PyInstaller 文件夹包^)
echo ============================================
echo.

rem 优先使用项目虚拟环境，然后尝试 Python 3.10 和其他 Python 3。
set "PYCMD="
if exist ".venv\Scripts\python.exe" set PYCMD="%~dp0.venv\Scripts\python.exe"
if defined PYCMD goto python_ready
where py >nul 2>nul
if errorlevel 1 goto try_python
py -3.10 -c "pass" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3.10"
if defined PYCMD goto python_ready
py -3 -c "pass" >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto python_ready
:try_python
where python >nul 2>nul
if not errorlevel 1 set "PYCMD=python"
if not defined PYCMD (
    echo [错误] 未找到 Python，请安装 Python 3.10+ 并添加到 PATH。
    goto fail
)
:python_ready
%PYCMD% -c "import sys, struct; sys.exit(0 if sys.version_info >= (3,10) and struct.calcsize('P') == 8 else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] 需要 64 位 Python 3.10+。请检查项目虚拟环境或 Python 安装。
    goto fail
)
echo [1/5] 使用 Python: %PYCMD%
%PYCMD% --version
echo.

rem 有些虚拟环境没有安装 pip；使用 Python 自带的离线安装器修复。
%PYCMD% -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [1/5] 当前环境缺少 pip，正在自动修复…
    %PYCMD% -m ensurepip --upgrade
    if errorlevel 1 (
        echo [错误] 无法自动安装 pip。请修复 Python 安装，确保包含 pip 和 ensurepip 后重试。
        goto fail
    )
)
%PYCMD% -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [错误] pip 修复后仍不可用，请检查上方安装日志。
    goto fail
)

%PYCMD% -c "import fastapi, uvicorn, PyQt6, httpx" >nul 2>nul
if errorlevel 1 (
    echo [2/5] 安装运行依赖…
    %PYCMD% -m pip install .
    if errorlevel 1 goto fail
) else (
    echo [2/5] 运行依赖已就绪
)
%PYCMD% -c "from importlib.metadata import version; v=tuple(int(n) for n in version('pyinstaller').split('.')[:2]); assert (6,10) <= v and v[0] < 7" >nul 2>nul
if errorlevel 1 (
    echo [3/5] 安装 PyInstaller…
    %PYCMD% -m pip install "pyinstaller>=6.10,<7"
    if errorlevel 1 goto fail
) else (
    echo [3/5] PyInstaller 已就绪
)

%PYCMD% scripts\build_package.py
if errorlevel 1 goto fail
echo.
echo ============================================
echo    打包完成！
echo ============================================
echo 产物目录: %~dp0dist\声笺\
echo 主程序:   %~dp0dist\声笺\声笺.exe
echo.
echo 请分发 dist 中自动生成的版本 ZIP，首次运行无需安装 Python。
echo GGUF 模型、audio.cpp 引擎及备用 IndexTTS 环境需另行配置。
echo 安装版数据位于用户目录，首次升级自动迁移原 EXE 同级 data。
echo ZIP 不包含 data、参考音频和历史生成音频，已有发布目录 data 会保留。
echo.
if /i "%~1"=="--no-pause" exit /b 0
explorer "%~dp0dist\声笺" >nul 2>nul
pause
exit /b 0

:fail
echo.
echo [错误] 打包未完成，请查看上方日志定位原因。
if /i "%~1"=="--no-pause" exit /b 1
pause
exit /b 1
