# 本机服务接口

默认地址 `http://127.0.0.1:8765`。启动服务后可在 `/docs` 查看完整请求结构。

音频编辑使用独立的项目与版本 ID。[操作说明](docs/AUDIO_EDITING.md)列出文件限制和模型要求。`POST /audio-editor/projects` 接收本机 `path`、可选 `name` 和 `original_text`，返回原始版本。处理请求的 `source_version` 必须来自同一项目已完成的版本：

```json
{
  "source_version": "从项目 versions 中取得的 ID",
  "operation": "render",
  "start": 0.5,
  "end": 3.5,
  "gain_db": -3,
  "fade_in": 0.1,
  "fade_out": 0.2,
  "state": {"name": "旁白后期", "original_text": "原台词", "target_text": "新台词"}
}
```

`end=null` 表示到音频末尾。`operation` 可取 `render`（保留选区并调整）、`append`（另填 `append_path`）、`vc`（Vevo2 换声）、`edit`（Vevo2 台词修改）。AI 请求携带 `model_path`、`device`（cuda / cpu）、`reference_path`、`steps`（默认 32）、`seed`（-1 随机）；`edit` 可省略参考音频沿用选区音色，`vc` 必须提供参考。处理返回 202 后轮询项目的 `versions`，状态为 queued / running / done / failed / cancelled，完成版本包含 `audio_url` 和音频详情。目录中的 `edit_adapted` 表示编辑支持，独立于文章 / 对话使用的 `adapted`。

## 常用接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 服务版本、数据目录、能力、队列与引擎状态 |
| `GET /workflow` | 工作流接入信息、可用声音预设及配置版本 |
| `GET /voices`、`PUT /voices/{id}` | 查询、保存声音预设 |
| `GET /parameter-presets`、`POST /parameter-presets` | 查询、创建高级参数预设（创建返回 201） |
| `PUT /parameter-presets/{identity}`、`PATCH /parameter-presets/{identity}`、`DELETE /parameter-presets/{identity}` | 更新参数、仅重命名、删除参数预设 |
| `DELETE /voices/{id}`、`POST /voices/{id}/restore` | 删除、恢复预设 |
| `GET /references`、`POST /references`、`PATCH /references/{id}` | 查询、导入、编辑参考声音 |
| `DELETE /references/{id}`、`POST /references/{id}/restore` | 删除、恢复参考声音 |
| `POST /jobs/draft`、`PATCH /jobs/{id}/document` | 创建作品、保存标题与草稿 |
| `POST /jobs`、`POST /jobs/{id}/generate` | 提交生成、向作品追加批次，返回 202 |
| `GET /jobs`、`GET /jobs/{id}` | 普通作品列表、片段进度与错误 |
| `GET /recent-works` | 最近 100 个文章、对话、音频编辑和识别任务的只读摘要，按创建时间倒序 |
| `DELETE /jobs/{id}`、`POST /jobs/{id}/restore`、`PATCH /jobs/{id}` | 删除、恢复、重命名作品 |
| `POST /jobs/{id}/retry`、`POST /jobs/{id}/cancel` | 重试失败片段、取消生成 |
| `POST /jobs/{id}/generations/{generation}/cancel` | 取消指定批次 |
| `GET /audio/{filename}`、`GET /logs/{id}/{index}` | 普通生成音频、错误详情 |
| `GET /generated-audio`、`POST /generated-audio/delete` | 查询与批量删除生成音频 |
| `GET /audio-editor/options`、`POST /audio-editor/models` | 编辑可用模型 / 参考声音与本地 Vevo2 GGUF 导入 |
| `GET /audio-editor/projects`、`POST /audio-editor/projects` | 编辑项目列表、导入 PCM WAV |
| `GET /audio-editor/projects/{id}`、`PATCH /audio-editor/projects/{id}` | 编辑版本列表、保存名称 / 台词 / 当前版本 |
| `POST /audio-editor/projects/{id}/process`、`POST /audio-editor/projects/{id}/cancel` | 后台音频处理（202）、取消处理 |
| `GET /audio-editor/audio/{filename}` | 读取管理目录内的编辑版本 WAV |
| `GET /recognition/options`、`POST /recognition/selection` | 识别可用的 ASR / 对齐器模型、引擎与语言；保存默认选择 |
| `GET /recognition/sessions`、`POST /recognition/sessions` | 识别记录列表、导入音频并开始识别（返回 201） |
| `GET /recognition/sessions/{id}`、`PATCH /recognition/sessions/{id}` | 识别状态、文本与分行；保存名称 |
| `POST /recognition/sessions/{id}/retry`、`POST /recognition/sessions/{id}/cancel` | 重新识别、取消当前识别 |
| `DELETE /recognition/sessions/{id}` | 删除识别记录及其音频副本 |
| `GET /recognition/audio/{filename}` | 读取管理目录内的识别音频 |
| `GET /models`、`POST /models/import` | 模型目录、下载状态、导入 GGUF |
| `POST /models/scan` | 后台扫描当前引擎声明及官方 GGUF 目录，返回 202；使用 `source` 字段 |
| `POST /models/{id}/download`、`POST /models/{id}/pause`、`POST /models/{id}/remove` | 下载、暂停、移除模型 |
| `PUT /models/runtime` | 设置本地统一引擎路径 |
| `POST /models/runtime/download`、`POST /models/runtime/pause`、`POST /models/runtime/use` | 下载、暂停、启用引擎 |
| `POST /models/runtime/verify` | 验证引擎、计算后端和 DLL 依赖 |
| `POST /models/runtime/check-updates` | 检查 audio.cpp 官方正式版，不下载程序 |
| `POST /models/runtime/upgrade`、`POST /models/runtime/rollback` | 升级或回退引擎，使用 `backend` 字段 |
| `POST /models/check-updates` | 检查已知 GGUF 文件更新，使用 `source` 字段 |
| `POST /models/{id}/update`、`POST /models/{id}/rollback` | 更新或回退已安装模型；`id` 来自 `installed`，更新请求携带 `source` |
| `GET /settings/performance`、`PUT /settings/performance` | 读取、设置 `workflow_workers` 与 `cpu_threads` |
| `POST /engine/release` | 释放空闲推理引擎 |

