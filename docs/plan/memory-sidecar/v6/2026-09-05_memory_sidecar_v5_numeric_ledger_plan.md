# Memory Sidecar V6 数字事实账本方案（延期预研）

> 状态：延期到 V6。本文件描述完整的 typed ledger、QuerySpec、程序计算和专用 Answer
> 上下文，当前不实施。当前阶段只实施 V5 原始事实抽取层，见
> [2026-09-05_memory_sidecar_v5_raw_fact_extraction_plan.md](../../archive/memory-sidecar/v5/2026-09-05_memory_sidecar_v5_raw_fact_extraction_plan.md)。

## 1. 结论先行

V4 不再作为 V3 的通用替代方案。120 条全量实验中，V3 为 81/120（67.50%），V4
`graph-all` 为 68/120（56.67%）。V4 的图谱写入、规范化和去重基本可审计，但 Answer
需要从全图中自行完成实体筛选、状态判断和数字聚合，导致上下文竞争。

V5 设计为一个独立的数字事实账本（Numeric Fact Ledger），只处理：

```text
数字、金额、数量、日期、时间、持续时间、百分比、频率和区间
```

架构保持双通道：

```text
所有 user evidence
        ├── V3 通用 memory（保持现有协议）
        └── 结构化信号命中时 -> V5 typed ledger

问题 -> QuerySpec 编译 -> 程序查询/计算 -> compact numeric context
      -> Answer；无法可靠查询时回退 V3
```

V5 不创建通用实体关系图，不把完整账本或完整图谱交给 Answer，也不让 LLM 负责最终的
`sum/count/date difference` 计算。非数字问题的上下文和 Answer prompt 与 V3 保持一致。

## 2. 实验中必须解决的问题

### 2.1 V3 的失败类型

从 V3 轨迹看，数字题主要有四类失败：

1. 多条金额或数量存在于 memory，但 Answer 漏掉一条，不能稳定求和；
2. provider、对象或时间范围相近，Answer 选择了错误候选；
3. `target/preference/observed/completed` 混在文本中，目标时间被当成实际时间；
4. 事件明细、累计快照和新增事实没有区分，Answer 自行决定是否重复计数。

这些失败不应通过给 Answer 增加更多原文来解决，因为上下文越大，候选竞争越严重。

### 2.2 V4 的失败类型

V4 证明了程序级抽取、规范化、去重和 SQLite 追溯是可行的，但 `graph-all` 有三个问题：

- 相关事实和无关事实同时呈现，金额聚合被污染；
- 辅助关系、时间关系和状态关系没有按问题投影；
- 图谱“存储正确”不等于 Answer“选择正确”。

因此 V5 的新变量是“有类型的查询结果”，不是“更大的图谱”。

## 3. 目标与非目标

### 3.1 目标

- 对直接证据支持的金额、数量、日期和时间建立可查询的 typed records；
- 将事实抽取、规范化、去重、查询和计算拆开，分别记录轨迹；
- 保留原始表达、evidence ID 和 session 日期，任何结果可回放；
- 在数字题上提升 V3，且普通题不低于 V3；
- 规则只依赖通用字段和操作，不依赖 question ID、答案或数据集实体名称。

### 3.2 非目标

- 不构建通用知识图谱；
- 不由模型猜测缺失的年份、时区、货币、数量或事件次数；
- 不用 embedding 相似度做去重；
- 不使用 reference answer、gold evidence 或最终答案参与路由；
- 不修改当前 V3 Answer prompt，先只改变其可见上下文。

## 4. V5 的核心对象：不可变事实账本

V5 每次从 user evidence 抽取一条或多条原始事实。程序为事实生成不可变记录；后续“更新”
不覆盖旧记录，而是新增版本并保留关系。这样既能回答“当前值”，也能追溯“为什么变成当前值”。

### 4.1 最小模型输出

模型只负责复制 evidence 中的事实原子，不负责类型、ID、计算或去重：

```json
{
  "records": [
    {
      "subject_text": "I",
      "predicate_text": "spent",
      "object_text": "bike chain replacement",
      "value_text": "$25",
      "unit_text": "USD",
      "time_text": "last Saturday",
      "qualifier_text": "completed purchase",
      "evidence_ids": ["u-17"]
    }
  ]
}
```

