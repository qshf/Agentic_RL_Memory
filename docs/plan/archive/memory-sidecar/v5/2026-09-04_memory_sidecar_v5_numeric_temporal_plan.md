# Memory Sidecar V5 数字与时间专用记忆方案

## 1. 决策

V4 的 `graph-all` 不再作为通用 Memory Manager。现有实验显示，V4 的图谱存储、规范化和
去重可以运行，但完整图谱直接交给 Answer 会引入大量无关关系，导致金额聚合和时间推理
错误。

V5 不继续扩大 V4 graph-all，而是验证一个范围更小的专用模块：

```text
普通事实、偏好、计划、描述 -> V3 key/value 路由
数字、金额、日期、时间、数量 -> V5 typed numeric-temporal memory
最终 Answer -> V3 memory + V5 typed records + raw tail
```

V5 的目标不是构建通用知识图谱，而是验证：**对需要数值、时间和确定性聚合的问题，程序
辅助的 typed records 是否比 V4 graph-all 更稳定。**

V3 baseline 保持不变。V4 代码、数据库和结果保留为负向实验和审计材料，不删除、不覆盖。

## 2. 问题边界

### 2.1 处理范围

V5 只处理原文中有直接证据的以下信息：

- 金额和货币：`$120`、`120 USD`、`€40`；
- 数量和计数：`12 items`、`three courses`；
- 持续时间：`5 days`、`2 hours`；
- 日期：`May 20, 2023`、`2023-05-20`；
- 时间：`7:15 AM`、`18:30`；
- 相对时间：`last Saturday`、`the week before June 9`；
- 区间、百分比和频率：`7-10 days`、`20%`、`twice a week`。

### 2.2 不处理的内容

- 不创建通用实体关系图谱；
- 不让模型自行计算总数、总金额或日期差；
- 不从两个无明确关联的数字推导新事实；
- 不使用 embedding 或模糊相似度做去重；
- 不使用问题答案或 evidence gold 决定历史路由；
- 不修改现有 V1 Answer prompt。

## 3. 数据与实验范围

### 3.1 候选池

对已有数据做离线初筛，仅用于生成候选清单，不参与运行时路由：

| 数据集 | 数字/金额/数量候选 | 日期/时间候选 | 去重后候选 |
|---|---:|---:|---:|
| LongMemEval-S 120 | 约 73 | 约 43 | 约 95 |
| LOCOMO 类别 1--4 | 约 314 | 约 390 | 约 638 |

候选筛选必须依据问题意图和 evidence 内容。仅因为答案文本包含数字，不得将题目归为
数字题。一个题目可以同时有 `numeric` 和 `temporal` 标签，但评测时指定一个 primary type，
避免重复计数。

### 3.2 第一轮 pilot

第一轮使用 48 条目标问题：

| 数据集 | 数字/金额/数量 | 日期/时间 | 合计 |
|---|---:|---:|---:|
| LongMemEval-S 120 | 12 | 12 | 24 |
| LOCOMO 类别 1--4 | 12 | 12 | 24 |
| 合计 | 24 | 24 | 48 |

样本应覆盖单 session、多 session、金额比较、金额求和、数量、持续时间、绝对日期、相对
日期和先后顺序。LOCOMO 类别 5 暂不纳入，因为它需要独立的 adversarial 拒答 rubric。

为检查专用路由是否损害普通内容，可额外增加 12 条非数字控制题（每个数据集 6 条）。预算
有限时先运行 48 条目标题，控制题放在第二轮。

### 3.3 LOCOMO 的运行单位

LOCOMO 按 conversation 构建一次 memory，再回答该 conversation 的多条问题。pilot 不按
每条 QA 重建 Manager memory。建议选择 6 个 conversation，每个 conversation 取 4 条目标
题，覆盖数字和时间两类；同一 conversation 的 V3 和 V5 必须使用相同历史和相同状态起点。

## 4. 运行时路由

### 4.1 路由粒度

路由单位是完整的 user/assistant turn。一个 turn 中任意 user evidence 命中强结构化信号，
整个 turn 进入 V5 typed memory；否则进入 V3。

不得只把包含数字的半句话送入 V5，也不得因为一个 chunk 含数字就把整个历史全部送入 V5。
路由不读取当前问题、reference answer 或 gold evidence，避免数据泄露。

### 4.2 信号分级

强信号包括：

```text
货币符号或货币代码
带单位的数字或数量
明确的时间格式
明确日期格式
持续时间单位
百分比或数值范围
```

弱信号包括孤立年份、序号、代码名和普通整数。弱信号单独出现时不触发 V5，除非同一 turn
同时出现日期、时间、单位或明确的数量语义。每次路由保存命中的模式和规则版本。

### 4.3 两个 memory state

