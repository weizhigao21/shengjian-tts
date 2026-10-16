# 独立批量推理 · v0.7.44

批量推理是独立后端模块，不替换现有 audio.cpp。首版适配 Qwen3-TTS Base 声音克隆：一个持久 Python 进程加载一份原始模型，使用官方列表生成接口一次处理多条文本，并复用参考音色提示。桌面与本地 API 不导入 PyTorch，发行包不包含该运行环境和原始权重。

## 使用

在「设置 → 批量性能 → Qwen 共享模型批量推理」选择独立 Python、Base 原始模型目录、关联的 GGUF，检查并应用。原始模型与关联 GGUF 应对应同一规格，例如 1.7B Base；GGUF 本身不能交给官方 Python 后端加载。默认每批最多 2 条、总计 2400 字；可调为 2–8 条。自动精度在 CUDA 上使用 FP16，在 CPU 上使用 FP32；2080 Ti 使用自动或 FP16。

启用后，现有 `/jobs` 和追加生成 API 会自动调度：

- 同一任务、同一生成批次内合并兼容短句；不跨任务 / 生成批次，保留框 ID、字幕顺序与各条结果。
- 生成语言可不同，模型、参考音色、参考原文及有效参数（含种子）必须兼容；不同种子分组，不静默修改参数。
- 孤立单条冷任务走现有引擎；批量模型已驻留时，兼容的末尾单条继续使用该模型，避免奇数条批次频繁切换。
- 不支持的模型 / 模式、超长文本、环境未就绪时使用现有引擎；批量运行报错或显存不足时，本生成批次回退到单实例原生引擎，不无限重复尝试。
- 取消一批不会提交该批未完成输出；已完成内容保留。默认空闲约 120 秒后释放批量模型，手动释放引擎、识别和 AI 编辑也会释放它。

新后端与 audio.cpp 的随机采样、精度和实现不同，音频不保证逐样本相同。结果缓存按后端、原始权重版本、运行库版本及完整有序批次隔离；强制重生成与工作流临时音频仍遵守原有规则。若批次缓存不完整，重新生成整个张量批次，以免删除某一输入后改变其他输入的随机数位置。

## 按需安装

在专用环境中安装，不覆盖系统 Python 或声笺配置。建议使用 Python 3.10–3.12。安装脚本默认只显示计划；加 `--install` 后才联网安装。下面示例以当前使用的 1.7B Base 为例，需要额外下载 PyTorch 和原始权重。

```powershell
python scripts/setup_qwen_batch.py --target "G:\models\qwen-batch-env" --download-model 1.7B --model-dir "G:\models\Qwen3-TTS-1.7B-Base"
python scripts/setup_qwen_batch.py --target "G:\models\qwen-batch-env" --download-model 1.7B --model-dir "G:\models\Qwen3-TTS-1.7B-Base" --install
```

脚本从官方 PyTorch CUDA 12.8 索引安装 torch / torchaudio 2.9.0，再安装 qwen-tts 0.1.1，从 Qwen 官方 Hugging Face 仓库下载原始模型；下载可继续使用已有文件。CPU 环境加 `--device cpu`。已有原始模型时省略下载参数；已有独立环境时可直接在设置中选择。Python SDK 首次加载可能需要较多时间和内存；模型加载仅访问本地文件，不在生成请求中自动下载。该环境与模型保存在自选目录，程序原位更新不会覆盖。

安装脚本与说明也包含在 Windows 分发包中；运行安装脚本需要本机已有 Python。首版不在后台静默安装大型运行库或模型。
源码目录中的脚本位于 `scripts/setup_qwen_batch.py`；Windows 分发包中的脚本位于 `docs/setup_qwen_batch.py`，在解压目录运行示例时将命令中的 `scripts/` 改为 `docs/`。

## API

| 接口 | 用途 |
| :--- | :--- |
| `GET /inference/backends` | 原生 / 批量后端能力、是否就绪、未就绪原因、是否已驻留。 |
| `GET /settings/batch-inference` | 配置、运行库版本、上批阶段耗时与音色缓存命中、近期回退原因。 |
| `PUT /settings/batch-inference` | 保存独立环境、模型关联、设备、精度与批大小；活动推理期间拒绝更改。 |
| `POST /inference/plan` | `{ "voice_id": "预设ID", "texts": ["你好", "Hello"] }`；只读预览该预设的路由与批大小，不创建任务 / 加载模型。 |
| `POST /jobs`、`POST /jobs/{id}/generate` | 新增可选 `inference_mode: "auto" | "native" | "batch"`，默认 `auto`；`batch` 表示优先尝试，无法合批时仍返回原生结果及原因。 |

其他原有字段、轮询、取消、重试、工作流交付 API 保留。每条结果新增 `inference_mode` 与 `inference`；后者记录实际后端、批大小、整批耗时、模型版本与缓存状态，原生回退时记录原因。批量结果的 `generation_seconds` 是该条等待整批完成的耗时，不是独立测得的单条 GPU 时间。
`/health` 的能力列表包含 `batch_inference` 和 `inference_plan`，`inference_api:1` 表示接口版本；能力存在不代表原始模型或独立环境已安装，应再检查后端的 `ready` 与 `reason`。

## 模块与验证

`batch_inference/policy.py` 处理兼容性，`backend.py` 处理配置与选择，`process.py` 管理可取消的持久进程，`qwen_worker.py` 调用官方 SDK，`execution.py` 对接队列、缓存与文件交付。独立设置组件位于 `desktop/settings/batch_inference.py`，API 位于 `http_api/inference.py`。

自动回归覆盖队列、接口、真实子进程 IPC，以及 SDK 的列表输入 / 提示缓存调用契约；测试替身不代表真实模型性能。原始模型未安装时不能宣称已获得 GPU 加速收益，应在安装后用相同字幕比较总耗时、输出质量、模型冷 / 热启动及显存峰值。

依据：[Qwen 官方批处理与提示复用](https://github.com/QwenLM/Qwen3-TTS#voice-clone)、[qwen-tts 0.1.1](https://pypi.org/project/qwen-tts/0.1.1/)、[PyTorch 官方安装组合](https://pytorch.org/get-started/previous-versions/)。
