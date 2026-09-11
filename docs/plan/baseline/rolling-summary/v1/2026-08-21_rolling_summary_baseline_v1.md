# Rolling Summary Baseline V1 实施方案

## 1. 目标与范围

本轮实现一个 **query-independent Rolling Summary** 基线：历史处理阶段不读取最终问题，将连续对话历史压缩为自然语言摘要和最近的原文尾部；全部历史处理完成后，才把“摘要 + 原文尾部 + 最终问题”交给 Answer Model。

输入数据在清洗阶段已经把同一 session 内连续同角色消息合并成一条；系统维护的是连续的 logical message stream，而不是以 session 为压缩边界的 chunk。session 只用于时间排序、来源 ID、渲染 header 和审计；跨 session 不合并消息，也不会因为 session 切换自动压缩。

本轮不实现 Memory Sidecar、SFT/RL、自动 LLM judge，也不实现新的方法。代码保留 `full_context` 作为对照臂；两种方法都输出官方 evaluator 所需的 `question_id`/`hypothesis` JSONL，并记录可回放的实验轨迹。

## 2. V1 固定配置与运行入口

| 项目 | 当前代码行为 |
|---|---|
| 历史数据 | `data/official_longmemeval/longmemeval_s_cleaned.json` |
| 样本输入 | 冻结 CSV manifest；脚本支持 smoke 12 条和 eval 120 条清单 |
| API 默认地址 | `http://117.186.43.62:5027/v1` |
| 模型默认值 | `qwen3.8-27b` |
| 认证 | 只从 `QWEN38_API_KEY` 读取 |
| 可覆盖环境变量 | `QWEN38_BASE_URL`、`QWEN38_MODEL` |
| decoding | `temperature=0`，`enable_thinking=false` |
| rolling trigger | `80 * 1024` tokens |
| summary generation cap | `8 * 1024` tokens |
| compression prefix | `80 * 1024` tokens |
| raw-tail 固定上限 | 没有单独的 16K 上限；压缩会驱逐达到 prefix budget 的前缀，剩余 tail 不设独立上限 |
| transport retry | 最多 3 次；超时、HTTP 状态和 retry 行为由 `QwenClient` 控制 |

命令行入口为 `scripts/run_rolling_summary.py`。它默认运行 `rolling_summary`，也可通过 `--method full_context` 运行对照臂；支持 `--limit`、`--question-id`、三个 rolling budget 参数和 `--max-concurrency`。`--max-concurrency` 默认是 1；大上下文单卡服务应从 2 开始压测，不能把 12 个样本直接当成一个 API batch。并发 worker 分别持有 HTTP client、SQLite WAL 连接和本地 tokenizer，run config 与 `run_summary.json` 记录并发度、wall time、samples/s 和模型 tokens/s。CLI 显式传入 `LocalQwenTokenizer()`，因此运行脚本中的历史计数和切割计数使用本地 tokenizer；Answer 请求仍由 `QwenClient.tokenize_messages()` 使用服务端 `/tokenize` 进行最终可运行性检查。

直接调用 `process_sample()` 时，如不传 tokenizer，则使用 `TokenCounter(client)`，通过服务端 `/tokenize` 计数；测试可以注入 fake tokenizer/client。

## 3. Message Stream 规范化

1. 清洗 `longmemeval_s_cleaned.json` 时，按 session 内原始顺序把连续同 role 的消息以两个换行合并；合并时保留 `role` 和拼接后的 `content`，`has_answer` 取合并消息中任一条为 true 的结果。当前数据已无连续同角色边界。
2. `chronological_sessions()` 按 `haystack_dates` 从旧到新稳定排序；日期相同则保持源文件顺序。session 内消息顺序保持不变。
3. `build_message_stream()` 直接消费清洗后的消息：一条清洗后消息对应一条 `HistoryMessage`，运行时不再执行同角色合并；跨 session 永不合并。
4. 每个 `HistoryMessage` 在内存中保存：`unit_ordinal`、规范化 session index、`session_id`、session date、role 和 content；可由 content 计算 `content_sha256` 作为内容校验值。

例如，清洗后的某个 session 是：

