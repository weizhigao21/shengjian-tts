# 独立批量推理 · v0.7.46

批量推理是独立后端模块，不替换现有 audio.cpp。首版适配 Qwen3-TTS Base 声音克隆：一个持久 Python 进程加载一份原始模型，使用官方列表生成接口一次处理多条文本，并复用参考音色提示。桌面与本地 API 不导入 PyTorch，发行包不包含该运行环境和原始权重。

## 使用

在「设置 → 批量性能 → Qwen 共享模型批量推理」选择已安装的 Base GGUF，点击 **一键安装并关联**。程序读取 GGUF 内部配置匹配 0.6B / 1.7B 原始权重，检测已有文件，下载缺少的独立 Python、PyTorch、Qwen SDK 和配套模型，完成后自动检查并保存关联。用户不需要预装 Python。也可以单独点击「下载运行环境」「下载关联模型」，或展开「高级手动配置」使用自选路径。

- Windows x64 自动安装将文件放在当前数据目录：`runtime/qwen-batch/` 保存便携 Python、CUDA / CPU 专用环境和 pip 缓存；`models/qwen-batch/规格/固定版本/` 保存原始权重。程序原位更新保留这些目录。
- 首次独立 Python 约 44 MB，PyTorch 等依赖另需数 GB；Base 0.6B 原始权重约 2.34 GB，1.7B 约 4.23 GB。点击安装前界面显示规格和保存位置。模型源支持 HF 镜像或 Hugging Face；Python 使用 Astral 官方发行，运行库使用 PyPI / PyTorch 官方索引。
- 自动检测限于已导入的 GGUF、已保存路径、专用环境和标准 Hugging Face 缓存，不扫描整个磁盘或修改系统 Python。设备变更使用独立环境；默认 NVIDIA CUDA，驱动不可用时提示更新驱动或选择 CPU。
- Python 压缩包固定大小和 SHA-256，安全解压；模型固定官方提交并逐文件核对大小 / SHA-256，包含 speech_tokenizer 和文本分词器配置。下载只有用户点击安装 / 下载按钮后开始，打开界面、启动服务或轮询不会静默下载。
- 任务显示安装阶段、文件进度、近期下载速度。可暂停和继续；模型 `.part` 支持 HTTP Range 续传，依赖安装重试复用 pip 缓存。安装日志保存在 `logs/qwen-batch-install.log`。
- 完整文件优先复用；原始模型也可从标准 Hugging Face 缓存自动关联。安装期间不能修改高级配置，未完成或失败时保留原配置；若安装完成时正在推理，等待空闲后再应用。
- 首次打开和文件路径 / 环境文件变化时自动检测，下载期间轮询状态，完成后自动刷新关联与就绪提示。检测只检查元数据和依赖，不加载大模型；重复轮询不重复启动依赖检查。

原始模型与关联 GGUF 应对应同一规格；GGUF 不能交给官方 Python 后端直接加载。默认每批最多 2 条、总计 2400 字；可在高级配置调为 2–8 条。自动精度在 CUDA 上使用 FP16，在 CPU 上使用 FP32；2080 Ti 使用自动或 FP16。
启用后，现有 `/jobs` 和追加生成 API 会自动调度：

- 同一任务、同一生成批次内合并兼容短句；不跨任务 / 生成批次，保留框 ID、字幕顺序与各条结果。
- 生成语言可不同，模型、参考音色、参考原文及有效参数（含种子）必须兼容；不同种子分组，不静默修改参数。
- 孤立单条冷任务走现有引擎；批量模型已驻留时，兼容的末尾单条继续使用该模型，避免奇数条批次频繁切换。
- 不支持的模型 / 模式、超长文本、环境未就绪时使用现有引擎；批量运行报错或显存不足时，本生成批次回退到单实例原生引擎，不无限重复尝试。
- 取消一批不会提交该批未完成输出；已完成内容保留。默认空闲约 120 秒后释放批量模型，手动释放引擎、识别和 AI 编辑也会释放它。

