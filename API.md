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
| `POST /models/{id}/download`、`POST /models/{id}/pause`、`POST /models/{id}/remove` | 下载、暂停、移除模型 |
| `PUT /models/runtime` | 设置本地统一引擎路径 |
| `POST /models/runtime/download`、`POST /models/runtime/pause`、`POST /models/runtime/use` | 下载、暂停、启用引擎 |
| `POST /models/runtime/verify` | 验证引擎、计算后端和 DLL 依赖 |
| `GET /settings/performance`、`PUT /settings/performance` | 读取、设置 `workflow_workers` 与 `cpu_threads` |
| `POST /engine/release` | 释放空闲推理引擎 |

引擎操作使用 `{"backend":"cuda"}` 或 `{"backend":"cpu"}`。`GET /models` 中的 `engine.options` 提供分项下载与验证状态。

## 生成与工作流

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