`value_text/unit_text/time_text/qualifier_text` 都是可选字段；没有直接证据时必须为 `null`。
`claim_text` 不再要求模型重写，原始 claim 直接从 evidence message/quote 保存。

程序生成以下字段，模型禁止填写：

```text
fact_id, snapshot_key, occurrence_key, value_type, normalized_value,
status, session_date, route_status, parse_status, created_at
```

### 4.2 Evidence 约束

- `evidence_ids` 必须存在，且 role 必须是 `user`；assistant、question、answer 不能写入账本；
- `value_text/time_text/qualifier_text` 若非空，必须是对应 user evidence 的严格子串，或由
  程序从该子串切片得到；
- evidence 日期以所在 session/message 为准；多条 evidence 日期冲突时标记
  `relative_time_ambiguous`；
- 解析失败保留 raw record，但不能用于精确计算、排序或 supersede。

## 5. 程序规范化协议

原始字段和 typed 字段始终同时保存：

| 字段 | 说明 |
|---|---|
| `value_text` | 原文表达，例如 `$25`、`three courses` |
| `value_type` | `money/count/number/date/time/duration/percentage/range/frequency` |
| `value_number` | 程序解析的数值；金额统一保存最小货币单位整数 |
| `currency/unit` | ISO 货币或受控单位；原文没有则为 `unknown` |
| `date_start/date_end` | 日期或日期区间；不确定时为空 |
| `time_minute` | 当日分钟数；不包含未经证据支持的时区 |
| `duration_seconds` | 持续时间的标准单位 |
| `parse_status` | `parsed/unknown/ambiguous/invalid` |
| `raw_expression` | 无法解析时的原始表达 |

规范化规则必须是版本化的纯函数：相同输入、同一规则版本得到相同结果。浮点金额不参与
比较；货币不同不能直接相加。`three` 等数字词可用固定词典解析，但不允许根据常识补全。

相对时间使用 evidence 所在 session 的日期：

```text
reference_date = evidence.session_date
```

若原文只有“上周”而 session 日期缺失，保存 raw expression 和 `unknown`，不使用运行日期
或数据集所在地推断。

## 6. 事实语义：快照、事件和状态必须分开

每条记录由程序根据受控 predicate contract 标记 `record_kind`：

```text
snapshot    当前累计值或属性值，例如“共有 38 枚硬币”
occurrence  一次具体事件，例如“周六买了链条”
measurement 一次观察值，例如“今天体重 70kg”
```

模型可以提供 `predicate_text` 和 `qualifier_text`，但最终 `record_kind` 由程序规则和显式
措辞共同决定；无法判断时使用 `unknown`，不猜测。

状态是事实的一部分，而不是普通文本：

```text
observed, completed, planned, target, preference, cancelled, missed, unknown
```

例如“目标 7:30 起床”和“实际 8:30 起床”必须是两条不同记录。只有在有限的、可测试的
更新词表命中“改为/现在是/实际为”等明确表达时，程序才把同一 snapshot 标记为新版本；
否则保留并列版本或 conflict。

## 7. 程序级去重与更新

LLM 不参与去重。去重只使用规范化字段，不使用模型改写的整句文本：

```text
snapshot_key = subject + predicate + scope/time + provider/location + unit
occurrence_key = subject + predicate + object + time + provider/location + scope + status
```

规则：

1. 同一 key、同一 typed value：合并 provenance，保留全部 evidence IDs；
2. 同一 snapshot key、不同 value：新增版本并标记 `conflict`，不静默覆盖；
3. 同一日期、同一对象但没有 discriminator：标记 `ambiguous_occurrence`，两条都保留；
4. 不同日期、provider、location、scope 或 status：默认不是重复；
5. 同一事实重复出现在不同 chunk：由 canonical key 合并，而不是依赖 chunk ID；
6. 旧版本只有在查询要求“current/latest”且 status/scope 兼容时才被排除。