```json
[
  {"role": "user", "content": "I need help with my garden."},
  {"role": "assistant", "content": "What would you like to know?"}
]
```

其中第二条清洗后消息会被表示为：

```python
HistoryMessage(
    unit_ordinal=16,                 # 0-based；整个历史 stream 中展开后的第 17 条逻辑消息
    session_index=4,                 # 按日期排序后的第 4 个 session
    session_id="session_abc",
    session_date="2023/06/01 (Thu) 10:30",
    role="assistant",
    content="What would you like to know?",
)
```

这里的 `unit_ordinal=16` 是整个跨 session 历史流的 0-based 编号，即展开后的第 17 条逻辑消息。`content_sha256` 是由 `content` 自动计算的属性，不是构造参数。代码中的 index 都从 0 开始。

5. canonical renderer 在 session 首条保留 `## Session <n> — <date>` header，消息渲染为 `User: ...` 或 `Assistant: ...`，各行以换行连接。
6. 规范化过程只读取 role、content、session 元数据；不读取 question、question_date、answer、answer_session_ids 或 has_answer。

合法压缩切点由 `HistoryStream.legal_cuts` 定义：stream 起点，或逻辑 `assistant` message 之后的位置。这样正常被驱逐的前缀总在完整 assistant message 后结束；末尾 user 不会被单独切走。

## 4. Rolling 压缩流程

`RollingSummaryEngine.ingest()` 逐条接收 logical message，并维护 summary、tail、各自 token 数、`history_tokens = summary_tokens + tail_tokens` 和 `terminal_issue`。

每次 ingest 只对新增 message 内容做一次增量计数；如果进入新 session，还计入该 session header。这一步只是 O(1) 维护是否超过 rolling trigger，不是在搜索压缩切点，也不扫描已有 tail。user message 即使使历史暂时超过 trigger，也不触发压缩；只有 assistant message 到达且 `history_tokens > rolling_trigger_tokens` 时才调用 `_compress("rolling_compression")`。

当前 V1 的 `_find_cut()` 在合法 assistant 切点上做**二分查找**：每个候选切点都把完整前缀按 canonical renderer 渲染后计数，找到最早达到 `compress_prefix_tokens` 的切点并驱逐其前缀。前缀 token 数随切点单调增加，因此候选 tokenizer 调用为合法切点数量的对数级；若整段 tail 都未达到 prefix budget，则退回到末尾消息之前最近的合法 assistant 边界。实现不逐条保存 token_count 字段。

压缩步骤为：选出合法切点；将旧 summary、驱逐前缀和 reason 交给 Summary Model；对返回 summary 和保留 tail 重新计数；重建 `RollingState`；记录 `Compression` 元数据并写入 state snapshot。

Summary prompt 只有旧 memory、被驱逐的 history 和压缩预算，不包含最终问题。Answer prompt 只在所有历史 ingest/finalize 完成后构造；`raw tail` 只包含历史消息，不包含最终问题。最终 prompt 顺序为 summary、raw tail、question_date、回答规则、question，其中 question 是用户 prompt 的末尾字段。

## 5. 边界和失败行为

- **末尾 user**：`finalize()` 不压缩，只将 `terminal_issue` 设为 `terminal_unpaired_user`（若此前已有 issue 则保留此前 issue）。
- **没有合法 assistant 切点**：压缩不执行，将 issue 设为 `no_legal_assistant_cut`。
- **超长单条消息**：正常 stream 构建不会因为 raw-tail budget 截断消息；当前 `build_message_stream()` 不执行 tokenizer 边界拆分。
- **Answer 超过服务端上下文**：先调用服务端 `tokenize_messages()`；若 prompt token 数大于等于 `max_model_len`，不发送 completion 请求，将样本标为 `not_runnable` 并记录原因。
- **Summary 请求无 generation headroom**：若 summary prompt 计数超过 `max_model_len - summary_max_tokens`，抛出 `ApiError`，由 runner 将样本标为 `failed`。
- **模型请求失败**：`QwenClient` 对可重试错误最多重试 3 次；最终错误写入 calls，并由 runner 结束该 sample。

