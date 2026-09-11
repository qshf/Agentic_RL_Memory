# Memory Sidecar V5 事件束事实抽取方案

## 1. 版本定位

V4 的 `graph-all` 在 120 条全量实验中低于 V3：

```text
V3：81/120 = 67.50%
V4：68/120 = 56.67%
```

问题不只是事实有没有进入数据库，而是 Answer 同时承担了实体筛选、时间判断、状态判断
和数字聚合。V5 不继续修补 V4 图谱，也不提前实现完整数字账本，而是先验证抽取层：

> 模型能否把事件、金额/数量、日期/时间、状态和证据绑定成一个可追溯的事件束。

当前版本只做 shadow extraction，不改变 V3 的 memory、Answer 上下文和最终答案。

```text
历史 chunk（chunk=2048）
        ├── V3 Manager：正常构建现有 memory
        └── V5 Event-Bundle Extractor：旁路抽取并写入 SQLite

Answer：仍使用 V3 memory + 原有 raw tail
```

已有的 [2026-09-05_memory_sidecar_v5_raw_fact_extraction_plan.md](../../archive/memory-sidecar/v5/2026-09-05_memory_sidecar_v5_raw_fact_extraction_plan.md) 保留为早期 raw-span 草案；
本文件是当前 V5 执行方案。完整 typed ledger、规范化、去重、查询和计算延期到 V6。

## 2. 为什么不能只抽取数字片段

仅保存以下内容是不够的：

```json
{"raw_text":"$25", "kind_hint":"amount"}
```

因为它无法说明 `$25`：

- 对应哪个对象；
- 是已完成、计划还是预算；
- 与同一句中的另一个金额是否属于同一事件；
- `Saturday` 是事件发生时间还是记录时间；
- `7:30` 是目标时间还是实际时间。

例如：

```text
I paid $160 for the bike and $25 for the chain on Saturday.
```

V5 必须至少保留：

```text
bike -> $160 -> paid -> Saturday
chain -> $25 -> paid -> Saturday
```

本轮仍不判断两个金额是否应该相加，只保存后续 V6 所需的局部绑定关系。

## 3. 外部研究带来的设计启发

### 3.1 事件中心的记忆编码