V5 保留两个相互独立的状态：

```text
v3_memory_state：普通 key/value 记录
v5_typed_state：数字、金额、数量、日期、时间记录
```

两者不在 Manager 阶段互相修改。最终 Answer projection 阶段才将两种记录合并成上下文，
并保留每条记录的来源 ID。

## 5. V5 Manager 协议

### 5.1 模型最小输出

模型只负责从当前 turn 中抽取原始事实和证据：

```json
{
  "records": [
    {
      "entity_text": "bike chain replacement",
      "attribute": "amount",
      "value_text": "$120",
      "unit_text": "USD",
      "time_text": "last Saturday",
      "event_text": "purchased",
      "evidence_ids": [2]
    }
  ]
}
```

以下字段由程序生成，不允许模型填写：

- `record_id`；
- `occurrence_key`；
- 规范化金额、数值和日期；
- `route_status`；
- `lifecycle`；
- `created_at` 和数据库主键。

模型不得把没有直接证据的时区、年份、货币或日期补入结果。缺失字段保留为 `null`，不猜测。

### 5.2 程序规范化

程序分别保存原始字段和 typed 字段：

```text
value_text       模型从 evidence 中复制的原始表达
value_number     程序解析后的数值
unit             程序解析后的单位或货币
time_text        原始时间表达
normalized_time  程序解析后的日期/时间/区间
parse_status     parsed / unknown / ambiguous
```

规范化失败时保留 raw record，但不得把失败结果用于精确聚合、排序或 supersede。

相对时间以 evidence 所在 session/message 日期为 reference date。多条 evidence 的日期不同且
无法确定锚点时标记 `relative_time_ambiguous`，不强行解析。

## 6. Occurrence 去重与状态更新

### 6.1 identity key

程序必须区分两类 identity，不能把 `normalized_value` 无条件放进 key：

```text
snapshot_key = entity + attribute + normalized_time/scope
              + unit/currency + provider/location

occurrence_key = entity + event + normalized_time
                 + provider/location/scope
```

`snapshot_key` 不包含 value，才能识别同一属性从 `$120` 更新为 `$150`。普通 event 的
`occurrence_key` 也不把金额、数量或整句文本作为默认身份字段；只有数据本身明确提供了
可区分 occurrence 的字段时，才加入该字段。

不得使用模型自由改写的 `claim_text` 或整句文本 hash。字段不足以区分同一日的两次事件时，
标记 `ambiguous_occurrence`，保留两条记录，不强行合并。

### 6.2 更新与重复

- 同一 occurrence、同一 typed value：合并 evidence provenance；
- 同一 snapshot/occurrence、不同 value：标记 conflict，等待明确的后续事实；
- 明确出现“改为/现在是/实际为”等更新表达：程序只在有限词表命中时执行 replace；
- 无法确认是更新还是第二次事件：保留为两个 occurrence 或 ambiguous，不猜测；
- 不同日期、不同 provider 或不同 scope：默认视为不同 occurrence。

金额、数量、日期和时间的去重行为必须写入 SQLite，不能只保留最终状态。

## 7. Answer 上下文投影

### 7.1 不使用 graph-all

V5 不把全部节点和关系直接交给 Answer。上下文按以下顺序组织：

1. 与问题类型匹配的 V5 typed records；
2. 相关的 V3 current records；
3. raw tail。

每条 typed record 都带 `record_id`、规范化值、原始表达、时间和 evidence ID。上下文中不重复
输出同一事实的多种边或节点。

### 7.2 问题类型投影

使用确定性规则解析问题意图，不调用 LLM：

| 问题信号 | 优先记录 |
|---|---|
| how many / count / number | quantity/count records |
| how much / cost / price / spent | amount/currency records |
| when / what date / what time | date/time records |
| how long / duration | duration records |
| before / after / first / last | 带 normalized_time 的 occurrence |
| 无法可靠判断 | V5 records 全量的有界投影 + V3 memory |

程序只在单位、实体和 scope 兼容时执行 `count/sum/min/max/before/after`。不兼容时保留明细，
让 Answer 说明信息不足；不能生成未经证据支持的新数字。

### 7.3 预算

V3 memory、V5 projection 和 raw tail 共用现有 Answer budget。优先保留：

```text
明确命中的 typed records
同一 occurrence 的全部 evidence
与问题实体相同的 V3 records
raw tail 的最近完整 turn
```

发生截断时分别记录 `typed_context_truncated`、`v3_context_truncated` 和
`raw_tail_truncated`。不删除数据库中的原始记录。

## 8. 数据库与审计

V5 必须和 V3/V4 一样保留完整轨迹。推荐新增或复用以下逻辑表：

