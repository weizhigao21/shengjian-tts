# 上游参考与运行时

本项目参考 yovoice 的本机进程管理和 audio.cpp 请求协议，使用 Python 独立实现界面、任务队列和工作流适配。

桌面视觉设计也参考 yovoice 的 `paper-theme.ts`、`styles.css` 和侧栏工作台布局；Qt 控件、图标与波形绘制为本项目独立实现。

声音库的票根卡片、唱片试听图形和简洁弹窗参考 yovoice 的 `features/library/library-layout.tsx`、`characters.tsx`、`record-cover.tsx` 和 `voice-editor.tsx`，使用 Qt 重新绘制与实现；未直接打包其 React 前端。

高级生成参数的名称、范围和默认值参考模型包内嵌声明、yovoice 的 `generation-options.json` 及 audio.cpp v0.7.4 的模型请求解析与分段实现；控件和参数校验由 Python 独立实现。

`voice_workbench/assets/model_catalog.json` 改编自 yovoice 的 `internal/catalog/catalog.json`（Apache-2.0，作者 leemysw），仅保留本程序已有适配的四种语音模型家族。下载地址规则参考 `internal/catalog/catalog.go`，固定默认 revision 为 `6d5436fc85f7a20c2e9f4e472b7f3a532f686444`，单条模型指定的 revision 优先；模型说明、下载器和管理界面由 Python / Qt 实现。

目录中的 GGUF 来源为 [audio-cpp/audio.cpp-gguf](https://huggingface.co/audio-cpp/audio.cpp-gguf)。下载的权重各自遵循其模型许可，目录的 Apache-2.0 许可不替代权重许可。本项目没有打包这些权重。yovoice 的完整 Apache-2.0 许可保存在 `voice_workbench/assets/YOVOICE_LICENSE.txt`。

`voice_workbench/assets/engine_catalog.json` 记录 [audio.cpp v0.7.4 官方发布](https://github.com/0xShug0/audio.cpp/releases/tag/v0.7.4) 的 Windows CPU 及 CUDA 12.4 ZIP 包、文件大小和 SHA-256；CUDA 程序包与运行库的组合方式参考 yovoice 的 `internal/catalog/catalog.go`、`internal/workbench/runtime_install.go`。下载、续传、解压及配置由 Python 独立实现，下载后的完整目录保留上游包中的许可文件。运行时依赖仍遵循其上游许可；本仓库与声笺 EXE 包不内置这些下载资产。

- [yovoice](https://github.com/leemysw/yovoice)：Apache-2.0；原项目作者 leemysw。
- [audio.cpp](https://github.com/0xShug0/audio.cpp)：Apache-2.0；Copyright ShugoAI LLC；原生统一推理引擎。
- [IndexTTS](https://github.com/index-tts/index-tts)：模型与官方实现遵循其上游许可。
- [Qt / PyQt6](https://www.riverbankcomputing.com/software/pyqt/)：依赖遵循其上游许可。

当前仓库不打包上游二进制或模型权重；从本机已安装目录引用的资产仍遵循其原许可。
如果将来发布含上游运行时或模型的安装包，须同时保留其许可证和相应 notices。
