# 批量推理加速框架调研 · 2026-11-16

调研目的是回答一个问题：在现有的 Qwen3-TTS 批量后端上，有没有现成的推理加速框架可以直接替换或叠加，让批处理更快。结论先说：**在当前 Windows + RTX 2080 Ti（11 GB，sm_75）组合下，没有可以直接换上的成熟框架；两个真正支持 Qwen3-TTS 的服务框架都只跑 Linux，且收益集中在高并发在线场景。先把栈里剩下的三处做完，比换框架更划算。**

本文把"框架官方说法"和"本机推算"分开写，推算部分不作为实测结论引用。

## 现在的瓶颈在哪里

沿用 [批量推理说明](BATCH_INFERENCE.md) 里的分阶段计时（RTX 2080 Ti、1.7B Base FP16、批 8、单批 57 帧）：

| 环节 | 每帧耗时 | 占比 |
| :--- | ---: | ---: |
| Talker 单步 | 68.7 ms | 82.6% |
| └ 其中 Predictor 15 步图重放 | 31.4 ms | 37.7% |
| Code2Wav 解码 | 11.7 ms | 14.1% |
| 采样与调度 | 2.8 ms | 3.4% |
| **合计** | **83.2 ms** | — |

热模型批 8 为 0.56–0.60 秒 / 条（RTF 约 0.35），批 4 为 0.90 秒 / 条，批 10 以上在 11 GiB 卡上显存不足（批 8 峰值 9.60 GiB）。也就是说：

- 唯一的计算大头是 Talker 的「单 token decode + 15 步子码本预测」，Code2Wav 只占七分之一。
- 批 8 已经是这块卡的显存上限，靠继续加大批量拿吞吐这条路已经走到头。
- 现有实现已经把 CUDA Graph 用在 Predictor 上，但 Talker 自己的 forward（约 37 ms / 帧）仍走 eager。

## 候选框架

| 框架 | Qwen3-TTS 支持 | 批量能力 | 系统要求 | 本机（2080 Ti / 11 GB / Windows）可行性 |
| :--- | :--- | :--- | :--- | :--- |
| **vLLM-Omni** | 官方支持，含 Base 克隆、预计算音色、`/v1/audio/speech` 与 batch 端点 | 连续批处理 + 分阶段流水线（Talker / Code2Wav 拆开、异步 chunk） | 仅 Linux（Windows 需 WSL2）；官方 GPU 门槛 compute capability ≥ 7.5；当前版本固定 torch 2.13 + cu130 | 理论可达（sm_75 在 CUDA 13 与 torch cu130 的编译列表里），但 Turing 上注意力后端（FlashInfer / FA3）主要面向 SM80+，实际能不能起来必须实测；11 GB 下 KV 缓存与图缓存余量很小 |
| **SGLang-Omni 0.1.8** | 支持 Qwen3-TTS | 多阶段调度 + batch / streaming / 上传音色 | 仅 Linux | 同 vLLM-Omni，且 Turing 支持更不明确 |
| **TensorRT Edge-LLM** | 支持三组件（Talker / CodePredictor / Code2Wav），Base 克隆含 ECAPA + Mimi 编码器，CodePredictor 可 FP8 / NVFP4，还有 CodePredictor 投机解码 | `--max-batch-size 1`（当前运行时） | 目标平台是 Jetson / DRIVE / DGX Spark；x86 主机只在 SM80 / 86 / 89 / 120 上可跑调试 | **不可用**：sm_75 不在支持列表，且批量固定为 1 |
| **TensorRT-LLM** | 不支持 Qwen3-TTS 架构 | — | Linux | 不可用 |
| **OpenVINO** | 有 Qwen3-TTS 转换与 INT8（NNCF）方案 | — | Intel GPU / CPU | 不适用 NVIDIA 卡 |
| **torch.compile (Inductor)** | 通用，非 TTS 专用 | 无 | Windows 上 Triton 可装但脆弱 | 收益与手写 CUDA Graph 重叠，不建议在打包环境里开 |

补充两点判断依据：

