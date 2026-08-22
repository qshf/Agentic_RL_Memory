# Memory Sidecar 方案一：快速可行性验证计划

## 1. 目的

本计划建立在已完成的 Rolling Summary V1 基线上，验证第一个 Memory Sidecar 方案：

```text
小模型：逐个 turn/chunk 抽取和维护结构化记忆增量
大模型：在最终问题阶段读取结构化记忆并负责复杂推理
```

本阶段只验证架构和数据协议，不做 SFT、RL 或复杂 Agent 编排。核心问题是：

> 在相同最终 Answer Model 下，结构化增量记忆能否比自然语言 Rolling Summary 更少丢失关键事实，同时降低最终答案上下文成本。

V1 Rolling Summary 已作为冻结基线，结果和错误归因记录在：

- `docs/plan/2026-08-21_rolling_summary_baseline_v1_evaluation.md`
- run `rolling-summary-eval120-v1-atomic-c2`

## 2. V1 基线给出的实验动机

V1 的 120 条样本结果为 73/120，DeepSeek judge 准确率 60.83%。人工复核显示：

- 18 条是关键事实没有进入最终 `summary + raw tail`；
- 26 条上下文已经存在，但 Answer Model 在计数、排序、日期选择或实体消歧上出错；
- 将上下文充分的 26 条交给 DeepSeek 重新回答后，14 条得到纠正，说明部分问题属于答案模型而非摘要存储；
- 对剩余 12 条要求模型输出理由后，只有 2 条纠正，说明“增加解释”本身不能解决旧值选择、局部计数和事件定位问题。

因此，Sidecar 第一阶段只应重点验证：

1. 结构化记忆是否减少 summary omission；
2. 结构化 provenance 和状态更新是否减少旧值误用；
3. 在相同 Answer Model 下，最终输入是否更短且证据更完整。

不能把所有答案错误都预期为 Sidecar 可以解决的问题。

## 3. 方案一的职责边界

### 3.1 小模型职责

小模型只处理当前 turn/chunk 和当前 memory state，输出结构化 memory event：

- `ADD`：新增事实、偏好、事件、计划或可复用 assistant fact；
- `UPDATE`：已有事实发生时间或状态变化；
- `SUPERSEDE`：明确旧值失效，由新值替代；
- `NOOP`：没有值得长期保留的信息。

小模型不负责：

- 回答最终问题；
- 读取最终问题或 gold answer；
- 对整个历史重新写自然语言摘要；
- 自主调用搜索、数据库或外部业务工具；
- 根据不确定内容编造事实。

### 3.2 大模型职责

最终 Answer Model 读取：

```text
current_memory_state
+ 必要的 recent_delta/events
+ final question
```

它负责跨事实推理、求和、排序、日期解析、偏好迁移和最终回答。若 memory 中存在冲突，Answer Model 必须根据事件日期和状态字段选择适用值。

后续可以增加大模型 compactor，但第一轮不让它每次重新读取全部历史。若需要压缩，只读取当前状态和上次 checkpoint 之后的增量。

## 4. 小模型是否需要工具调用

### 4.1 方案一：不加入原生 tool calling

第一轮不加入原生工具调用。小模型的动作空间固定为 JSON，而不是让模型自由选择工具：

```json
{
  "action": "ADD|UPDATE|SUPERSEDE|NOOP",
  "memory_type": "fact|preference|event|plan|assistant_fact",
  "key": "finance.wells_fargo.preapproval",
  "value": "$400,000",
  "status": "active|superseded|planned|completed",
  "event_date": "2023-08-20",
  "source": {
    "session_id": "session_xxx",
    "message_index": 17
  },
  "confidence": 0.96
}
```

原因：

1. 第一阶段要验证的是 memory policy 和 schema，不是工具路由能力；
2. JSON action 更容易做 schema validation、重放、比较和离线打分；
3. 原生 tool calling 会同时引入工具选择、参数生成、失败重试等变量，难以判断准确率变化来自哪里；
4. 写文件和更新状态由 runner 执行，不应交给小模型自行决定路径或 SQL。

### 4.2 什么时候再加入工具调用

当以下需求出现时，再考虑工具调用：

- memory state 超过预算，需要小模型选择 `search_memory` 或 `get_entity_history`；
- 需要按 key 查询历史版本，而不是把整个 state 放入 prompt；
- Sidecar 迁移到在线 Agent，需要 `append_event`、`read_recent_events`、`compact_memory` 等动作；
- 需要让模型根据任务类型动态选择不同记忆模块。

届时工具应由 runner 白名单提供，工具参数仍经过 JSON schema 校验；小模型不能直接执行任意文件系统或数据库操作。

## 5. 存储设计

第一版使用“追加事件 + 当前状态”两层结构，文件是可读接口，SQLite 是可回放存储。

```text
memory_events.jsonl   # append-only，每次 ADD/UPDATE/SUPERSEDE/NOOP 一行
memory_state.json     # 根据事件日志物化的当前有效记忆
trajectory.sqlite3    # 实验级 provenance、模型调用、状态快照和成本
```

事件必须保留：

- `sample_id`、`session_id`、`turn_index`、`source_message_indices`；
- `memory_type`、`key`、`value`、`status`；
- `event_date`、`observed_at`、`supersedes_event_id`；
- 小模型原始 JSON、校验结果和错误信息；
- `content_sha256` 或规范化事件 hash，便于去重和回放。

当前状态只保留 active/planned 等可供回答的记录；被 supersede 的事件不能删除，必须能回放出旧值和更新顺序。

## 6. turn/chunk 处理规则

LongMemEval 的单条消息不一定等于一个自然 turn。第一版采用：