当前 V1 没有 final flush 到 16K raw tail、超大 message 硬切或摘要超预算后二次滚动等行为；这些不能作为本版本的验收承诺。

## 6. Prompt 与 token 计数

当前聚焦版 prompt 版本为 `rolling-summary-v1-atomic-facts-question-last`。Summary 首先把可回答事实保留为紧凑原子记录：`[日期 | 来源] 实体 | 属性/关系 | 精确值 | 条件`；条件包含“首单”“前三个月”“截至某日”“当时”等范围。它不得把精确值改为范围或 `+`，不得丢弃数值的时间/比较/排序条件；冲突事实保留带来源和日期的多个版本，不在压缩时自行裁决。对助手提供的命名实体定义、清单、计算、比较和流程，也保留为 `assistant-sourced` 原子事实，即使用户没有明确确认。之后才保留偏好与约束、时间状态变化和多步关系，去除寒暄、重复确认和泛化建议。它只合并旧 memory 与被驱逐历史，不针对最终问题做筛选。

Answer 指令要求只依据 memory 和最近原文历史回答，直接输出答案。数值、比较、排序和算术题必须先依据证据得到精确结论，不能以估计替代精确值或在同一答案中自相矛盾；遇到日期冲突时使用不晚于问题日期的最新事实，只有无法确定时才说明冲突。Full Context 对照臂则把完整 canonical history 与问题一起发送。

`QwenClient` 的服务端 tokenizer 是 API 预算权威：`tokenize_text()` 用于纯文本计数，`tokenize_messages()` 使用相同 chat template 和 generation prompt，completion usage 记录实际 input/output token。但 CLI 的 V1 运行路径注入 `LocalQwenTokenizer` 来做历史预处理计数，因此 states 中的 history/summary/tail token 是本地 tokenizer 结果；最终 Answer 的可运行性判断使用服务端计数，成功请求的 input/output 来自 completion usage。

## 7. SQLite 轨迹存储设计

每次运行写入 `results/rolling_summary/<run-id>/`：`config.json`、`trajectory.sqlite3`、`hypotheses.jsonl` 和 `run_summary.json`。SQLite 仅四张表：`runs`、`samples`、`calls`、`states`。

### 7.1 `runs`

主键为 `run_id`。保存 created_at、method、模型名、config_fingerprint 和完整 config_json。同一 run_id 只能续跑 fingerprint 相同的配置；配置变化必须使用新 run_id。

### 7.2 `samples`

一题一次 attempt 一行，主键为自增 id，外键关联 runs，唯一键为 `(run_id, question_id, attempt)`。保存题目 ID、源数据集 index、question type、fingerprint、状态、开始/结束时间、完整历史 token、压缩次数、`summary_tokens`/`raw_tail_tokens`、Answer input/output token、总调用 input/output token、调用数、延迟、hypothesis 和 error。生成的最终答案写入 `samples.hypothesis`；`samples` 只保存摘要 token 统计，不保存摘要正文。

同一题失败后再次运行会创建新的 attempt，不覆盖旧行；断点续跑只跳过同一 fingerprint 下已 completed 或 not_runnable 的题目。

### 7.3 `calls`

每次 Summary 或 Answer 请求一行，外键关联 samples，唯一键为 `(sample_id, call_ordinal, attempt)`。保存调用序号、底层重试 attempt、kind、status、时间、模型返回文本、实际 input/output token、延迟和 error。最终 Answer 也保存在 `calls.response_text`（`kind='answer'`），与 `samples.hypothesis` 内容相同。

当前 `record_call()` 会接收 request_params、prompt token 估计值、父单元和 source unit ordinals，但为了保持四表最小 schema 会丢弃这些参数；它们不在 SQLite 中持久化。

### 7.4 `states`

每次 ingest、rolling compression 和 final 都追加一行，外键关联 samples，唯一键为 `(sample_id, step_ordinal)`。摘要正文存储在 **`states.summary_text`**；每条 ingest 的单条 canonical 消息存储在 **`states.raw_text`**，另保存 event、时间、summary/tail/history token 和 JSON detail。第一次压缩前，`summary_text` 为 `NULL`；压缩产生摘要后，压缩行及其后的每个 ingest 行都重复写入当前摘要，便于逐行读取状态。`raw_text` 只在 ingest 行写入一条消息（session 首条消息同时带 canonical session header）；压缩和 final 行的 `raw_text` 为 `NULL`。数据库不再保存完整 `raw_tail_text`。