“一共买了一个还是两个”这类原文无法区分的问题不强行解决；账本必须保留歧义并在查询结果
中明确 `ambiguous_count`，避免伪造精确答案。

## 8. 问题查询编译器

数字题和数字事实是两个不同路由：事实进入账本不代表所有问题都要查账本。Answer 前先把
当前问题编译成受控 `QuerySpec`：

```json
{
  "operation": "sum",
  "value_type": "money",
  "subject": "I",
  "predicate": "spent",
  "object": "bike-related items",
  "time_filter": {"start": null, "end": null, "raw": "all purchases"},
  "status_filter": ["completed"],
  "group_by": ["provider"],
  "parse_status": "parsed"
}
```

支持的 operation 只有：

```text
lookup, count, sum, min, max, difference, duration, before, after, latest, membership
```

`QuerySpec` 可以由规则解析器优先生成，规则无法覆盖时使用一个严格 JSON 输出的轻量 LLM
解析器；LLM 只填写问题中的实体、运算和筛选文本，程序验证字段并拒绝猜测。query parser
的调用、版本、原问题和失败原因必须落库。

程序只在以下条件满足时计算：单位/货币兼容、实体和 scope 兼容、状态符合、时间范围可解析、
没有未处理的 ambiguous record。否则返回候选明细和 `calculation_status=insufficient`，再
回退 V3，而不是输出看似精确的错误数字。

## 9. Answer 上下文

V5 使用紧凑的“计算上下文”，不使用 V4 `graph-all`：

```text
Question operation: SUM money
Included facts:
  [f-12] completed | bike chain | $25 USD | 2023-05-13 | evidence u-17
  [f-19] completed | bike rack   | $160 USD | 2023-05-13 | evidence u-21
Excluded facts:
  [f-20] planned | bike lights | $30 USD | excluded: status != completed
Calculation: 25 + 160 = 185 USD
Ambiguous facts: 0
```

渲染优先级：

1. QuerySpec 命中的 typed records；
2. 被排除但会造成歧义的 records，以及排除原因；
3. 与问题实体直接相关的 V3 records；
4. 最近的完整 raw user turn。

程序先计算，Answer 只负责把已验证结果组织成自然语言。纯 `lookup/count/sum` 且计算状态为
`complete` 时，可以使用确定性答案 renderer；需要解释时再调用现有 V3 Answer prompt。

V3 memory、V5 context 和 raw tail 共用 Answer budget，分别记录
`typed_context_truncated/v3_context_truncated/raw_tail_truncated`。任何一层截断都不能删除
SQLite 中的原始事实。

## 10. 路由与回退

### 10.1 事实路由

V3 对所有 user evidence 正常运行；V5 只在当前 turn 命中通用结构化信号时运行：

- 货币符号或货币代码；
- 数字与单位组合；
- 日期、时间、百分比、范围和频率表达；
- `how many/how much/when/how long/total/average/before/after` 等运算意图。

孤立年份、编号、产品型号和普通整数不单独触发 V5。每次保存命中的模式、router version
和 input hash，禁止写 question ID 特例。

### 10.2 查询回退

以下任一情况回退 V3：

- QuerySpec 无法可靠解析；
- 没有命中事实或 required field 未解析；
- 货币/单位不兼容；
- 时间锚点缺失或多个 evidence 日期冲突；
- ambiguous occurrence 影响目标运算；
- typed context 超预算且无法按事实完整保留。

回退不是静默行为，必须保存 `fallback_reason`，以便区分“账本没有事实”“查询没解析”与
“Answer 选择错误”。

## 11. SQLite 追溯设计

新增逻辑表，沿用现有 `samples/calls` 记录模型调用、token、延迟和失败重试：

```text
v5_numeric_routes
  sample_id, turn_ordinal, route, matched_patterns, router_version, input_hash

v5_numeric_batches
  sample_id, batch_ordinal, evidence_json, raw_response, parse_status,
  parser_version, input_hash

v5_numeric_facts
  fact_id, sample_id, batch_id, raw_fields_json, typed_fields_json,
  record_kind, status, snapshot_key, occurrence_key, route_status,
  parse_status, source_evidence_json

v5_numeric_versions
  fact_id, supersedes_fact_id, relation, update_reason, created_at

v5_query_specs
  sample_id, question, spec_json, parser_version, parse_status, fallback_reason

v5_numeric_context
  sample_id, selected_fact_ids, excluded_fact_ids, operation,
  calculation_json, projection_text, token_count, truncation_flags
```