引擎操作使用 `{"backend":"cuda"}` 或 `{"backend":"cpu"}`。`GET /models` 中的 `engine.options` 提供分项下载与验证状态。

## 高级参数预设

创建 / 更新请求包含 `name`（1–100 字）、`model`（现有 `VoiceConfig` 结构，用于读取当前模型能力）和 `request_options`（当前模型支持的高级参数）。服务仅存储名称、ID、生成参数及模型关联描述，不保存传入的音色、参考声音或运行配置，也不会更新声音预设。响应包含 `id`、`name`、`model`、`model_key`、`request_options`、`created` 和 `updated`。重命名请求仅含 `name`；删除返回 `{"deleted":"参数预设 ID"}`。

预设按后端、模型家族、模型路径、模型变体及官方 IndexTTS 版本区分，与音色 ID、引擎路径和计算设备无关。同一模型的名称不区分大小写且不能重复；更新不能改变模型关联。查询返回全部预设，客户端按 `model_key` 筛选。参数预设不存在返回 404，非法值 / 不支持的参数或重复名称返回 400，请求结构错误返回 422。保存预设不启动推理，套用由客户端明确操作，并按当前模型的参数契约再次检查；生成时仍使用现有 `overrides.request_options`。

## 引擎与模型版本

引擎和已安装模型的版本检查由用户触发。支持模型扫描在首次读取模型页、更换引擎或缓存超过一天时后台运行；同一引擎每次启动最多自动尝试一次，也可手动扫描。检查或扫描失败不移除当前引擎、模型或已知目录；下载继续使用固定文件大小和 SHA-256 校验。

`GET /models` 新增 `scan` 状态，包含 `scanning`、`checked`、`families`、`error`、`warnings`。目录条目的 `adapted` 表示能否用于声笺配音，`downloadable` 表示能否自动下载，两者独立。待适配单文件模型下载后仅登记文件，不创建预设；多文件包的 `downloadable` 为 `false`。扫描详情见 [MODEL_DISCOVERY.md](docs/MODEL_DISCOVERY.md)。