ARTEM 将输入抽取为带有时间、空间、实体和语义信息的事件，再存入 episodic memory，而不是
只保存孤立文本片段。[ARTEM](https://ojs.aaai.org/index.php/AAAI/article/view/39773)

本项目吸收其中的“事件中心”思想，但不引入其神经记忆模块：V5 只保存事件束和 evidence，
后续检索与计算留给 V6。

### 3.2 时间约束先结构化

TimeR⁴ 采用问题重写、时间约束检索和时间相关性重排，以减少模型直接从混合文本中判断时间
关系的负担。[TimeR⁴](https://aclanthology.org/2024.emnlp-main.394/)

SAR 进一步强调先将问题拆成实体、关系和时间约束，再进行 schema-consistent retrieval，最后
验证答案是否满足时间条件。[SAR](https://ojs.aaai.org/index.php/AAAI/article/view/40369)

V5 暂不实现查询阶段，但必须把原始时间角色保存下来，例如 `event_time`、`target_time`、
`valid_time`、`session_time`，不能只存一个孤立日期。

### 3.3 计算与语言生成分离

Program of Thoughts 将数值推理和外部程序计算分离，避免模型在自然语言推理过程中同时承担
算术运算。[Program of Thoughts](https://arxiv.org/abs/2211.12588)

V5 只做输入事实抽取；金额求和、数量统计、日期差和排序统一放到 V6 的程序查询层，避免在
当前版本提前引入不可归因的答案变化。

## 4. V5 的边界

### 4.1 处理内容

抽取原文中直接出现且有事实可能性的：

- 金额和货币：`$25`、`120 USD`、`€40`；
- 数量和计数：`3 books`、`three courses`、`38 coins`；
- 日期：`May 20, 2023`、`2023-05-20`、`last Saturday`；
- 时间：`7:30 AM`、`18:30`、`around noon`；
- 持续时间、百分比、范围和频率：`5 days`、`20%`、`7-10 days`、`twice a week`；
- 与上述表达同一事件中的对象、动作、状态和时间角色。

### 4.2 当前不处理

- 不将金额转成标准货币或最小单位；
- 不将相对日期解析成绝对日期；
- 不做 occurrence/snapshot 去重和更新；
- 不计算 `sum/count/min/max/difference`；
- 不判断当前值、最终值或历史值；
- 不修改 V3 memory 和 Answer 输入；
- 不使用 question、reference answer、gold evidence 或最终答案指导抽取；
- 不因单条 fact 失败而拒绝整个 chunk。

## 5. 输入和证据规则

### 5.1 处理单位

V5 与 V3 使用同一个 `chunk=2048` 的历史分块，每个 chunk 独立抽取。模型接收完整的本地
evidence 编号和 user/assistant 角色，但只有 `user` evidence 可以成为有效事实来源。

V5 不接收当前问题，避免“问题驱动抽取”造成只抽答案相关数字，也不读取 LongMemEval 的
evidence 标注。

### 5.2 强制证据约束

- 每个事件必须有至少一个本地 `evidence_id`；
- 所有 `raw_text`、对象片段、动作片段、时间片段必须是 user message 的连续子串；
- assistant 中出现的数字只能记录为被拒绝的候选，不得进入有效事件；
- 缺失的年份、时区、货币、单位和事件次数保持缺失，不允许常识补全；
- 证据跨多条 user message 时，保留多个 evidence ID，不拼接成未经验证的新句子。

## 6. 最小事件束协议

模型只负责从原文复制事件及其局部组成，不负责规范化、ID、去重和计算：

```json
{
  "events": [
    {
      "event_text": "I paid $160 for the bike and $25 for the chain on Saturday.",
      "subject_text": "I",
      "action_text": "paid",
      "objects": [
        {"text": "the bike", "value_text": "$160"},
        {"text": "the chain", "value_text": "$25"}
      ],
      "time_mentions": [
        {"text": "Saturday", "role_hint": "event_time"}
      ],
      "status_text": "paid",
      "evidence_ids": [2]
    }
  ]
}
```

字段要求：

| 字段 | 必填 | 说明 |
|---|---:|---|
| `event_text` | 是 | 原文连续片段；优先覆盖一个完整事件句 |
| `evidence_ids` | 是 | 当前 chunk 的 user evidence ID |
| `subject_text` | 否 | 事件主体原文片段 |
| `action_text` | 否 | 原文动作，如 `paid`、`bought`、`planned` |
| `objects` | 否 | 对象及其同事件中的数值原文 |
| `time_mentions` | 否 | 时间原文及角色提示 |
| `status_text` | 否 | 原文状态表达，如 `planned`、`actually` |

模型不输出 `fact_id`、`event_id`、日期标准值、金额标准值、predicate、occurrence key、
snapshot key、confidence 或 route status。

## 7. 事件与数字、时间的绑定规则

### 7.1 数字绑定

数字不能脱离事件单独返回。每个金额/数量必须放在：

```text
event -> object -> value_text
```

如果一个数字无法绑定对象，仍可放入 `unbound_values`，但必须标记 `binding_status=unknown`，
不能由模型猜对象。

### 7.2 时间绑定

每个时间表达必须带原始角色提示：

```text
event_time       事件发生时间
target_time      目标时间
observed_time    实际观察时间
valid_time       事实有效区间
session_time     消息所属 session 时间（程序注入）
unknown          无法判断角色
```

例如：

```text
The target is 7:30, but I usually wake up at 8:30.
```

应保留：

```text
7:30 -> target_time
8:30 -> observed_time
```

不能只返回两个无关系的时间值。

### 7.3 多事件句

一个 user message 中有多个事件时，模型可以返回多个事件束，但每个事件必须有独立的
`event_text` 和对象绑定。若无法判断是否是一个事件还是两个事件，返回一个事件束并设置
`event_boundary_status=ambiguous`，不强行拆分或合并。

## 8. 程序只做校验和审计

程序职责限定为：

1. 校验 JSON schema；
2. 校验 evidence ID 是否存在且 role 为 user；
3. 校验所有原文片段是否为 evidence 的连续子串；
4. 检查 object/value/time 是否来自同一事件 evidence；
5. 标记 `invalid_evidence`、`substring_mismatch`、`unknown_hint` 和
   `ambiguous_boundary`；
6. 保存原始模型响应、解析失败和被拒绝的候选。

本轮程序不执行语义纠正，不把 `$25` 转成 25，不把 `Saturday` 解析成日期，不把两个金额
相加，也不做去重。相同事件在不同 chunk 重复出现时保留重复观察，交由 V6 处理。

## 9. SQLite 记录

沿用现有 `samples`、`calls` 和 `sidecar_batches`。建议新增：

```text
v5_event_routes
  sample_id, batch_ordinal, route, matched_signals, router_version, input_hash

v5_event_batches
  sample_id, batch_ordinal, input_text, evidence_json, raw_response,
  parse_status, extractor_version, input_hash, created_at

v5_event_bundles
  sample_id, batch_id, event_ordinal, event_json, evidence_ids_json,
  event_boundary_status, validation_status, created_at

v5_event_mentions
  bundle_id, mention_ordinal, mention_type, raw_text, role_hint,
  parent_object_text, substring_status, binding_status, created_at
```

要求：

- `sample_id + batch_ordinal + input_hash + extractor_version` 幂等；
- 原始 response 和失败记录不可覆盖；
- 删除进程内缓存后重放，raw bundle、mention 和 validation status 必须一致；
- 并发 2 使用现有 SQLite WAL、busy timeout 和文件锁；
- 可从 `sample -> batch -> event bundle -> mention -> evidence` 完整回查。

V5 结果不写入 `sidecar_memory`，不影响 V3 的 state、route 和 Answer。

## 10. 评估指标

V5 本轮不以最终 Answer accuracy 作为主指标，而是验证抽取能力：

- `event_recall`：标注事件是否被抽取；
- `numeric_span_recall`：金额和数量原文是否被覆盖；
- `temporal_span_recall`：日期、时间和持续时间原文是否被覆盖；
- `event_value_binding_accuracy`：数字是否绑定到正确对象；
- `time_role_binding_accuracy`：target/observed/event time 是否正确绑定；
- `evidence_precision`：是否引用正确 user message；
- `invalid_evidence_rate`：非法 evidence 引用比例；
- `false_positive_rate`：普通编号、型号、代码被误抽取的比例；
- `event_boundary_ambiguity_rate`：无法可靠拆分的事件比例；
- token、延迟、失败重试和 SQLite 写锁冲突。

## 11. 实验阶段

### Phase 0：离线协议测试

- 固定 `chunk=2048`、temperature=0 和 extractor prompt 版本；
- 实现 parser、evidence validator 和 SQLite 表；
- 用合成句覆盖多金额、多数量、target/observed、相对时间、assistant 污染和模糊事件边界；
- 不调用 Answer。

### Phase 1：4 条 smoke

选择四类样本：

1. 多金额同句；
2. 多数量跨 session；
3. target 与 observed 时间并存；
4. 相对日期且 session 日期可追溯。

并发 2 运行，检查事件束、mention 绑定、SQLite 锁和重放一致性。

### Phase 2：24 条 pilot

覆盖金额、数量、日期、时间、持续时间、频率和事件歧义。V3 继续生成最终答案，但 V5 只
旁路记录；逐条分析抽取轨迹，不根据单个 question ID 写规则。

### Phase 3：120 条 holdout

冻结 extractor 版本后再跑 120 条，验证跨数据集泛化。只有 V5 的事件和值绑定稳定，才进入
V6 的规范化、去重和查询设计。

## 12. 进入 V6 的门槛

- 事件、数字、时间和 evidence 的绑定结果可完整回放；
- 24 条 pilot 中不出现系统性的金额漏绑、target/observed 混淆或 assistant 污染；
- holdout 不依赖实体名称、question ID 或答案关键词；
- V3 在启用与不启用 V5 时最终答案一致；
- 失败可明确归因于模型漏抽、事件边界不确定或证据校验失败。

V6 才负责：

```text
事件束 -> typed ledger -> 规范化 -> 去重/版本 -> QuerySpec -> 程序计算 -> Answer context
```

## 13. 最终判断

V5 的产物不是一个新的 Answer 方案，而是一层可靠的“事件事实采集器”。它回答的是：

> 原文中的数字和时间，是否被正确地绑定到了对象、动作、状态和证据上？

先把这个问题单独测清楚，才能在 V6 判断程序规范化、查询和聚合是否真正有效，也能避免再次
把 V4 的图谱存储问题、Answer 选择问题和数值计算问题混成一个不可归因的版本。