例如查看某题的摘要快照：

```sql
SELECT step_ordinal, event, summary_text, raw_text, summary_tokens
FROM states
WHERE sample_id = ?
ORDER BY step_ordinal;
```

例如一次历史流在第 70 条消息后第一次压缩、第 120 条消息后第二次压缩，日志可以是：

| step | event | summary_text | raw_text |
|---:|---|---|---|
| 1 | ingest | `NULL` | 第 1 条 canonical 消息 |
| 2 | ingest | `NULL` | 第 2 条 canonical 消息 |
| ... | ... | ... | ... |
| 70 | rolling_compression | 第一次摘要（1-70） | `NULL` |
| 71 | ingest | 第一次摘要（1-70） | 第 71 条消息 |
| 72 | ingest | 第一次摘要（1-70） | 第 72 条消息 |
| ... | ... | ... | ... |
| 120 | rolling_compression | 第二次摘要（第一次摘要 + 71-119） | `NULL` |
| 121 | ingest | 第二次摘要 | 第 121 条消息 |
| 122 | ingest | 第二次摘要 | 第 122 条消息 |
| ... | ... | ... | ... |
| 130 | final | 第二次摘要 | `NULL` |

回放时，按 `step_ordinal` 顺序读取 `raw_text` 即可重建原始 canonical message stream；在每个 `rolling_compression` 行切换到该行的 `summary_text`，并从后续 ingest 行重新收集 raw message，得到当时的 raw tail。`summary_text` 的重复写入会增加线性存储，但不会再复制不断增长的 tail，空间量级从 tail 的累积平方降为消息文本和摘要文本的线性累积。

压缩快照的 detail 包含 Compression 字段：reason、evicted ordinals、evicted token、旧 summary hash/token、new summary token、cut index 和二分候选计数。

### 7.5 Provenance 的实际边界

V1 不在 SQLite 中复制官方 JSON，也没有 messages 或 units 表。运行时 `HistoryMessage` 不保存独立的 source-message index；其 `unit_ordinal` 只标识清洗后消息直接展开形成的 logical stream 位置。清洗前的消息边界不属于本版本的运行时语义。`states.raw_text` 保存每条 ingest 的 canonical 单消息，`states.detail` 保存被驱逐的 logical `unit_ordinal` 列表及压缩元数据；完整 raw tail 由最近一次压缩之后的 raw_text 行重建。

因此，V1 的可回溯方式是：从 samples.dataset_index 找到清洗后的源题目；按同一 chronological_sessions() 和 build_message_stream() 规则重建 logical stream；再用 state detail 中的 evicted_ordinals、快照顺序和 raw tail 文本对照回放。回溯目标是清洗后消息的压缩边界和状态演进；清洗前消息排列不属于本版本的审计范围。calls 也不保存请求 prompt 或 source ordinals。

## 8. 验证与验收

当前测试为 57 项，重点包括：session 日期排序稳定、清洗后消息直接展开且不在运行时二次合并、user 超 trigger 时不压缩、assistant 边界切割、二分候选计数、session header、末尾 user、无合法切点、超长 Answer、Summary/Answer 请求无 gold、最终 Answer prompt 以 question 结束、并发度参数校验、四表 SQLite 外键/WAL、attempt 重试、state 快照、hypothesis 导出、统计，以及 Qwen client 的 tokenize 路径和重试。

运行前应先用 smoke manifest 验证 API preflight、hypothesis 导出和 trajectory 写入，再使用冻结的 120 条 eval manifest。评估结果必须携带 run_id、config fingerprint、manifest hash 和代码版本；不同 prompt、模型或 budget 不得混入同一 run。

V1 的 120 条实测结果和错误样例分析记录在 [V1 评估记录](2026-08-21_rolling_summary_baseline_v1_evaluation.md)。