`catalog` 和 `installed` 的 `published_at` 为当前 GGUF 文件版本的仓库提交时间，使用 UTC ISO 8601；`publication_commit` 为日期来源的文件提交 ID。无法确认时返回 `null`。已安装模型使用实际文件版本的日期，不随可下载的新版本变化。

`engine.options` 包含 `installed_version`、`recommended_version`、`update_available`、`can_rollback`，安装状态包含 `target_version` 与 `upgrade`。升级下载、校验和启动检查完成后显示 `waiting`，当前生成结束才切换；暂停等待启用后，可继续安装，重启后也可启用已安装的新引擎。

`installed` 的模型记录提供 `catalog_id`、`revision`、`new_revision`、`update_available`、`update_status`、`can_rollback`。新版保存在 `models/versions/<摘要前缀>/`，校验包内模型能力后更新预设路径；旧文件保留，历史任务的旧路径仍可使用。回退需要旧文件仍存在，生成或下载期间拒绝回退。

`GET /models` 的 `catalog` 条目增加 `speed_bps`（字节 / 秒），模型更新的已安装条目提供 `update_speed_bps`。它们按本次下载实际传输的字节计算，断点续传的已有文件不计入；暂停、校验、完成或停滞三秒后为零。下载状态仍使用原 `status` / `received` 与暂停、下载、更新接口，界面据此构建独立任务区域。

## 语音识别

侧栏使用 `GET /recent-works` 汇总各模式。文章 / 对话保留原作品 ID；音频编辑与识别摘要分别使用 `audio:<id>`、`recognition:<id>` 作为列表 ID，并通过 `source_id` 返回原 ID、`task_type` 返回模式。外部任务的详情仍从 `/audio-editor/projects/{source_id}` 或 `/recognition/sessions/{source_id}` 读取。汇总只读现有数据库，不迁移或合并数据库，不返回工作流临时记录；原 `/jobs` 结构不变。

识别复用统一引擎：`GET /recognition/options` 按引擎的 `model_specs` 任务声明把已安装模型分为 `asr` 与 `align` 两组，并返回 `runtime`、`selection`、`languages` 和当前是否 `active`。`POST /recognition/selection` 的全部字段可选，只覆盖传入项，`asr_model` / `align_model` 已不存在的路径会被剔除。

```json
{
  "path": "本机音频路径",
  "name": "显示名称",
  "language": "zh"
}
```

`POST /recognition/sessions` 复制音频到数据目录并返回 201 与会话 ID；未选择或缺少 ASR 模型时返回 400，格式或路径无效时返回 400。会话状态为 `queued` / `running` / `done` / `failed` / `cancelled`，`done` 时包含 `text`、`lines`、`duration`、`warning`、`recognized_language`、`timing` 和 `audio_url`。

`lines` 每项含 `index`（从 1 开始）、`start`、`end`（秒，相对音频开头）与 `text`，可直接用于点击播放；时间戳来自引擎的 16 kHz 采样帧号换算，分行按句末、从句、长度、时长与停顿综合判断。未安装强制对齐器时按标点估算每行时间，并通过 `warning` 说明。`language` 可取 `auto` 或 `recognition/options` 中给出的代码。

识别与音频编辑共用管理器的推理互斥，`GET /health` 的 `recognition` 字段显示当前会话 ID；识别期间 `POST /engine/release` 和 `PUT /models/runtime` 返回 400。删除会话会同时删除该音频副本，重新识别沿用同一会话并覆盖上一次结果。详见[语音识别说明](docs/SPEECH_RECOGNITION.md)。

语音识别导入时，PCM WAV 保存独立副本，其他支持格式转换为私有 PCM WAV。推理引擎被配音、识别或 AI 编辑占用时拒绝新识别，返回 400，不留下等待记录或音频副本；重试被拒绝时保留原状态。成功提交固定本次模型与语言，取消旧版残留的等待记录会将其标记为已取消。

