# MCP 翻译 Token 优化方案

基线：0.9.8 · 2026-11-25
P0 实施：0.10.1 · 2026-11-25
目标：外部 Agent 翻译 430 行字幕时，单文件工具载荷从约 28k tokens 降到 9k 以内；重复读取同一会话 ≤200B；不改变原文、时间码和现有译文语义。

## 1. 实测消耗（430 行真实会话）

| 环节 | 基线 0.9.8 | P0 实测 0.10.1 | 主要手段 |
|---|---|---|---|
| tools/list（18 个工具） | 9.6KB ≈ 2.4k | 8.9KB ≈ 2.2k | 精简描述，结果单行 JSON |
| get_subtitle | 62.9KB ≈ 15.7k | 15.7KB ≈ 3.9k | compact 数组、50 行分页、默认不带时间码 |
| save_translation 回执 | 39.4KB ≈ 9.8k | 约 0.2KB ≈ 0.05k | 默认只回写回执，不回显全文 |
| 重复读取同一会话 | 62.9KB/次 | 142B | revision 未变直接返回 unchanged |

430 行读取按 50 + 200 + 180 三块实测合计 15.7KB（1.7 + 6.5 + 7.3KB）；写入回执按同长度 430 行结果实测 175B。P0 全流程（tools/list + 读 + 写回执）约 24.9KB ≈ 6.2k tokens，比基线 111.9KB ≈ 28k tokens 减少约 78%。tokens 按 UTF-8 4 字节≈1 token 粗估。

## 2. 四条原则

1. **字幕只读一次**：读接口返回 `revision`；客户端带 `known_revision`，未变化时返回 `{"unchanged":true,"revision":"…"}`。
2. **按块流转**：`cursor + limit` 读，`cursor + translations[]` 写；单次 ≤50 行（约 1.7KB）。
3. **不回传原文、不回显全文**：写接口只收译文；返回只给 `written/translated_lines/next_cursor`，finish 只给 `run_id/issues/memory_written`。
4. **默认精简**：工具结果用 `separators=(",",":")`，不再 `indent=2`；错误只回 message+hint，不列全部工具名。

## 3. 读：一次拿一块，不拿整本

`get_subtitle` 新增参数（0.10.1 已实施）：

- `cursor`：起始行位置，默认 0
- `limit`：默认 50，上限 200
- `include_timecodes`：默认 false
- `format`：`"compact"`（默认）/ `"objects"`（兼容旧客户端）
- `include_translation`：compact 默认 false；objects 默认 true，保持旧形状
- `known_revision`：命中直接返回 unchanged

compact 返回示例：

```json
{"session_id":"…","revision":"be618723c47b","line_count":430,"translated_lines":0,
 "cursor":0,"next_cursor":50,"done":false,"has_translation":false,
 "columns":["i","text"],"lines":[[1,"There are"],[2,"…"]]}
```

50 行实测 1.7KB，200 行实测 6.5KB，unchanged 回执 142B。`known_revision` 命中时只回 `session_id/revision/unchanged/line_count/translated_lines/target`。

`list_translation_sessions`（0.10.1）：默认 `limit=20`，只回 `id/name/status/batch_id/batch_index/line_count/translated_lines/target`；保留 `batch_id` / `status` 筛选和 `count/total/has_more`。`get_batch_manifest` 仍留 P1。

## 4. 写：P0 先压回执，P1 再分块

0.10.1 已实施：`save_translation` 仍收完整 `lines`，但响应默认只回

```json
{"session_id":"…","target":"en","line_count":430,"translated_lines":430,
 "revision":"be618723c47b","run_id":"…","issues":0,"memory_written":120}
```

需要回显全文的调试场景显式传 `include_translation=true`，才会返回完整 `translation` 和 `run`——这正是旧版 39.4KB 的来源。`translate_subtitle` 同样改为只回回执，全文用 `get_subtitle` 再读。

P1 才引入分块草稿：

```text
save_translation_chunk(session_id, revision, cursor, translations[], target_lang, provider, model)
→ {written, translated_lines, next_cursor}

finish_translation(session_id, revision, target_lang, provider, model)
→ {status, run_id, issues[], memory_written}
```