新后端与 audio.cpp 的随机采样、精度和实现不同，音频不保证逐样本相同。结果缓存按后端、原始权重版本、运行库版本及完整有序批次隔离；强制重生成与工作流临时音频仍遵守原有规则。若批次缓存不完整，重新生成整个张量批次，以免删除某一输入后改变其他输入的随机数位置。

## 速度对比测试

安装就绪后，在同一页面的「速度对比测试」选择声音预设，点击「开始测速」。可选择中英双语、中文或英文 4 条固定短句，热模型重复 1–3 组，默认 1 组；默认总计生成 16 段测试音频。仅支持当前已关联的 Qwen Base 声音克隆预设、兼容的语言 / 语速和参数。

- 原生基线为 audio.cpp 单实例逐条生成，使用现有适配器；批量侧按当前每批条数和字数限制分组。两侧使用同一参考声音副本、原文、文本、语言和有效生成参数，固定种子 1234，每条最多 256 tokens（预设上限更小时保留其限制）。不更改保存的预设。
- 每个后端从新进程开始，先测一整组含模型加载的用时，再用同一进程和热模型完成后续组。这里只控制进程和模型驻留，不清空操作系统文件缓存，因此不将其称为磁盘完全冷启动。
- 热模型用时、音频时长按每组平均；RTF = 总生成秒数 / 总音频秒数，越小越快；吞吐 = 生成条数 / 秒。倍率 = 原生组耗时 / 批量组耗时，大于 1 代表该组工作负载中批量更快。两种模型的精度和采样实现不同，音频长度、内容完整性和质量可能不同，需结合时长和实际输出判断。
- 测试绕过成品音频缓存，仍会自然复用热模型和后端内部音色特征。某后端报错时保留错误和另一侧结果；批量侧不回退到原生后伪装成批量测速，不对失败或取消的对比计算倍率。
- 开始前需完成 / 取消已有生成、识别、编辑、安装及排队任务。测试保留引擎，新提交的生成等待测试结束；测试中不可改引擎性能或模型配置。可以随时取消，服务退出也会取消并释放测试进程。
- 测试不写入作品、生成记录或成品缓存。临时 WAV 验证后清理，最近报告保存在 `benchmarks/latest.json`，日志在 `logs/benchmarks/测试ID/`。界面显示实时进度，支持复制 JSON 报告；重启后仍能查看最近报告。

## 高级命令行安装

在专用环境中安装，不覆盖系统 Python 或声笺配置。建议使用 Python 3.10–3.12。安装脚本默认只显示计划；加 `--install` 后才联网安装。下面示例以当前使用的 1.7B Base 为例，需要额外下载 PyTorch 和原始权重。

```powershell
python scripts/setup_qwen_batch.py --target "G:\models\qwen-batch-env" --download-model 1.7B --model-dir "G:\models\Qwen3-TTS-1.7B-Base"
python scripts/setup_qwen_batch.py --target "G:\models\qwen-batch-env" --download-model 1.7B --model-dir "G:\models\Qwen3-TTS-1.7B-Base" --install
```

脚本从官方 PyTorch CUDA 12.8 索引安装 torch / torchaudio 2.9.0，再安装 qwen-tts 0.1.1，从 Qwen 官方 Hugging Face 仓库下载原始模型；下载可继续使用已有文件。CPU 环境加 `--device cpu`。已有原始模型时省略下载参数；已有独立环境时可直接在设置中选择。Python SDK 首次加载可能需要较多时间和内存；模型加载仅访问本地文件，不在生成请求中自动下载。该环境与模型保存在自选目录，程序原位更新不会覆盖。

安装脚本与说明也包含在 Windows 分发包中；此高级脚本需要本机已有 Python。推荐直接使用设置页的一键安装，它会下载独立 Python。
源码目录中的脚本位于 `scripts/setup_qwen_batch.py`；Windows 分发包中的脚本位于 `docs/setup_qwen_batch.py`，在解压目录运行示例时将命令中的 `scripts/` 改为 `docs/`。