v0.7.41 中，自动判断模式优先保留 ASR 返回的具体语言，手选语言优先；`recognized_language` 表示实际用于对齐的语言名称。没有文字时跳过对齐，语言未知 / 不受支持或对齐失败时仍返回 `done` 与已识别的文字，`warning` 明确说明时间估算；取消返回 `cancelled`。升级前失败的记录可用原 `/retry` 接口重新识别。

v0.7.42 桌面端可右键识别结果保存参考声音：后台裁剪所选的 1–60 秒连续音频，在原参考声音窗口确认后调用现有 `POST /references`。整段入库入口位于当前音频名称的右键菜单。此功能不新增识别接口，不修改识别数据库或原音频；库中保存独立副本。

## 生成与工作流

### IndexTTS 表达方式

v0.7.26 起，IndexTTS 2.0 / 2.5 的预设、全局 `overrides` 和对白 `overrides` 支持以下可选字段，旧预设无需迁移。

| 字段 | 说明 |
| --- | --- |
| `emotion_mode` | `speaker` 跟随音色、`reference` 参考演绎、`vector` 情绪调节、`text` 文字引导；默认 `auto` 按旧 `emotion` 是否为空选择跟随音色或文字引导 |
| `emotion_strength` | 0–1，默认 0.6；跟随音色模式不使用此值 |
| `emotion_reference_audio` | 演绎参考 WAV 路径，1–60 秒；与 `reference_audio` 的音色参考分开 |
| `emotion_vector` | 八个 0–1 的值，顺序为高兴、愤怒、悲伤、恐惧、厌恶、低落、惊讶、平静 |
| `emotion` | 文字引导中的情绪描述，最多 500 字 |
| `emotion_infer_from_text` | 文字引导中从当前片段正文推断情绪，默认 `false`；开启时忽略 `emotion` |

四种模式互斥，仅发送当前模式的参数；12 个按钮是八维组合的快捷预设。正文推断需要模型包内的情绪语言模型。演绎参考在提交时复制到数据目录并参与缓存校验，排队后移动原文件不影响生成；切换模式不会覆盖音色参考。

普通创作提交示例；`voice_id` 来自 `/voices`：

```json
{
  "voice_id": "预设ID",
  "title": "作品名称",
  "segments": [{"text": "你好，欢迎使用声笺。", "timestamp": "00:00:00.00"}],
  "overrides": {"request_options": {"seed": 123}}
}
```

固定种子只适用于支持该参数的模型；`-1` 表示随机。提交返回 `job_id`、`generation`、`segment_indices` 和 `status_url`。查询时可使用 `GET /jobs/{id}?generation=<编号>`，只看当前批次。`use_preset:false` 表示未使用预设，默认值为 `true`。

v0.7.21 起，片段可选填 `voice_id` 指定已有声音预设：`{"text":"这句话使用另一个音色","voice_id":"speaker-id"}`。该片段使用预设保存的完整配置，不叠加整批 `overrides`；未指定的片段沿用原整批配置。不同音色仍按原文顺序在一个作品、一个批次中生成，片段 `voice` / `provenance` 记录实际配置。客户端需先检查 `/health` 中的 `script_voice_mentions` 能力。

作品创建、保存及生成请求支持可选 `script_mentions`：`[{"start":0,"end":3,"preset_id":"speaker-id","label":"御姐"}]`，用于重开编辑器时恢复 `@御姐` 的身份，偏移按 Qt UTF-16 单位计算。此字段仅保存编辑标签；推理以片段 `voice_id` 为准。旧客户端无需新增字段，普通文稿更新未携带标签时会清空旧标签，避免误用声音。

v0.7.22 增加可选 `editor_state`，保存 `mode`（`article` / `dialogue`）、`article_text`、`article_mentions` 和 `dialogue_entries`（每条含 `id`、`text`、`voice_id`、`voice_name`）。对白提交仍使用原生成接口；片段的可选 `source_id`、`source_revision` 关联条目及提交版本，`force_generate: true` 执行新推理并保留旧音频。未更新文稿的追加生成沿用作品当前编辑状态；旧客户端仍可按原字段提交普通文章。