- vLLM 官方明确"不支持原生 Windows"，只能 WSL2 或社区分支；GPU 要求 compute capability 7.5+，2080 Ti 正好卡在门槛上。
- CUDA 13 只移除了 Maxwell / Pascal / Volta，Turing 仍在支持列表；PyTorch cu130 的 wheel 也仍编译 sm_75，所以"版本不兼容"不是主要障碍，障碍是 Turing 上的算子后端。

## 换框架能拿到多少

官方口径（不可直接套用，但可作上限参考）：

- vLLM-Omni 对 Qwen3-TTS 的单请求对比：RTF 0.17 vs Hugging Face Transformers 的 2.64，首包快 8.4 倍；这批数据跑在 H20 / L40S 级数据中心卡上，且 HF 基线是**未优化的单流 `model.generate()`**。
- 其收益构成是：分阶段流水线让 Code2Wav 与 Talker 重叠、批处理预处理去掉每步 Python 开销、CUDA Graph 覆盖解码器、高并发（c=16 / 64）摊薄固定成本。

本机推算（仅供判断方向，不是实测）：按显存带宽差距，2080 Ti 大约是 RTX 5090 的三分之一；博客里 5090 上 0.6B 流式约 13 倍实时，折算到 1.7B 大致是 1.5–2 倍实时（RTF 0.5–0.7）。而现有批 8 已经是 2.9 倍实时（RTF 0.35）。**也就是说，在「离线一次 20 条、批量上限 8」这个负载下，换框架的净收益可能很小甚至为负**——vLLM-Omni 的长处是几十路并发和流式首包，不是单批 8 条的离线吞吐。

## 建议的三条路

### A. 栈内改造（先做，风险最低）

按投入产出排序：

1. **把 CUDA Graph 从 Predictor 扩到整个 Talker 单步**。现在 68.7 ms 里有 37 ms 是非图路径的 eager 执行，这是最大的一块可回收开销，也是 vLLM / TensorRT 路径里收益最大的单项（官方口径单这一项约 4 倍）。做法沿用 `qwen_cuda_graph.py` 的录制 / 重放 / 失败回退框架，把输入从 15 步子码本扩展到整步。
2. **Code2Wav 分块解码并与 Talker 重叠**。每帧 11.7 ms 全串行，理论上可隐藏在下一帧的 Talker 计算后面，上限约 14%。这也是 vLLM-Omni「stage 拆分」的核心思路，可以只借思路不换框架。
3. **批次按预估帧长分桶**。当前一批按最长句子的令牌上限算（20 字约 174 帧），一条长句会把整批拖长；把长度接近的句子排到同一批，能减少 padding 浪费，也能减少尾批被拆成 4 条（尾批 0.90 秒 / 条，是批 8 的 1.6 倍）。
4. **把已验证的 `predictor_cuda_graph` 打开**。README 里 20 条中文 14.27 → 7.15 秒的结论已验证过，只是默认关闭；同时确认批量上限 8、自动启动门槛 8。
5. **0.6B 作为「速度档」**。参数量 1/2.8，同硬件大致 2 倍以上吞吐，代价是音色细腻度；适合长文批量配音这种对音质要求不极端的场景。

### B. WSL2 + vLLM-Omni（作为可选 Linux 批量后端）

如果要验证框架路线，这是唯一有意义的形式，不要指望塞进 Windows 打包环境：

- 现有 worker 是「父进程 stdio 传 JSON」的协议，改成 HTTP 客户端即可对接 `/v1/audio/speech` 或 batch 端点，`batch_inference` 的调度、降批、回退逻辑可以原样保留在后端选择层。
- 音色侧用 vLLM-Omni 的预计算音色（`custom_voice_manifest.json` + safetensors），避免每次请求重跑参考音频编码——这正好对应项目里 `prompts` 缓存的思路，可以一次预计算、长期复用。
- 上线前必须先验证四件事：Turing 上能否起服务（尤其注意力后端）、11 GB 下的 batch 与 KV 余量、预计算音色后的单请求 RTF、批 8 的 RTF。任何一项不过，这条路就不成立。
- 验收口径：同 20 条中文、热模型、同音色同参数，和现有后端做 A/B，比秒 / 条与 RTF，不比首包延迟。