## API

| 接口 | 用途 |
| :--- | :--- |
| `GET /inference/backends` | 原生 / 批量后端能力、是否就绪、未就绪原因、是否已驻留。 |
| `GET /settings/batch-inference` | 配置、运行库版本、上批统计和近期回退；新增 `setup`，含本地关联候选、目标目录、大小和安装任务状态。 |
| `POST /inference/setup` | `{ "action": "all", "source_model": "已导入GGUF路径", "device": "cuda", "source": "mirror" }`，202 异步安装。action 支持 `all / runtime / model / detect`；detect 仅本地检测和关联。 |
| `POST /inference/setup/pause` | 暂停当前专用环境 / 模型安装；已有文件保留。 |
| `GET /inference/benchmark` | 可选预设、就绪原因、测速进度和最近报告；`GET /settings/batch-inference` 也返回同一 `benchmark` 快照。 |
| `POST /inference/benchmark` | 202 异步测速：`{ "voice_id": "预设ID", "workload_name": "bilingual", "warm_rounds": 1 }`。文本组支持 `bilingual / zh / en`，热模型组数 1–3。 |
| `POST /inference/benchmark/cancel` | 取消测速并释放测试引擎，保留部分报告。 |
| `PUT /settings/batch-inference` | 保存独立环境、模型关联、设备、精度与批大小；活动推理期间拒绝更改。 |
| `POST /inference/plan` | `{ "voice_id": "预设ID", "texts": ["你好", "Hello"] }`；只读预览该预设的路由与批大小，不创建任务 / 加载模型。 |
| `POST /jobs`、`POST /jobs/{id}/generate` | 新增可选 `inference_mode: "auto" | "native" | "batch"`，默认 `auto`；`batch` 表示优先尝试，无法合批时仍返回原生结果及原因。 |

其他原有字段、轮询、取消、重试、工作流交付 API 保留。每条结果新增 `inference_mode` 与 `inference`；后者记录实际后端、批大小、整批耗时、模型版本与缓存状态，原生回退时记录原因。批量结果的 `generation_seconds` 是该条等待整批完成的耗时，不是独立测得的单条 GPU 时间。
`/health` 的能力列表包含 `batch_inference` 和 `inference_plan`，`inference_api:1` 表示接口版本；能力存在不代表原始模型或独立环境已安装，应再检查后端的 `ready` 与 `reason`。

## 模块与验证

`batch_inference/policy.py` 处理兼容性，`backend.py` 处理配置与选择，`process.py` 管理可取消的持久进程，`qwen_worker.py` 调用官方 SDK，`execution.py` 对接队列、缓存与文件交付。`discovery.py` 做本地规格识别和配对，`provisioning.py` 管理隔离安装、校验与取消。`benchmark.py` 管理独占测速、快照、无成品缓存的冷 / 热运行和报告。独立设置组件位于 `desktop/settings/batch_inference.py`，API 位于 `http_api/inference.py`。

自动回归覆盖队列、接口、真实子进程 IPC，以及 SDK 的列表输入 / 提示缓存调用契约；测试替身不代表真实模型性能。原始模型未安装时不能宣称已获得 GPU 加速收益，应在安装后用相同字幕比较总耗时、输出质量、模型冷 / 热启动及显存峰值。

依据：[Qwen 官方批处理与提示复用](https://github.com/QwenLM/Qwen3-TTS#voice-clone)、[qwen-tts 0.1.1](https://pypi.org/project/qwen-tts/0.1.1/)、[PyTorch 官方安装组合](https://pytorch.org/get-started/previous-versions/)。

运行环境来源：[Astral Python 独立分发](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901)、[Python 安装方式说明](https://docs.astral.sh/uv/concepts/python-versions/)。
