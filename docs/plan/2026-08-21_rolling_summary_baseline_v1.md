# Rolling Summary Baseline V2 实施方案

## 1. 目标与范围

本轮实现一个 **query-independent Rolling Summary** 基线：历史处理阶段不读取最终问题，将持续对话上下文压缩为自然语言摘要和最近原文尾部；全部历史处理完成后，才将“摘要 + 原文尾部 + 最终问题”交给 Answer Model。

这次修订的核心是：**系统维护的是连续 message context，不是 session chunk。** session 只提供时间、来源 ID 和审计信息；它不是摘要的切分边界。

本轮不实现 Memory Sidecar、SFT/RL、Full Context 或自动 LLM judge。仅产出可交给官方 evaluator 的 hypothesis 与可回放实验轨迹。

## 2. 固定实验协议

| 项目 | 固定值 |
|---|---|
| 历史数据 | `data/official_longmemeval/longmemeval_s_cleaned.json` |
| 首轮样本 | 未与冻结 120 条重叠的 12 条 smoke manifest，seed `20260822` |
| API | `http://117.186.43.62:5027/v1` |
| 模型 | `qwen3.8-27b`，`max_model_len=262144` |
| 历史触发阈值 | 100K tokens |
| 摘要预算 | 8K tokens |
| 原文尾部预算 | 16K tokens |
| 最终历史预算 | 24K tokens（8K 摘要 + 16K 尾部） |
| decoding | `temperature=0`，`enable_thinking=false` |

密钥只从 `QWEN38_API_KEY` 读取；`QWEN38_BASE_URL`、`QWEN38_MODEL` 可覆盖默认值。所有 token 判断必须使用本服务的 `/tokenize`，不使用字符数或第三方 tokenizer。

## 3. Message Stream 规范化

1. 按 `haystack_dates` 对 session 稳定排序；session 内原始消息顺序不变。
2. 在**同一个 session 内**，连续同角色消息合并为一个逻辑 message，原文以空行连接。例如 `assistant, assistant, user` 变为 `assistant, user`。
3. 每个逻辑 message 保存 `first_source_message_index`、`last_source_message_index` 和所有原始消息 provenance。SQLite 仍可回溯每一条合并前消息；任何合并不得读取 `has_answer`、`answer` 或最终问题。
4. 将各 session 的逻辑 message 按时间拼为一个连续 stream；在 session 切换处渲染 session ID/date 标记，但不强制 flush 或压缩。

这里的“assistant 边界”是指规范化后的逻辑 `assistant` message 结束位置。连续 assistant 消息先合并，避免把同一段助手输出人为拆开。

## 4. 精确计数与二分切点

### 4.1 禁止逐项加总

不再保存或使用 `sum(unit.token_count)` 作为任何预算结论。每次判断都先将候选 `summary + raw_tail` 按最终的 canonical renderer 渲染成完整文本，再调用服务端 `/tokenize` 计数。Summary/Answer 请求本身则对完整 chat messages 调 `/tokenize`，包括 system prompt、模板和 generation prompt。

### 4.2 合法切点

设规范化后的连续消息为 `m[0:n]`。合法切点集合为：

```text
{0} ∪ {i | m[i - 1].role == "assistant"}
```

切点 `i` 表示早期前缀 `m[0:i]` 被纳入摘要、原文尾部为 `m[i:n]`。因此所有被摘要的正常历史均以 assistant 回复结束；不会只把 user 提问送入摘要。

### 4.3 二分流程

仅在处理完逻辑 assistant message 后检查 100K trigger；user message 后即使暂时超限也不得摘要。

当触发中间压缩或 final flush 时：

1. 在合法切点数组上二分，而不是从旧 chunk 开始逐个累计或逐个尝试。
2. 对每个中点切点渲染连续后缀 `m[mid:n]`，调用 `/tokenize` 测量其真实原文 token 数。
3. 找到**最早**满足 `raw_tail <= 16K` 的合法切点，以保留尽可能长的最近连续原文上下文。
4. 用旧摘要与前缀 `m[0:cut]` 生成不超过 8K 的新摘要；尾部更新为 `m[cut:n]`。
5. 重新渲染新摘要加尾部并精确计数；若超过 24K，记录摘要超预算并将该样本标为失败，不静默接受超额状态。

对一个 stream 有 `B` 个合法 assistant 切点时，尾部定位最多 `ceil(log2(B))` 次候选 tokenization，加上最终验证；不得对每条 message 做一次 token 累加或线性扫描。

## 5. 异常与边界行为

- **末尾 user message**：LongMemEval-S 有少数 session 以 user 结束。它保留为未完成的 `raw_tail`，记录 `terminal_unpaired_user`；在没有后续 assistant 前不得单独摘要。
- **尾部无法满足 16K**：若最后一个未配对 user message 自身超过 16K，保留完整 user message、记录 `tail_budget_exceeded_unpaired_user`，不破坏 message。若完整 Answer 请求超过服务端最大上下文，则将该题标记 `not_runnable`。
- **超大完整轮次**：若合法 assistant 边界之间的一段上下文超过 16K，允许选择该 assistant 后的切点，使该完整轮次进入摘要而不是被截断。只有单条 message 超过服务端最大上下文时，才允许 tokenizer 边界硬切，并标记 `degraded_single_message_split`。
- **summary 超 8K**：请求使用 `max_tokens=8192`；实际返回仍超预算时，记录失败而不是继续滚动，避免失去预算可解释性。

## 6. Prompt、日志与恢复

Summary 请求只接收旧摘要和已选定的早期 message 前缀；禁止包含 `question`、`question_date`、`answer`、`answer_session_ids`、`has_answer`。Prompt 要求保留用户事实、偏好、时间、数值、状态更新，以及可被后续引用的 assistant 信息。

Answer 请求只在全部历史处理完成后接收：新摘要、连续原文尾部、`question_date`、最终 `question`。最终 Answer 输入须用 `/tokenize` 实测并记录。

所有产物写入 `results/rolling_summary/<new-run-id>/`：

- `config.json`：模型、预算、`rolling-summary-v2` prompt/history-policy 版本、manifest 哈希；
- `trajectory.sqlite3`：仅保留四张 demo 表：`runs`（运行配置）、`samples`（每题结果）、`calls`（Summary/Answer 调用）和 `states`（摘要/原文尾部快照）。不单独复制 `units` 或 `messages` 表；完整原始历史仍从官方 JSON 读取。
- `hypotheses.jsonl`：`question_id`、`hypothesis`；
- `run_summary.json`：成功/失败数、调用、输入输出 token、压缩次数、二分计数与时延。

V2 的 history policy、renderer 与 prompt version 纳入 fingerprint。V1 运行产物不得续跑或混合；必须使用新的 `run_id`。

## 7. 验证与验收

本地测试必须覆盖：

1. 连续 user 或 assistant 消息合并后，内容顺序与原始消息索引范围均可回溯；
2. user 消息导致超限时不调用摘要，只有对应 assistant 到达后才允许摘要；
3. 每个正常摘要前缀都以逻辑 assistant message 结束，且尾部是同一 stream 的连续后缀；
4. 二分选出的切点是满足 16K 的最早合法切点；测试 spy 证明候选 tokenization 为对数级，而非逐 message 累加；
5. session 首条 assistant、连续同角色、末尾 user、跨 session 连续 stream 与超大 message 的降级行为正确；
6. Summary 和 Answer 请求均不含 gold 标注，最终 renderer token 数和服务端 `/tokenize` 一致；
7. 12 条真实 smoke 全部生成合法 hypothesis 与审计日志，再冻结实现并运行 120 条评测 manifest。
