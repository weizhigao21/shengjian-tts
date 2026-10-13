# 第三方声明

声笺源代码使用 [GPLv3](LICENSE)。Windows 分发包包含 Python、Qt / PyQt6 及相关依赖，
完整版本、原始许可和源码来源见 [licenses/README.md](licenses/README.md)。
发行页同时提供对应声笺源码，构建环境记录在 `requirements-release.txt`，入口为 `打包.bat` 与 `声笺.spec`。

## 参考与改编

| 来源 | 参考或改编内容 | 许可 |
| :--- | :--- | :--- |
| [yovoice](https://github.com/leemysw/yovoice) · leemysw | 模型目录、IndexTTS 情绪组合；进程管理、请求协议与界面布局参考 | Apache-2.0；[完整许可](licenses/YOVOICE-LICENSE.txt) |
| [audio.cpp](https://github.com/0xShug0/audio.cpp) · ShugoAI LLC | 原生引擎协议、模型参数与运行库安装方式 | Apache-2.0；引擎另行下载 |
| [IndexTTS](https://github.com/index-tts/index-tts) | 四种表达方式、八维情绪顺序与官方 Python 接口 | 官方实现与权重各自遵循上游许可 |

声笺使用 Python / Qt 实现界面、队列和适配，没有直接打包 yovoice 的 React 前端。
视觉设计参考其 `paper-theme.ts`、`styles.css`、侧栏工作台与声音库组件；Qt 控件、SVG 图标与波形由本项目实现。

改编内容保留如下来源信息：

- `voice_workbench/assets/model_catalog.json`：改编自 yovoice 的 `internal/catalog/catalog.json`；
  下载规则参考 `internal/catalog/catalog.go`，默认 revision 为 `6d5436fc85f7a20c2e9f4e472b7f3a532f686444`，条目自身的 revision 优先。
- `voice_workbench/index_emotion.py`：12 个情绪组合改编自 yovoice 的 `web/src/features/create/inspector.tsx`。
  标准参数依据 IndexTTS 官方接口和 audio.cpp 模型解析，保存与推理适配由本项目实现。
- `voice_workbench/assets/engine_catalog.json`：记录 audio.cpp 官方发行资产及摘要；
  CUDA 程序与运行库的组合方式参考 yovoice 的 `internal/workbench/runtime_install.go`。

## 包内依赖

- PyQt6：Copyright Riverbank Computing Limited，GPLv3。
- Qt：Copyright The Qt Company 及贡献者，LGPLv3；通过动态 DLL 链接，程序未修改 Qt。
- CPython：Python Software Foundation 许可，完整历史声明保存在 `licenses/Python-LICENSE.txt`。
- Python 依赖、PyInstaller 引导程序与构建工具：各自原始许可收录于 `licenses/`。
- CPython 动态库依赖 OpenSSL、libffi 和 SQLite；分别遵循 OpenSSL、MIT 与公有领域声明。
  Windows 的 Microsoft C++ 运行库按其再分发条款使用。

## 模型与下载引擎

发行 ZIP 不包含 audio.cpp、CUDA 运行库或模型权重。用户通过设置页下载或导入，下载后的目录保留上游许可。
GGUF 默认来源为 [audio-cpp/audio.cpp-gguf](https://huggingface.co/audio-cpp/audio.cpp-gguf)。
模型权重遵循各自模型许可，目录数据的 Apache-2.0 许可不替代权重许可。