要求：

- `sample_id + turn_ordinal + input_hash + router_version` 幂等；
- 相同 batch 重放产生相同 typed fields 和 keys；
- 原始 response、parse failure 和旧版本不可覆盖；
- 并发写入使用现有 SQLite 写锁/事务机制；
- 任何最终答案都能反查 QuerySpec、fact IDs、evidence 和计算过程。

## 12. 泛化性约束

为了避免针对样本修规则，实施时必须满足：

1. 规则按 `value_type/operation/status/record_kind` 组织，不按 question ID、实体名称或答案
   文本组织；
2. 先用合成样本覆盖金额、单位、相对日期、更新、重复和歧义，再用真实样本验证；
3. 训练/调试时不读取 reference answer 和 gold evidence；
4. 任何新规则必须有正例、反例和跨数据集测试；
5. 失败统一进入 `unknown/ambiguous/fallback`，不通过常识补全；
6. 报告分别统计抽取、规范化、去重、查询、计算、Answer 五个阶段，不能只看最终准确率。

## 13. 实验计划

### Phase 0：离线协议测试

- 实现金额、数量、日期、时间、持续时间和状态解析器；
- 用合成案例测试 canonical key、版本更新、冲突和幂等回放；
- 固定 `chunk=2048`，冻结 router/parser 版本；
- 不调用 Answer，先保证账本结果可解释。

### Phase 1：4 条轨迹试跑

- 选择金额求和、数量汇总、目标/实际时间、相对日期四类样本；
- 并发 2，检查 SQLite 锁、facts、QuerySpec、context 和 fallback；
- 逐条阅读完整轨迹，只修复泛化规则。

### Phase 2：24 条数字/时间 pilot

- V3 baseline、V4 graph-all 负向对照、V5 ledger 三组使用同一问题和 judge；
- DeepSeek 只评最终答案，另保存程序级 exact metrics；
- 输出每题 paired comparison 和 token/延迟。

### Phase 3：120 条确认实验

只有 Phase 2 满足下列条件才扩大：

- 数字/金额/日期/时间/数量目标子集高于 V3；
- 非目标题与 V3 相同或不低于统计噪声范围；
- 无未经证据支持的金额、日期、时区和数量；
- 去重、计算和回退原因可从 SQLite 完整复现；
- 不出现由 graph-all 引入的上下文竞争。

## 14. 评估指标与淘汰标准

### 14.1 主要指标

- 数字/金额/数量准确率；
- 日期/时间/持续时间准确率；
- 全量 120 条 paired accuracy；
- `count/sum/min/max/difference` 程序计算 exact rate。

### 14.2 诊断指标

- evidence recall、typed field coverage；
- parse success、ambiguous rate、duplicate precision；
- wrong provider/entity association；
- query parse success、candidate recall、false-positive rate；
- fallback rate、context token、LLM calls、latency。

### 14.3 预注册门槛

- V5 目标子集不得低于 V3；理想目标是至少提升 10 个百分点；
- 全量不得低于 V3；非目标问题不得因 V5 变化而回退；
- `unsupported_inference_count = 0`；
- 所有最终数字必须来自已保存 fact 或明确计算式；
- 若目标子集提升但全量下降，V5 只能作为数字题专用分支，不能替换 V3。

## 15. 最终判断

V5 不试图证明“图谱比 JSON 更好”，而是验证一个更窄、更可归因的假设：

> 对数字事实，类型化账本 + 程序查询/计算，比把混合关系交给 Answer 自行推理更可靠。

V3 仍是通用主方案。V5 只有在目标问题上通过 paired 实验，且具备可回放、可解释、可回退
的行为后，才进入生产式混合路由；V4 graph-all 保留为负向对照，不再作为主线继续堆规则。