- `translations` 是不带 index 的字符串数组，顺序对应 cursor 起的行；行号由服务端映射。
- 草稿存 `translation.sqlite3` 的 `drafts(session_id, target, revision, cursor, payload)`；finish 时才写识别记录的 `translation` 并写记忆，失败不覆盖旧译文。
- 同一 `(revision, cursor, translations hash)` 重复提交为幂等 no-op；revision 不匹配返回 `STALE_REVISION`，要求重读。
- `save_translation` 保留：P1 后内部等价于“全量 chunk + finish”，供 ≤100 行或旧客户端使用。

## 5. 语言代码与重试

0.10.1 已实施：

- `normalize_language` 统一 `zh-CN/zh_CN/zh-Hans/zh-TW → zh`、`en-US → en`、`ja-JP → ja` 等；设置、Agent、外部回写、术语和记忆的 target 过滤都走同一函数。
- 三处 MCP 描述里的 `zh-CN` 示例改为 `zh`；服务端仍接受 `zh-CN`，落库统一为 `zh`。
- `save_translation` 的 `target_lang` 改为必填，缺省返回 `target_lang 不能为空，例如 zh、en。`，不再落到设置里的 `en`。
- `list_translation_terms` / `add_translation_term` 的 target 过滤同样归一，旧术语不会因客户端写 `zh-CN` 而查不到。

## 6. 工具定义与描述

- 18 个工具保留，不合并；描述只留“何时用 + 关键约束”。0.10.1 实测 9.6KB → 8.9KB（含两处回执开关说明）。方案里 7KB 的目标没有达到：剩余体积主要是 JSON Schema 本身，继续删描述收益已很小，留 P2 再评估。
- 批量流程（list → get → translate → save）暂不新增 MCP prompt；桌面端「复制 MCP 指令」仍是固定短指令。
- 结果统一 `separators=(",",":")`；错误只回 message + hint，不再列全部工具名。

## 7. 声笺自己的翻译 Agent（顺带）

- `ApiTranslator` 请求改用 `[[i,text]]` 紧凑数组；术语只传当前块命中的，不再传最多 80 条全局表。
- `agent.chat` 不再把 100 行字幕塞进 system；按需用 `get_subtitle` 取 20 行。
- 先查记忆再要求模型：全行命中记忆时不要求 base_url / model（当前 `prepare` 会先报“未配置模型”）。
- chunk 20 行保持不变，避免模型行数对齐退化。

## 8. 分阶段

P0（0.10.1，已完成）

- 结果单行 JSON；`get_subtitle` 增加 cursor / limit / timecodes / translation 开关与 compact 格式
- revision / unchanged 已落地；`save_translation` / `translate_subtitle` 默认只回回执
- 语言归一与描述修正；工具描述精简
- 实测全流程载荷 -78%（28k → 6.2k tokens）

P1（下一版，暂记 0.10.2）

- `save_translation_chunk` + `finish_translation` + drafts，写也按块
- `get_batch_manifest`；复制指令更新
- `get_subtitle` 返回本块命中的 terms / memory（≤20 条）
- 预期单次 ≤5KB，不再整本往返

P2

- 内部 Agent 请求紧凑化与记忆优先
- 新增 `tests/test_mcp_token_budget.py`，对 50 / 120 / 430 行样本断言字节上限
- 继续评估 `tools/list` 是否能压到 7KB；本轮已更新 README 与接入文档

## 9. 验收

- 430 行 compact 读取 ≤16KB（实测 15.7KB）；单次 50 行 ≤2KB（1.7KB）；unchanged ≤200B（142B）。
- 430 行写回总载荷 ≤18KB（P0 回执 175B；P1 分块后再压）；单块 ≤2KB；重复提交不重复写记忆。
- `zh-CN` 可直接用于 save，落库为 `zh`，术语按 `zh` 能命中（测试覆盖）。
- 旧 `save_translation` 请求体与 dsh-agent 120 行回写、桌面译文显示与导出全部回归通过；完整测试套件通过（1030 passed / 1 skipped）。

## 10. 不做与风险

- 不自动翻译批次，不实现 MCP Client，不改原文、时间码和导出。
- compact 作为默认会改变返回形状；保留 `format="objects"` 兼容旧客户端，文档和复制指令统一用 compact。
- 分块期间 UI 只显示“翻译中 x/y”，finish 前不覆盖旧译文；重识别导致 revision 变化时旧草稿自动作废。
- 草稿按 session 保留最近 7 天，finish 或超时清理。