### C. 只看不做的

- **SGLang-Omni**：同样支持 Qwen3-TTS，但同样只跑 Linux，Turing 支持更不明确，暂时没有独立的验证价值。
- **M\***（arXiv 2026-11）：宣称 B=16 时吞吐是 vLLM-Omni 的 2.7 倍、SGLang-Omni 的 4 倍，属于研究系统，跟着看即可。
- **TensorRT Edge-LLM**：值得记住它的两个思路——CodePredictor 用 FP8/NVFP4，以及 CodePredictor 无草稿模型的投机解码（`--cp-spec-verify-size`，输出分布不变）。这两点在 Turing 上做不了（无 FP8，且 sm_75 不在支持列表），但「子码本预测投机解码」这个方向可以在现有专用循环里试。
- **`wllm-server` 之类的 Windows 原生替代**：第三方实现，无 TTS 支持，不作为选项。

## 进展：A-1 已落地（2026-11-16）

第 1 项「把整步 CUDA 图从 Predictor 扩到整个 Talker」已实现并实测，细节见 [`TALKER_GRAPH.md`](TALKER_GRAPH.md)：

| 每批 | 关整步图 | 开整步图 | 加速 |
|---|---|---|---|
| 2 | 2.030 s/条 | 0.973 s/条 | 2.09× |
| 4 | 0.989 s/条 | 0.500 s/条 | 1.98× |
| 8 | 0.502 s/条 | 0.285 s/条 | 1.76× |

批 8 的 RTF 由 0.161 降到 0.092。开关默认关闭（`talker_graph`），打开方式与回退边界见该文档；代价是同一 seed 不再与旧路径逐位相同（差异来自 SDPA 内核规约顺序，非模型逻辑）。

剩下可做的按投入产出是：A-2 Code2Wav 与 Talker 重叠（理论上限约 14%）、A-3 按帧长分桶（尾批 1.6× 的浪费）、A-4 把 `predictor_cuda_graph` 定为默认开启。

## 数据来源

- vLLM-Omni TTS 工程实践与 Qwen3-TTS 优化路径：<https://vllm-project.github.io/2026/06/23/vllm-omni-tts.html>
- vLLM-Omni Speech API（Base 克隆、batch、预计算音色）：<https://docs.vllm.ai/projects/vllm-omni/en/latest/serving/speech_api/>
- vLLM 安装要求（不支持原生 Windows、CC ≥ 7.5）：<https://docs.vllm.ai/en/latest/getting_started/installation/gpu/>
- vLLM-Omni 与 HF Transformers 的 Qwen3-TTS 对比：<https://developers.redhat.com/articles/2026/07/01/inside-vllm-omni-architecture-serving-qwen3-omni>
- RTX 5090 上 vLLM-Omni 实测（0.6B、流式、TTFA）：<https://www.storminthecastle.com/posts/qwen_tts_latency/>
- SGLang-Omni 支持模型与平台：<https://pypi.org/project/sglang-omni/>
- TensorRT Edge-LLM 的 Qwen3-TTS 管线与批量限制：<https://nvidia.github.io/TensorRT-Edge-LLM/latest/user_guide/examples/tts.html>
- CUDA 13 架构支持变更（Turing 保留）：<https://developer.nvidia.com/blog/navigating-gpu-architecture-support-a-guide-for-nvidia-cuda-developers>
- PyTorch cu130 构建的 sm_75 列表：<https://dev-discuss.pytorch.org/t/introducing-cuda-13-2-and-deprecating-cuda-12-8-release-2-12/3337>

## 边界说明

- 本文所有"收益倍数"都是他人硬件、他人负载、他人基线的数据，不能直接当作本机承诺；本机已有合批 + 专用循环 + Predictor 图 + 显存整理四层优化，和这些基线不在同一条起跑线上。
- 本机推算只用于判断方向（是否值得投入），需要在目标显卡上实测才能成为结论。
- 本轮没有在目标机器上实际跑过任何框架，也没有修改项目代码。