v0.7.23 的对白条目可保存 `model_id` 和 `overrides`；片段可填局部 `overrides`，只叠加在这一条的模型 / 预设上。使用未保存的模型设置时，以模型配置 ID 填写 `voice_id`，同时设置 `use_preset: false`。这些能力在 `/health` 中标为 `dialogue_audio_scope`，整批覆盖不会修改其他对白。

作品查询增加 `work_type` 和 `display_title`；生成音频记录增加 `content_type`（`article` / `dialogue`）、`source_label` 与 `audio_label`。完整对白 ID 仍是 `source_id`，短 ID 只用于显示。两类记录保存在同一 SQLite，通过 `article_audio_records` / `dialogue_audio_records` 视图分类。

v0.7.24 增加能力标记 `fixed_creation_type`：任务类型在创建时的 `editor_state.mode` 中确定，未提供时按文章处理。保存文稿或追加生成不能改变类型，否则返回 HTTP 400、`TASK_TYPE_LOCKED`；省略 `editor_state` 时保留已有状态。另一种类型应创建新任务，旧记录不受影响。

v0.7.28 增加 `dialogue_role_names`：对白条目可填 `role_name`（最多 100 字），例如 `{"id":"row-1","role_name":"姐姐","text":"你好"}`。角色名属于显示信息，改名不改变 `source_id`、提交版本或声音快照。同名角色按 ID 区分；有名称时，查询片段和生成音频列表返回当前 `source_name`，`audio_label` 优先显示角色名加音色，未命名或已移除的对白沿用短 ID。完整 ID 始终保留，旧客户端无需新增字段。

v0.7.19 起，生成时按当前模型定义兼容已知旧参数：Qwen 的旧 `text_chunk_mode:default` 在新版不再提供此参数时省略；VoxCPM2 的 `voxcpm2.chunk_strategy` / `chunk_strategy` 按引擎契约转换。原预设及版本标签保留，新生成记录保存实际使用的参数；未知参数、非默认的旧分段方式及无效值仍报错。

片段与生成音频列表中的 `generation_seconds` 为本次推理调用至 WAV 校验完成的耗时（秒），含首次模型加载，不含排队；`duration` 为音频时长。缓存复用时 `cached:1`、`generation_seconds:0`，旧记录或尚未推理时耗时为 `null`。失败重试记录本次尝试的耗时。

双语工作流另加 `record_only:true`、`delivery_ack:true` 和同一次运行共用的 `workflow_key`。生成记录保存模型、文本、预设、参数、时间和错误，侧栏不创建作品。工作流适配器要求声笺 v0.7.11+。

| 方法与路径 | 工作流用途 |
| --- | --- |
| `GET /workflow/records?limit=200` | 查询生成记录及音频清理状态，最多 500 条 |
| `GET /workflow/audio/{filename}` | 下载临时 WAV |
| `POST /workflow/audio/{filename}/release` | 保存、校验成功后确认接收，删除临时音频；重复调用安全 |
| `POST /workflow/records/cleanup` | 清理历史工作流音频，保留数据库记录；生成期间拒绝清理 |

启用接收确认后，先将 WAV 保存到工作流自己的输出目录并校验，再调用 `release`。未接收音频在整批完成 1 小时后清理，重启继续执行；取消批次会清理该批未接收的音频。普通作品或参考声音仍在使用的共享文件会保留。

兼容旧 `/infer_single`、`/infer_bulk`、`audio_url` / `audio_urls`。旧请求中已知工作流标题或 `workflow_key` 自动启用只记录模式；没有接收确认的旧适配器完整下载后清理，`HEAD`、`Range` 和断连保留重试机会。

## 错误与删除

失败响应提供 `error.code`、`stage`、`message` 和 `hint`；片段错误也保存在任务详情与日志中。生成中不能修改性能设置或删除正在使用的模型。

作品和声音库支持恢复删除。模型文件与生成音频的永久删除需要确认；音频删除应提交确认时的 ID 列表，避免删除随后新完成的音频。只有最后一个生成引用被移除时才清理共用 WAV 与缓存，文件占用失败保留记录以便重试。
