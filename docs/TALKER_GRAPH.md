# Talker 整步 CUDA 图 · 实现与实测 · 2026-11-16

把主模型（Talker）**每帧的整步前向**录进一张 CUDA 图，取代 SDK 的逐帧 eager 调用。这是《批量推理加速框架调研》里 A 路线的第 1 项：单帧 83 ms 中有 37 ms 花在非图路径的 Talker eager 执行上，比已经做了图的编码预测器更值钱。

**结论：批 8 从 0.502 s/条 降到 0.285 s/条（1.76×），批 2 从 2.03 降到 0.97（2.09×）；代价是同一 seed 不再与关闭开关时逐位相同（差在 fp16 内核规约顺序，不是模型或采样逻辑）。**

## 怎么做的

开关是 `schemas.BatchSettings.talker_graph`（默认 `False`，设置页在「批量后端」里有对应复选框）。打开后 worker 用 `qwen_talker_graph.install()` 接管 `talker.generate`，其余（合批、编码预测专用循环、Code2Wav、音色缓存）都不变。

CUDA 图要求形状固定，而 SDK 用的是逐帧增长的 `DynamicCache`，所以这里把解码循环自己重写了一遍：

- 一块预分配的 `StaticCache`，长度 = 提示长度 + `max_new_tokens`；每批开始把提示整段 eager 预填进去。
- 每帧只填 4 个静态缓冲区（帧 embedding、`cache_position`、`position_ids`、4D 注意力掩码），然后一次 `graph.replay()`。缓存写入在注意力之前发生，所以当帧的 K/V 已在图内，和 eager 语义一致。
- **采样不进图**：`torch.multinomial` 留在 eager，采样链与 `transformers` 的 `_sample` 完全同序（惩罚 → `min_new_tokens` → 抑制词表 → temperature/top_k/top_p → softmax → 多项式采样），这样 RNG 序列与 SDK 一致。
- **一图绑定一份缓存**：图读的是录制时的 buffer 地址和那块 `StaticCache`。同形状的下一批复用同一块缓存，并 `prepare()` 重写整张掩码（不同批的填充长度不同）。这条是踩过坑才定下来的，见下文第 3 条。
- 回退分三层：形状/预算/版本不符 → 整批先判后放、直接走 SDK 原循环；录制失败 → 该形状永久记为失败、之后不再尝试；RNG 状态无法恢复 → 抛 `GraphStateError`，让 worker 退出重载而不是带着不确定状态重试。

## 实测

环境：RTX 2080 Ti 11 GB / Windows / torch 2.9.0+cu128 / `qwen-tts` 0.1.1 / `transformers` 4.57.3 / FP16 / SDPA / seed 1234。负载：本文的 8 条中文短句（约 3.1 s/条），每批一次送满。编码预测专用循环 + 编码预测图均已开启（这是本项目的默认推荐组合）。数据通过 worker 的 stdio 协议（`BatchProcess.generate`）测得，不是脚本内联调用，热模型、每组跑 3 次取最好。

| 每批 | 关整步图 | 开整步图 | 加速 | 每帧（重放数） | 音频总长（关 / 开） |
|---|---|---|---|---|---|
| 2 | 4.06 s · 2.030 s/条 | 1.95 s · 0.973 s/条 | **2.09×** | 44 | 7.12 / 6.88 s |
| 4 | 3.95 s · 0.989 s/条 | 2.00 s · 0.500 s/条 | **1.98×** | 43 | 13.84 / 13.36 s |
| 8 | 4.01 s · 0.502 s/条 | 2.28 s · 0.285 s/条 | **1.76×** | 45 | 24.96 / 24.88 s |

批 8 的 RTF 从 0.161 降到 **0.092**（10.9 倍实时）。批越小，Talker 单步在总耗时里占比越高，所以批 2 的加速比更大；批 8 已经有编码预测图和 Code2Wav 并行占着剩下的时间，收益被摊薄。

录制成本：一批 ≤ 0.5 s（含 3 次预热 + 一次捕获），只在形状第一次出现时付一次；同一形状后续批次直接用。模型冷加载不计入这里。

关闭 / 开启两版的同一批音频留了 8 对在 `docs/bench-talker-graph/`（`off-01..08.wav` 与 `on-01..08.wav`），可以对着听。

## 一致性：能对到什么程度，为什么不能逐位一致

先说验证过的部分（都是逐位对照，不是目测）：

| 对照 | 结果 |
|---|---|
| `DynamicCache` vs `StaticCache`（真实左填充，含预填 + 一步解码） | hidden `0.00e+00` |
| 自建 4D 掩码 vs `transformers` 自建掩码（同一 `StaticCache`） | `0.00e+00` |
| 图回放 vs 同一 buffer 的 eager 调用（双缓存、连续 6 步） | hidden 与 logits 均 `0.00e+00` |
| 缓存 probe 两次同输入 | `0.00e+00`（可复现） |

也就是说缓存数学、掩码、RoPE 位置、图捕获本身都是对的。但**整批产物仍不会和 SDK 逐位相同**：

