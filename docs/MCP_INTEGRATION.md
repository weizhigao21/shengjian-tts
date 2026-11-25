# MCP 接入设计

版本：0.10.1

## 目标

让外部 AI 客户端通过 MCP 使用声笺已有的能力，而不把声笺变成一个可以随意操作本机的通用 Agent。

这一阶段只做 **MCP Server**：Claude Desktop、Cursor、Cline 等客户端可以调用声笺的语音生成、识别和翻译知识库。声笺主动调用别家 MCP 工具的 Client 能力放在下一阶段，需要按具体服务单独配置。

## 为什么不是直接在 MCP 进程里开数据库

MCP 进程只做协议翻译，所有读写都转给已经在运行的声笺 HTTP 服务：

- 不创建第二个 `Manager`，不抢同一个 `ServiceLock`，不重复加载模型。
- 术语、翻译记忆、识别记录仍由服务层统一校验和落库。
- MCP 进程退出不会影响正在生成的语音或翻译任务。

代价是使用前必须先打开声笺，或运行 `python -m voice_workbench serve`；MCP 的报错会明确提示这一点。

## 传输

- stdio，逐行 JSON-RPC 2.0，不监听端口。
- 支持 `initialize`、`ping`、`tools/list`、`tools/call`，未识别的通知直接忽略。
- 服务地址默认 `http://127.0.0.1:8765`，可用 `SHENGJIAN_API` 覆盖。
- 单次生成等待上限由 `SHENGJIAN_MCP_WAIT` 控制；超时返回 `job_id`，而不是假装完成。

## 工具范围

### 语音

`list_voices`、`speak`、`speak_dialogue`、`get_job`、`transcribe`。音频只返回本机绝对路径，不返回 HTTP 链接。

### 字幕与翻译

`list_translation_sessions`、`get_subtitle`、`correct_subtitle`、`list_translation_terms`、`add_translation_term`、`update_translation_term`、`disable_translation_term`、`list_translation_memory`、`add_translation_memory`、`update_translation_memory`、`delete_translation_memory`、`translate_subtitle`、`save_translation`、`list_translation_runs`、`get_translation_run`。

这些工具只调用现有接口：

- 术语新增/修改仍按 `source + source_lang + target_lang` 去重。
- 术语和记忆删除仍是软删除。
- `translate_subtitle` 调用确定性翻译 Agent：查术语、复用记忆、质检、按 20 行分批翻译，失败不覆盖旧译文；成功默认只回回执，`include_translation=true` 才回显全文。
- `save_translation` 接收外部 Agent 自己模型产出的完整译文：必须覆盖所有原始行号、不能重复、不能改原文或时间码；`target_lang` 必填，默认只回 `line_count/translated_lines/revision/run_id/issues/memory_written` 回执。
- 运行记录保留 trace，最多保留最近 100 条。

## 0.10.1 Token 约定

`get_subtitle` 是读字幕的主入口，默认按 token 预算返回：

- `format` 默认 `compact`，返回 `columns` + `lines` 数组；`columns` 默认 `["i","text"]`，只有 `include_timecodes=true` 才加 `start/end`。
- `cursor` 从 0 开始，`limit` 默认 50、上限 200；返回 `next_cursor` 与 `done`，按块读完。
- `include_translation` 在 compact 下默认 false；需要读已有译文时传 true，columns 追加 `translated`，并返回 `translation_meta`。
- 返回 `revision`（12 位源文哈希）。下次传 `known_revision`，原文未变时直接返回 142B 的 `{"unchanged":true,…}`，不再重读全文。
- 旧客户端传 `format="objects"` 仍得到 0.9.8 的 `lines` 对象数组和合并译文。

`save_translation` 的 `target_lang` 必填；`zh-CN/zh_CN/zh-Hans/zh-TW` 会归一为 `zh`，`en-US`、`ja-JP` 同理。回执里的 `revision` 可传给下一次 `get_subtitle` 的 `known_revision`，避免重复读。

430 行真实会话实测：`get_subtitle` 62.9KB → 15.7KB（50+200+180 三块），`save_translation` 回执 39.4KB → 约 0.2KB，`tools/list` 9.6KB → 8.9KB。

## 安全边界

MCP 不提供以下能力：

- 删除识别记录、音频或声音库数据。
- 读写任意文件、执行命令、修改配置。
- 读取 API Key；密钥只留在服务进程的配置里。
- 绕过术语校验直接改数据库。

MCP 的写工具覆盖术语、翻译记忆、整条译文回写和字幕文本修正，且术语/记忆删除是软删除。`correct_subtitle` 只改文字、不改时间码，并清除旧译文；外部 Agent 不能借字幕内容删除作品或访问文件系统。

## 用外部 Agent 的免费额度翻译

不需要让声笺实现一个通用 MCP Client，也可以反过来：让豆包工作、WorkBuddy 这类支持 MCP 的客户端连接声笺的 MCP Server，由客户端自己的模型完成翻译，再把译文回写。

流程：

