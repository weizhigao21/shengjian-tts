# 本机服务接口

默认地址 `http://127.0.0.1:8765`。启动服务后可在 `/docs` 查看完整请求结构。

## 常用接口

| 方法与路径 | 用途 |
| --- | --- |
| `GET /health` | 服务版本、数据目录、能力、队列与引擎状态 |
| `GET /workflow` | 工作流接入信息、可用声音预设及配置版本 |
| `GET /voices`、`PUT /voices/{id}` | 查询、保存声音预设 |
| `DELETE /voices/{id}`、`POST /voices/{id}/restore` | 删除、恢复预设 |
| `GET /references`、`POST /references`、`PATCH /references/{id}` | 查询、导入、编辑参考声音 |
| `DELETE /references/{id}`、`POST /references/{id}/restore` | 删除、恢复参考声音 |
| `POST /jobs/draft`、`PATCH /jobs/{id}/document` | 创建作品、保存标题与草稿 |
| `POST /jobs`、`POST /jobs/{id}/generate` | 提交生成、向作品追加批次，返回 202 |
| `GET /jobs`、`GET /jobs/{id}` | 普通作品列表、片段进度与错误 |
| `DELETE /jobs/{id}`、`POST /jobs/{id}/restore`、`PATCH /jobs/{id}` | 删除、恢复、重命名作品 |
| `POST /jobs/{id}/retry`、`POST /jobs/{id}/cancel` | 重试失败片段、取消生成 |
| `POST /jobs/{id}/generations/{generation}/cancel` | 取消指定批次 |
| `GET /audio/{filename}`、`GET /logs/{id}/{index}` | 普通生成音频、错误详情 |
| `GET /generated-audio`、`POST /generated-audio/delete` | 查询与批量删除生成音频 |
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

## 引擎与模型版本

引擎和已安装模型的版本检查由用户触发。支持模型扫描在首次读取模型页、更换引擎或缓存超过一天时后台运行；同一引擎每次启动最多自动尝试一次，也可手动扫描。检查或扫描失败不移除当前引擎、模型或已知目录；下载继续使用固定文件大小和 SHA-256 校验。

`GET /models` 新增 `scan` 状态，包含 `scanning`、`checked`、`families`、`error`、`warnings`。目录条目的 `adapted` 表示能否用于声笺配音，`downloadable` 表示能否自动下载，两者独立。待适配单文件模型下载后仅登记文件，不创建预设；多文件包的 `downloadable` 为 `false`。扫描详情见 [MODEL_DISCOVERY.md](docs/MODEL_DISCOVERY.md)。

`engine.options` 包含 `installed_version`、`recommended_version`、`update_available`、`can_rollback`，安装状态包含 `target_version` 与 `upgrade`。升级下载、校验和启动检查完成后显示 `waiting`，当前生成结束才切换；暂停等待启用后，可继续安装，重启后也可启用已安装的新引擎。

`installed` 的模型记录提供 `catalog_id`、`revision`、`new_revision`、`update_available`、`update_status`、`can_rollback`。新版保存在 `models/versions/<摘要前缀>/`，校验包内模型能力后更新预设路径；旧文件保留，历史任务的旧路径仍可使用。回退需要旧文件仍存在，生成或下载期间拒绝回退。

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