- 预填的 logits 就已经有 ~1e-3 的概率差（top-1 相同、最大概率 0.976 vs 0.975），从某帧开始 token 分叉；8 条短句最早第 1 帧、最晚第 32 帧开始不一样。
- 根因不是逻辑，是内核：预分配缓存让每步注意力都落在一张 200+ 长的掩码上，而 SDK 的 `DynamicCache` 在**无填充**（单条）时直接走 `is_causal` 快路径、有填充时掩码长度也只到 `past+1`。同一份数学换了 SDPA 内核与规约顺序，FP16 下就是 1e-3 级差异，而 `do_sample=True / temperature=0.9` 的采样器对 1e-3 很敏感。
- 实际影响：帧长与音频时长差在 ±3 帧（±0.25 s）内，音频总长差 0.3%（24.96 vs 24.88 s）；听感不是「变差」，是「另一种同样合法的采样结果」。
- 运维含义：**同一 seed 不再保证与关闭开关时逐字节相同**。成品音频缓存、A/B 对比脚本要按「同参数不保证同音频」处理；要严格复现旧产物，就把开关关掉再跑。

如果一定要逐位相等，只有两条路：让 SDK 也走「固定长度掩码」（改 SDK 内部，风险更大），或者放弃图的形状固定（等于不做图）。所以这里选择了「可接受的等价」并把这个前提写进文档和界面提示。

## 踩过的坑（值得记下来）

1. `SuppressTokensLogitsProcessor` 默认在 CPU 上建张量，CUDA 上 `torch.isin` 会报设备不一致；必须传 `device=str(device)`。`MinNewTokensLengthLogitsProcessor` 的 eos 同理要给 device 张量。
2. 预测器输入是「上一帧 hidden（1 个 token）+ 当前 token embedding」，把整段预填 `last_hidden_state` 拼进去会让预测器 `lm_head[15]` 越界（`index 15 is out of range`）。
3. **图必须和它的缓存同生共死**。最初按形状缓存图，第二批复用了上一批的 `StaticCache`，新批的填值根本没进图（探针里 `position_ids` 恒为 0、可见位恒为 10），表现为帧长从 47 暴涨到 114、而且更慢。现在每个图条目自带 cache，换批复用同一块并重写掩码。
4. `rope_deltas` 必须在**录制之前**算好：录制时的预热帧已经在读它，否则 `fill()` 抛 `int + None`。
5. 帧数上限要按显存卡：SDK 默认 `max_tokens` 能给到 8192，`cache_len` 8202 帧 × 8 条 ≈ 7 GiB，直接超预算。回退理由串现在带回实际数字（`8202 帧 × 8 条 ≈ 7176 MiB，预算 640 MiB`），用户看得懂为什么没加速。

## 开关、预算与回退边界

- 显存预算：静态缓存在批开始就整块分配，上限 `CACHE_BUDGET = 640 MiB`（按 `cache_len × 条数 × 每帧 K/V 字节` 估算）。本机批 8、45–57 帧的负载跑下来没有 OOM；峰值显存没有单独仪表化，判据就是这条预算加实际不 OOM。
- 位置上限：`cache_len ≤ max_position_embeddings`（32768）。
- 条数：≤ 8（与合批上限一致）。
- 精度/设备：只认 CUDA + FP16。
- 版本：`qwen-tts 0.1.1` / `transformers 4.57.3` / `torch 2.9.0`，任一不符就不启用（不是「试了再说」，避免在没验证的组合上产出可疑音频）。
- 降级：整批每帧耗时超过历史最快批 3 倍（例如显存吃紧开始走 WDDM 分页）→ 后续批次回 SDK 原循环，理由写进状态。
- 状态上报：worker 返回 `talker_mode`（`graph` / `standard`）、`talker_reason`、`talker_graph_replays`、`talker_graph_captures`、`talker_graph_fallbacks` 等字段，设置页与「推理状态」里能看到本批是否真的走了图。

## 回归

- `tests/test_qwen_talker_graph.py`（20 个 CPU 用例）：默认关闭、设备/开关门禁、版本门禁、9 类不支持调用先判后放、不可用图时恢复 RNG 并回退、RNG 丢失必须抛错、慢批降级、**条目自带缓存且同形状复用**（针对第 3 条坑的回归）、失败形状不再重录、eager 模式复用同一 buffer。
- 全量 `pytest tests`：547 passed / 1 skipped / 1 failed，失败项是 `tests/test_native_paths.py::test_shared_mapping_defers_only_owned_aliases_and_retries_after_all_runners_stop`（原生引擎硬链接别名），相关文件本次未改动（10-13 的时间戳），与整步图无关。
- 一致性回归脚本：`cache_probe`（DynamicCache vs StaticCache，期望全 `0.00e+00`）、`graph_probe`（图回放 vs 双缓存 eager，期望全 `0.00e+00`）—— 在调这块逻辑时先跑这两个，能立刻区分「缓存/掩码错」还是「循环逻辑错」。

## 下一步还能做什么（按投入产出）

1. **Code2Wav 与 Talker 重叠**：每帧 11.7 ms 全串行，理论上限约 14%，可以和一个专门的解码流配合。
2. **按帧长分桶**：现在按最长句子的令牌上限算整批帧数，一条长句拖整批；尾批降到 4 条时 0.90 s/条，是批 8 的 1.8 倍。
3. **静态缓存预算自适应**：按空闲显存把 `max_new_tokens` 上限抬到更合理的值，避免长句被误判超预算而回退。
4. 0.6B 速度档（调研文档里 A-5）：与整步图叠加是乘法关系，但音色细腻度会降，属于产品取舍。