1. 清洗阶段保留同 session 内连续同 role 合并结果；
2. 优先按 `user -> assistant` 组成一个 turn；
3. 没有完整 user/assistant 对时，按最多 4--8 条逻辑消息组成 chunk；
4. 每次小模型只收到当前 chunk 和当前 `memory_state`，不收到最终问题；
5. 普通寒暄、重复确认和无事实内容应输出 `NOOP`；
6. 数字、日期、金额、状态变化、偏好、计划和 assistant 提供的可复用事实必须保留来源。

第一轮可以同步调用以保证可回放；异步队列和批量处理属于后续吞吐优化，不作为架构有效性的前置条件。

## 7. 快速验证实验

### 7.1 样本

先运行 24 条固定样本，不直接跑完整 500 条：

- 12 条已知“上下文充分但二次答案仍错误”的样本；
- 6 条明确的 summary omission 样本；
- 6 条 V1 原本回答正确的样本。

样本按 `question_type` 和 haystack 长度分层，保存固定 manifest，保证 Rolling、Sidecar-Strong、Sidecar-Small 使用完全相同的历史顺序和问题。

### 7.2 对照组

| 组别 | Memory manager | 最终 Answer Model | 目的 |
| --- | --- | --- | --- |
| Rolling V1 | 现有 Rolling Summary | 固定同一个强模型 | 已有基线 |
| Sidecar-Strong | 强模型输出结构化 memory event | 同一个强模型 | 验证 schema/架构上限 |
| Sidecar-Small | 5090 上的小模型输出结构化 memory event | 同一个强模型 | 验证成本和小模型可行性 |
| Oracle/Full 对照 | gold evidence 或完整可行上下文 | 同一个强模型 | 估计证据上限和答案上限 |

第一轮应先运行 Rolling V1 与 Sidecar-Strong。只有 Sidecar-Strong 在关键指标上有收益，才运行 Sidecar-Small；否则先修改 schema 和更新策略，不进入小模型训练。

### 7.3 固定变量

- 最终 Answer Model、prompt、temperature 和最大输出长度固定；
- 不把 gold answer 或 answer session ids 传给小模型；
- 所有方法使用同一清洗后的 message stream；
- 记录完整 token、调用次数、延迟、失败重试和成本；
- Answer 阶段只改变 memory representation，不同时改变问题和回答协议。

## 8. 评价指标和 Go/No-Go

### 8.1 必测指标

1. Final QA accuracy：最终答案是否正确；
2. Evidence sufficiency：memory 是否包含 gold 所需的关键事实；
3. Update correctness：旧值、新值和适用日期是否正确；
4. Final answer input tokens：最终强模型实际读取的 token；
5. Total API tokens/cost：包括小模型、compactor 和 Answer Model；
6. Memory size：当前 state 和增量日志大小；
7. Error type：漏记、误记、过期值、重复值、答案推理错误；
8. Latency/call count：每 sample 的 sidecar 调用数和 wall time。

### 8.2 建议门槛

Sidecar-Strong 至少满足以下两项，才认为架构值得继续：

- 在 24 条 pilot 上相对 Rolling V1 有明确准确率提升，尤其是 omission 子集；
- Evidence sufficiency 明显高于 Rolling V1；
- 最终 Answer 输入 token 减少至少 30%；
- 总成本或总 token 减少至少 30%，且准确率不下降；
- knowledge-update 样本不再系统性选择摘要旧值。

Sidecar-Small 的判定分两层：

- 若 Strong 有效、Small 无效：问题在小模型抽取能力，进入蒸馏/换模型实验；
- 若 Strong 和 Small 都无效：问题在 schema、更新规则或实验假设，不进入 RL。

24 条只用于快速方向判断，不能作为最终 benchmark 结论。通过后扩展到 120 条，再决定是否跑 LongMemEval-S 全量。

## 9. 实施顺序

### P0：协议和回放

- 固定 24 条 manifest；
- 定义 JSON schema 和 event validator；
- 从 V1 trajectory 生成 Sidecar 输入；
- 实现 JSONL event log、state materializer 和 SQLite 记录；
- 用固定样例验证 ADD/UPDATE/SUPERSEDE/NOOP。

### P1：Strong manager 上限

- 使用强模型作为 memory manager；
- 不使用原生工具调用；
- 运行 Rolling V1 与 Sidecar-Strong；
- 比较 evidence sufficiency、最终答案和成本。

### P2：Small manager

- 在 5090 上部署 0.5B 或 1.5B 模型；
- 保持完全相同的 schema、validator 和 Answer Model；
- 先串行验证正确性，再测试并发和批量吞吐；
- 对失败事件保留原始输入和小模型输出，便于 SFT 数据构造。

### P3：扩展与否决

- 通过 pilot 门槛后扩展到 120 条；
- 仍然有效才考虑蒸馏和 RL；
- 若只改善 memory recall、不改善最终 QA，转向 Answer prompt、证据选择和 verifier，而不是继续扩大 Sidecar。

## 10. 预期结论格式

每个实验 run 必须输出：

- `run_config.json`：模型、prompt/schema 版本、并发、预算和 manifest hash；
- `run_summary.json`：准确率、token、成本、延迟和失败数；
- `memory_events.jsonl`：小模型逐次动作；
- `memory_state.json`：最终物化状态；
- `hypotheses.jsonl`：最终答案；
- `error_analysis.md`：逐题判断是 memory failure 还是 answer failure。

最终报告必须回答：

1. Sidecar 是否比 Rolling Summary 更完整地保留证据；
2. 小模型是否能以可接受成本维护这些证据；
3. 结构化记忆是否降低最终上下文长度；
4. 提升来自 memory recall，还是来自最终 Answer Model 的偶然波动；
5. 是否值得进入 SFT/RL。

