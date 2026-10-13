# 分发依赖与许可证

声笺源代码使用 [GPLv3](../LICENSE)。下表记录 v0.7.33 发行包的构建环境，
包含运行依赖与构建工具；后者不一定进入 EXE。许可文本从该版本已安装的分发包原样复制。

- 对应源代码：[声笺仓库](https://github.com/weizhigao21/shengjian-tts)，当前 v0.7.33 本地包尚未发布；公开发行页提供 v0.7.28 源码 ZIP。
- 可复建环境：[requirements-release.txt](../requirements-release.txt)，构建入口 `打包.bat` 与 `声笺.spec`。
- CPython 3.10.11：[完整声明](Python-LICENSE.txt)、[源代码](https://www.python.org/downloads/release/python-31011/)。
- Qt 6.11.2：LGPLv3，动态链接；来源为 PyQt6-Qt6 wheel，
  [Qt 源代码](https://download.qt.io/archive/qt/6.11/6.11.2/single/)。程序未修改 Qt / PyQt；Qt DLL 位于 `_internal/PyQt6/Qt6/`。
- yovoice：仅改编的目录与情绪预设，Apache-2.0；[完整许可](YOVOICE-LICENSE.txt)，详见[第三方声明](../THIRD_PARTY_NOTICES.md)。
- 引擎和模型由用户另行下载，各自保留其上游许可。
- CPython 附带的 OpenSSL 1.1.1t：[许可](OpenSSL-LICENSE.txt)、[源代码](https://github.com/openssl/openssl/tree/OpenSSL_1_1_1t)。
- 更新器进度窗使用 CPython 附带的 Tcl / Tk 8.6.12：[Tk 原始许可](Tk-LICENSE.txt)，Tcl / Tk 相关声明同时保留在 CPython 完整许可内。
- libffi：[许可](libffi-LICENSE.txt)；SQLite：[公有领域声明](https://www.sqlite.org/copyright.html)。

| 依赖 | 版本 | 包内许可 | 上游源码分发 |
| :--- | :--- | :--- | :--- |
| altgraph | 0.17.5 | [LICENSE](altgraph/LICENSE) | [来源](https://pypi.org/project/altgraph/0.17.5/#files) |
| annotated-doc | 0.0.5 | [LICENSE](annotated-doc/LICENSE) | [来源](https://pypi.org/project/annotated-doc/0.0.5/#files) |
| annotated-types | 0.8.0 | [LICENSE](annotated-types/LICENSE) | [来源](https://pypi.org/project/annotated-types/0.8.0/#files) |
| anyio | 4.15.1 | [LICENSE](anyio/LICENSE) | [来源](https://pypi.org/project/anyio/4.15.1/#files) |
| certifi | 2026.7.22 | [LICENSE](certifi/LICENSE) | [来源](https://pypi.org/project/certifi/2026.7.22/#files) |
| click | 8.5.0 | [LICENSE.txt](click/LICENSE.txt) | [来源](https://pypi.org/project/click/8.5.0/#files) |
| exceptiongroup | 1.3.1 | [LICENSE](exceptiongroup/LICENSE) | [来源](https://pypi.org/project/exceptiongroup/1.3.1/#files) |
| fastapi | 0.136.1 | [LICENSE](fastapi/LICENSE) | [来源](https://pypi.org/project/fastapi/0.136.1/#files) |
| h11 | 0.16.0 | [LICENSE.txt](h11/LICENSE.txt) | [来源](https://pypi.org/project/h11/0.16.0/#files) |
| httpcore | 1.0.9 | [LICENSE.md](httpcore/LICENSE.md) | [来源](https://pypi.org/project/httpcore/1.0.9/#files) |
| httpx | 0.28.1 | [LICENSE.md](httpx/LICENSE.md) | [来源](https://pypi.org/project/httpx/0.28.1/#files) |
| idna | 3.20 | [LICENSE.md](idna/LICENSE.md) | [来源](https://pypi.org/project/idna/3.20/#files) |
| packaging | 26.3 | [LICENSE](packaging/LICENSE), [LICENSE.APACHE](packaging/LICENSE.APACHE), [LICENSE.BSD](packaging/LICENSE.BSD) | [来源](https://pypi.org/project/packaging/26.3/#files) |
| pefile | 2024.8.26 | [LICENSE](pefile/LICENSE) | [来源](https://pypi.org/project/pefile/2024.8.26/#files) |
| pip | 23.0.1 | [LICENSE.txt](pip/LICENSE.txt) | [来源](https://pypi.org/project/pip/23.0.1/#files) |
| pydantic | 2.14.1 | [LICENSE](pydantic/LICENSE) | [来源](https://pypi.org/project/pydantic/2.14.1/#files) |
| pydantic_core | 2.50.1 | [LICENSE](pydantic-core/LICENSE) | [来源](https://pypi.org/project/pydantic_core/2.50.1/#files) |
| pyinstaller | 6.20.0 | [COPYING.txt](pyinstaller/COPYING.txt) | [来源](https://pypi.org/project/pyinstaller/6.20.0/#files) |
| pyinstaller-hooks-contrib | 2026.8 | [LICENSE](pyinstaller-hooks-contrib/LICENSE) | [来源](https://pypi.org/project/pyinstaller-hooks-contrib/2026.8/#files) |
| PyQt6 | 6.11.0 | [LICENSE](pyqt6/LICENSE) | [来源](https://pypi.org/project/PyQt6/6.11.0/#files) |
| PyQt6-Qt6 | 6.11.2 | [LICENSE](pyqt6-qt6/LICENSE) | [来源](https://pypi.org/project/PyQt6-Qt6/6.11.2/#files) |
| PyQt6_sip | 13.13.0 | [LICENSE](pyqt6-sip/LICENSE) | [来源](https://pypi.org/project/PyQt6_sip/13.13.0/#files) |
| pywin32-ctypes | 0.2.3 | [LICENSE.txt](pywin32-ctypes/LICENSE.txt) | [来源](https://pypi.org/project/pywin32-ctypes/0.2.3/#files) |
| setuptools | 84.0.0 | [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE.APACHE](setuptools/LICENSE.APACHE), [LICENSE.BSD](setuptools/LICENSE.BSD), [LICENSE](setuptools/LICENSE), [LICENSE](setuptools/LICENSE), [LICENSE.txt](setuptools/LICENSE.txt), [LICENSE](setuptools/LICENSE) | [来源](https://pypi.org/project/setuptools/84.0.0/#files) |
| starlette | 1.7.0 | [LICENSE.md](starlette/LICENSE.md) | [来源](https://pypi.org/project/starlette/1.7.0/#files) |
| typing-inspection | 0.4.4 | [LICENSE](typing-inspection/LICENSE) | [来源](https://pypi.org/project/typing-inspection/0.4.4/#files) |
| typing_extensions | 4.16.0 | [LICENSE](typing-extensions/LICENSE) | [来源](https://pypi.org/project/typing_extensions/4.16.0/#files) |
| uvicorn | 0.46.0 | [LICENSE.md](uvicorn/LICENSE.md) | [来源](https://pypi.org/project/uvicorn/0.46.0/#files) |
| wheel | 0.48.0 | [LICENSE.txt](wheel/LICENSE.txt) | [来源](https://pypi.org/project/wheel/0.48.0/#files) |