```text
v5_route_decisions
  sample_id, turn_ordinal, route, matched_patterns, router_version, input_hash

v5_batches
  memory_before, input_evidence, raw_response, parse_status, memory_after

v5_records
  raw fields, typed fields, occurrence_key, source_refs, route_status

v5_context
  question, selected_record_ids, projection_reason, projection_text, token_count
```

Answer call、DeepSeek judgment、token、延迟、失败重试和最终状态继续写入现有轨迹表。

要求：

- 同一 `sample_id + turn_ordinal + input_hash + router_version` 幂等；
- 原始响应和解析失败记录不可覆盖；
- 重试产生新的 attempt，但最终统计按最后一个 completed attempt；
- 可以从 SQLite 重放出同一组 typed records 和 occurrence keys；
- 所有投影记录输入 record IDs，禁止只保存不可解释的最终文本。

## 9. 对照实验

第一轮使用相同的历史、问题、Answer prompt、模型参数、`chunk=2048`、raw tail 和 judge：

| 组 | Memory 构建 | Answer 上下文 |
|---|---|---|
| V3 baseline | 全部 turn 走 V3 | V3 memory + raw tail |
| V4 negative control | 使用已有 V4 graph-all 结果 | graph-all + raw tail |
| V5 typed | 数字/时间 turn 走 V5，其余走 V3 | V5 typed + V3 memory + raw tail |

V4 只作为已有负向对照，不再为了本 pilot 扩大 graph-all。若 V3 和 V5 使用的模型不同，
必须重新构建 V3 baseline；不能把 `qwen3.8-27b` 结果与 `Qwen2.5-7B` 结果直接作为同一
组配对指标。

## 10. 评估指标

### 10.1 主要指标

- 目标问题端到端准确率；
- 数字/金额/数量子集准确率；
- 日期/时间子集准确率；
- V5 相对 V3 的逐题 paired accuracy。

### 10.2 Manager 指标

- evidence recall；
- typed field coverage；
- 金额/数量解析成功率；
- 日期/时间解析成功率；
- occurrence 去重 precision；
- ambiguous occurrence rate；
- 错误 provider/entity 关联率；
- 程序聚合正确率。

### 10.3 系统指标

- Answer 输入 token；
- Manager 输入/输出 token；
- LLM calls；
- 延迟；
- 失败重试次数；
- projection fallback 和截断次数。

DeepSeek 仍使用同一 rubric。LOCOMO 类别 5 单独评测，不能与普通正确性准确率混合。

## 11. 实施阶段

### Phase 0：冻结候选清单

1. 对 LongMemEval-S 120 和 LOCOMO 类别 1--4 做规则初筛；
2. 人工确认问题 primary type、evidence 和是否需要聚合；
3. 生成 48 条 pilot manifest，保存筛选规则版本；
4. 固定模型、Answer prompt、chunk 和 judge 配置。

### Phase 1：实现 typed memory

1. 实现 turn 级 router；
2. 实现最小 Manager schema；
3. 实现金额、数量、日期、时间解析器；
4. 实现 occurrence key 和保守去重；
5. 实现 SQLite 审计和 replay；
6. 加入离线单元测试，不调用 LLM。

### Phase 2：48 条 pilot

1. 运行 V5 memory build；
2. 使用同一 memory 回放 Answer；
3. 运行 DeepSeek judge；
4. 分析 typed record、projection、Answer 和 judgment 轨迹；
5. 只修复可泛化的程序规则，不针对单个 question_id 写特例。

### Phase 3：120 条确认实验

只有满足以下条件才扩大：

- typed records 和 evidence 可从数据库完整回放；
- 无未解释的重复、跨 provider 错误关联和时区补全；
- 目标子集准确率不低于 V3 pilot；
- 数值/时间题的错误归因可以落到 Manager、projection 或 Answer；
- 没有出现不可审计的 fallback 或上下文截断。

## 12. 成功与淘汰标准

48 条 pilot 不用于宣称最终提升，只用于判断实现是否值得扩大。

V5 进入 120 条确认实验的条件：

1. 数字/金额/数量和日期/时间两类的 paired accuracy 均不低于 V3；
2. 不引入明显的错误聚合和跨实体关联；
3. V5 context token 不高于 V4 graph-all；
4. 所有 route、parse、dedupe、projection 结果可从 SQLite 追溯。

若 V5 在 120 条上仍低于 V3，或程序规范化正确但 Answer 仍无法使用，则淘汰图谱/typed
memory 分支，保留 V3 作为主方案，并将失败结果作为消融实验记录。

## 13. 结论

V5 的核心不是继续修补 V4 图谱，而是缩小任务范围：模型抽取原始数值和时间证据，程序负责
规范化、去重、聚合和投影。第一轮先用 48 条相关问题验证这一分工，再决定是否扩大数据集。