1. 在外部客户端添加声笺 MCP Server（命令或地址见下一节）。
2. 外部 Agent 调 `list_translation_sessions` 找到记录，再按 `cursor/limit` 分块调 `get_subtitle` 读取原文；需要时间码时传 `include_timecodes=true`。
3. 外部 Agent 用自己的模型和免费额度逐行翻译，保持每行 `index`。
4. 外部 Agent 调 `save_translation`，传 `session_id`、必填的 `target_lang` 和完整 `lines`，按原始 `index` 回写。
5. 声笺校验行号、保留时间码、做术语/数字质检，然后把结果写进识别记录，并把合格行写入本地翻译记忆。
6. 主界面「译文」标签自动出现（桌面端空闲时最多约 8 秒刷新）；已打开的翻译窗口和记忆库每 1.5–2.5 秒自动刷新。

这样免费额度消耗在外部客户端，声笺不调用自己的翻译 API；外部 Agent 也不能修改字幕原文，只能提交译文。

## 批量识别后怎么翻译

批量识别会把每个文件建成一条独立的识别会话，但同一个批次里的会话共享 `batch_id`。外部 Agent 不需要在声笺里逐个打开结果，可以按下面流程批量处理。声笺的独立批量模式页提供「复制批次 ID」和「复制 MCP 指令」，复制后直接交给外部 Agent 即可。
1. 等批量识别跑完，调 `list_translation_sessions`，按 `batch_id` 分组；只取 `status="done"` 的会话。返回里的 `translated_lines` 和 `line_count` 可以判断哪些还没翻译。
2. 对每条会话按 `cursor/limit`（默认 50 行）调 `get_subtitle`，拿到 `lines`（compact 为 `[index, text]`；需要时间码时 `columns` 为 `[i,start,end,text]`）。
3. 用外部客户端自己的模型翻译，保持每一行的 `index`，不要合并或拆分行。
4. 对每条会话分别调一次 `save_translation`，传 `session_id`、`target_lang` 和完整 `lines`；行数必须和原文一致。回执只给 `line_count/translated_lines/revision`，需要检查全文再回读。
5. 声笺写入后，识别页的「译文」标签会显示，并自动写入翻译记忆；识别页有独立的「导出中文字幕」按钮，批量页选中记录后也能单独导出译文。批量识别时已经自动导出的 SRT 仍是原文，不会被自动替换。

`list_translation_sessions` 现在支持 `batch_id` 和 `status` 参数，`get_subtitle` 也返回 `batch_id`/`batch_index`。不要对批量会话调用 `translate_subtitle`，那样会消耗声笺自己的翻译 API 额度；批量外部翻译的正确路径是 `get_subtitle` → 外部模型翻译 → 每个 `session_id` 调 `save_translation`。

## 客户端配置

源码环境：

```json
{
  "mcpServers": {
    "shengjian": {
      "command": "E:\\code\\集成文本生语音程序\\.venv\\Scripts\\python.exe",
      "args": ["-m", "voice_workbench", "mcp"]
    }
  }
}
```

发行包：

```json
{
  "mcpServers": {
    "shengjian": {
      "command": "C:\\DIY\\声笺\\声笺服务.exe",
      "args": ["mcp"]
    }
  }
}
```

先启动声笺服务，再连接 MCP 客户端。

WorkBuddy 在「设置 → MCP」添加 STDIO 服务器：

```json
{
  "mcpServers": {
    "shengjian": {
      "type": "stdio",
      "command": "C:\\DIY\\声笺\\声笺服务.exe",
      "args": ["mcp"]
    }
  }
}
```

豆包工作在「插件 · 技能 · 伙伴 → 新建自定义连接器」中选择 STDIO：命令填 `C:\DIY\声笺\声笺服务.exe`，参数填 `mcp`。

连接后可以直接对客户端说：“读取声笺最后一条识别记录，把字幕翻译成英文，按原行号调用 `save_translation` 写回。” 客户端会用它自己的模型和额度完成翻译，声笺只负责校验和落库。

## 下一阶段：声笺作为 MCP Client

上面的“外部 Agent 翻译后回写”不需要声笺主动连接外部服务。只有当你希望声笺自己的 Agent 指令直接调用别家 MCP 工具时，才需要给声笺增加 Client：

- 配置文件放在 `data/mcp.json`，每个服务器记录 `command/args/env` 或 HTTP 地址、启用状态、允许的工具列表和超时。
- 外部工具名加服务器前缀，避免和内置工具重名。
- 只暴露允许列表内的工具；写操作默认需要用户确认。
- 免费额度由各工具方决定，MCP 不提供额度；必须处理限流、失败和隐私边界，不能静默把字幕发给第三方。
- 外部工具只用于检索、术语建议、第二意见或备用翻译；逐行翻译主链路仍由本地确定性流水线负责。

Client 的具体服务器清单需要按实际选用的官方工具接入，不能预先猜命令或猜鉴权方式。

## 验收

- 原有 TTS MCP 工具不回归。
- `tools/list` 返回全部翻译工具，`tools/call` 的参数能正确映射到 HTTP 请求。
- `get_subtitle` 默认 compact、按 50 行分页；`format="objects"` 仍保留原文时间码，并把已有译文合并到对应行；`known_revision` 未变化时只回 unchanged。
- `translate_subtitle` 失败时返回服务层的错误，不写半成品；成功默认只回回执。
- `save_translation` 在缺行、重复行或多出行时拒绝写入，旧译文保持不变；缺 `target_lang` 直接报错。
- 发行包内的 MCP 进程可以启动并列出工具。
