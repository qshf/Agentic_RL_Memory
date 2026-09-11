# LongMemEval 原始数据分析与记忆库架构建议

## 1. 结论先行

当前 v5-demo 效果差，不只是抽取 prompt 的问题，而是记忆对象定义错了：

```text
把每个 turn 改写成 flat fact，再把所有 fact 交给 Answer
```

不适合这份数据。

原始数据需要的是：

```text
不可变原文消息
  -> 事件/属性/偏好/计划/更新记录
  -> 实体和事件之间的关系
  -> 以当前问题为条件的结构化检索
  -> 少量原文证据 + 必要的程序计算
  -> Answer
```

用户提出的“LLM 生成问题，再让另一个 LLM 选择消息序号”可以保留，但只能作为**证据召回
层**，不能直接作为长期记忆库。长期记忆库必须同时保存：原文、来源、实体、事件、数值、
时间角色、状态和更新关系。

## 2. 原始数据的事实结构

以下统计来自 `data/official_longmemeval/longmemeval_s_cleaned.json` 的全部 500 条样本，
没有使用 v5 输出。

### 2.1 历史很长，但有效证据很少

| 指标 | 结果 |
|---|---:|
| 样本数 | 500 |
| 每题 session 数 | 平均 47.7，中位数 48，范围 38–62 |
| 每题消息数 | 平均 493.4，中位数 491 |
| 每题历史字符数 | 约 489K |
| 可回答样本 | 470 |
| 明确不可回答/缺失信息样本 | 30，占 6% |
| 可回答样本的答案 session 数 | 平均 1.89 |
| 每题 `has_answer` 消息数 | 平均 1.79 |
| 标记消息字符占完整历史字符 | 约 0.13% |

可回答题的答案 session 分布为：

```text
1 个 session：170
2 个 session：229
3 个 session：39
4 个 session：18
5 个 session：11
6 个 session：3
```

这说明真正需要进入 Answer 的不是几百个 session，而是少量经过关系筛选的证据消息。

### 2.2 答案位置不是“最近消息”

答案 session 在按时间排序后的历史中位于大约第 24–28 个 session，通常在整个 48 个 session
历史的中间位置。仅保留最近 raw tail 无法覆盖这类答案；仅做旧摘要也容易把少量关键消息
压掉。

### 2.3 `has_answer` 的来源分布

全部 500 条中，标记消息有：

```text
user：842 条
assistant：54 条
```

其中 assistant 标记几乎集中在 `single-session-assistant` 类型；其它类型主要依赖用户自己的
陈述。这意味着：

- user 消息不能被摘要器忽略；
- assistant 消息也不能整体删除，因为部分问题的答案就是 assistant 给出的推荐或说明；
- 但 assistant 中的泛化建议、免责声明和模板化解释必须和可复用的 assistant fact 区分开。

### 2.4 session 是主题线程，不是单一事实

一个 session 通常包含多轮围绕同一主题的问答，例如：

```text
用户提到自行车维护费用
  -> assistant 给建议
  -> 用户补充另一个费用/日期
  -> assistant 继续建议
```

最终问题可能要求把多个 session 中的费用相加，或者把一个 session 的实体和另一个 session
的更新状态拼起来。因此记忆单元不能只是孤立的数值，也不能只是整段 session 摘要。

## 3. LongMemEval 真正考察的关系

题型名称只是表面分类，实际需要的记忆操作如下：

| 数据能力 | 典型操作 | 记忆库要求 |
|---|---|---|
| single-session-user | 找用户明确说过的属性/事实 | 原文消息 + user provenance |
| single-session-assistant | 找 assistant 给出的具体推荐/信息 | assistant fact 与模板建议分离 |
| multi-session | 多 session 聚合、计数、求和 | 事件实例、实体绑定、去重规则 |
| knowledge-update | 旧值与新值冲突 | 版本链、有效时间、supersedes |
| temporal-reasoning | 日期差、先后、持续时间 | event_time、target_time、valid_time |
| single-session-preference | 从对话中推导偏好和限制 | preference/polarity/strength/source |
| absent questions | 信息未出现 | 支持“无证据”而不是强行猜测 |

最常见的错误不是“没看到数字”，而是：

```text
数字找到了，但不知道属于哪个对象、哪次事件、哪个状态或哪个时间范围。
```

例如自行车费用题需要：

```text
chain -> $25 -> paid -> April 20
bike lights -> $40 -> installed -> April 20
helmet -> $120 -> bought -> April 10
```

之后才可能判断题目是否要合计三项，而不是把所有 `$` 数字直接相加。

## 4. 对“生成问题再选消息”方案的判断

### 4.1 可保留的部分

它比 flat fact 抽取更好的一点是：最终保存的是原文句子，避免模型改写金额、日期和名称；
消息序号也能提供可追溯 provenance。

LLM-A 生成的问题可以作为一种窗口级 schema planner，例如：

```text
这个窗口是否包含金额及其对象和动作？
这个窗口是否包含同一事件的多个数量？
这里的时间是事件发生时间、目标时间还是实际观察时间？
是否存在旧值和新值，或者计划与已完成状态的冲突？
```

### 4.2 不能直接照做的部分

不要让 LLM-A 在整段窗口上自由发散地生成问题，原因有三点：

1. 它会为普通编号、产品型号和 assistant 示例编造“看起来值得记忆”的问题；
2. 问题数量会随窗口内容和模型随机性变化，难以重放和评估；
3. 选择到消息之后，仍然没有解决跨窗口实体对齐、旧值更新和事件去重。

因此建议把问题生成限制为固定 schema 问题集，LLM-A 只判断哪些问题在当前窗口有候选，
LLM-B 才选择原文消息。问题本身不是记忆记录。

## 5. 推荐的记忆库分层

### 5.1 L0：不可变原文层

这是唯一事实源，任何模型都不能覆盖：

```text
sessions
  session_id, session_index, session_date, original_index

messages
  global_message_id, session_id, ordinal, role, content,
  content_hash, created_at
```

所有后续记录只引用 `global_message_id` 和可选的字符 span。不要把原文只存在 prompt 或
模型响应中。

### 5.2 L1：候选提及层

由规则和小模型低风险识别，不做复杂推理：

```text
mentions
  mention_id, message_id, span_start, span_end, raw_text,
  mention_type, parser_status
```

`mention_type` 包括 amount、quantity、date、time、duration、frequency、entity、action、
status 等。这里保存 `$25`、`Saturday`、`three books` 的原文，不把它们转成标准值。

候选层的作用是减少 LLM 需要处理的文本量，也减少问题生成器自由发散。

### 5.3 L2：事件和属性层

LLM 在 2048/4096 窗口内只负责把候选绑定成事件，输出引用 ID 和原文 span：

```text
events
  event_id, subject_entity_id, event_type, action_raw,
  status_raw, event_time_raw, source_message_ids

event_attributes
  event_id, attribute_key, raw_value, unit_raw,
  object_entity_id, source_message_id, source_span
```

一个事件可以有多个对象和数值；金额/数量不能脱离事件单独落库。

### 5.4 L3：实体、偏好和更新关系层

```text
entities
  entity_id, surface_forms, entity_type, source_message_ids

preferences
  subject_entity_id, predicate, object_raw, polarity,
  strength, status, source_message_id

memory_links
  from_id, to_id, relation_type, evidence_message_ids
```

关系类型至少包括：

```text
same_entity | same_event | part_of | related_to |
supersedes | contradicts | planned_as | completed_as |
before | after | valid_during
```

旧值和新值不能覆盖写入，必须通过 `supersedes` 或版本链表达。

### 5.5 L4：检索投影层

使用 SQLite + FTS5 保存可检索的原文和结构化字段：

```text
retrieval_units
  unit_id, source_kind, source_id, searchable_text,
  entity_ids, event_type, time_bucket, numeric_terms
```

向量检索可以作为补充，但不能替代实体、时间、状态和数值字段；这类题需要精确过滤和
程序计算，单纯 embedding 相似度不够。

## 6. 推荐的运行流程

### Step 1：原文入库

按日期排序 session，给每条消息分配稳定的 `global_message_id`。保留 role、session 日期、
原始顺序和 hash。

### Step 2：确定性候选扫描

使用正则、日期解析器和轻量 NER 找出数字、货币、日期、时间、持续时间、频率、专名和动作
词。这个阶段允许漏掉复杂表达，但不允许改写原文。

### Step 3：窗口内关系绑定

按完整 turn 切分 2048/4096 token 窗口。LLM 输入当前窗口和候选提及，输出：

- 事件边界；
- subject/action/object；
- 数字与对象绑定；
- 时间角色；
- 计划/完成/取消/更新状态；
- 原文 message_id 和 span。

LLM 不输出 normalized amount、normalized date、confidence 或最终答案。

### Step 4：跨窗口实体和事件链接

只对候选相似的记录做 pairwise/linking：

```text
同名实体 + 相近主题 + 时间相容
  -> same_entity / same_event 候选
```

先用字符串、别名、session 主题和时间做程序过滤，再让 LLM 判断边界；不要让 LLM 把全部
500 条 session 一次性重新读一遍。

### Step 5：写入 append-only ledger

新事实追加；更新记录指向旧记录；无法判断是否同一事件时保留两个事件并标记 ambiguous。
任何模型输出都先经过 schema、source message、span 和时间角色校验。

### Step 6：当前问题解析

最终收到用户问题时，单独解析成 QuerySpec：

```json
{
  "entity": "bike-related expenses",
  "operation": "sum",
  "time_range": {"start": "2023-01-01", "end": "question_date"},
  "status": "completed",
  "need_evidence": true
}
```

这里才使用当前问题。QuerySpec 选择实体、操作、时间范围和状态约束，不让 Answer 从整库
自由搜索。

### Step 7：检索、计算、回答

检索顺序：

```text
QuerySpec
  -> 结构化过滤
  -> 关系邻居扩展
  -> 原文 evidence 排序
  -> 程序计算 sum/count/date-difference
  -> Answer 读取少量原文 + 计算结果
```

Answer 只负责语言表达和无法程序化的偏好解释；金额求和、数量统计、日期差和排序应由程序
完成，并把参与计算的 source message 一起传入 Answer。

## 7. “一次找联系”应该放在哪里

不要把“一次找联系”实现成把全部历史压进一个超长 prompt。更好的做法是两级关联：

```text
窗口内：LLM 绑定事件内部的 subject/action/object/value/time
窗口间：程序候选过滤 + LLM 判断 same_entity/same_event/update
```

关系链接的候选键可以是：

- entity surface form 和别名；
- 事件动作和对象词；
- 数值/单位；
- session 日期和相对时间；
- 主题词和问答上下文。

这样既能找出“同一辆自行车的多次费用”，也不会把所有出现 `$` 的消息误合成一个账单。

## 8. 对不同题型的专门处理

### 8.1 金额/数量

保存原始值、单位、对象、动作、事件和状态。程序负责 sum/count/min/max；模型不直接算。

### 8.2 日期/时间/持续时间/频率

同时保存原文表达和角色：

```text
event_time | target_time | observed_time | valid_time | session_time | unknown
```

相对日期的解析需要 session_date 或 question_date 作为锚点，解析失败时保持 raw_text。

### 8.3 knowledge-update

维护：

```text
old value -> superseded_by -> new value
```

Answer 查询时根据问题时间和“当前/最近/当时”选择正确版本。

### 8.4 preference

偏好不是普通事实，至少要保存：主体、偏好对象、正负极性、强度、适用场景和来源消息。
例如“喜欢海景和 rooftop pool”与“这次想住 Miami”是不同类型的记录。

### 8.5 absent / insufficient evidence

30 条缺失信息样本说明记忆库需要支持否定结果：

```text
没有检索到支持该实体/事件/属性的证据
```

这不等于“数据库证明它不存在”。Answer 应明确说当前记忆中没有足够信息，不能用相似主题
替代答案。

## 9. 评估方式

原始数据提供了很强的离线诊断信号，但不能把它泄漏到运行时：

- `has_answer`：只用于 evidence recall 诊断；
- `answer_session_ids`：只用于 gold session recall 和 Oracle 上界；
- `answer`：只用于最终 judge；
- 运行时不能读取这些字段。

建议指标：

```text
gold message recall
gold answer-session recall
selected-message precision
invalid-source rate
numeric/time binding accuracy
update/version accuracy
answer input tokens
LongMemEval judge accuracy
```

必须做的消融：

1. 2048 vs 4096 窗口；
2. 仅原文 FTS vs 结构化字段 + FTS；
3. 无跨窗口 link vs 有跨窗口 link；
4. 无程序计算 vs 程序计算；
5. 固定 schema 问题 vs 自由生成问题。

## 10. 最终建议

### 不建议

```text
整窗口自由生成大量问题
  -> 选择消息
  -> 直接把消息全文堆给 Answer
```

这会比 flat fact 更可追溯，但仍然把实体消歧、更新判断、时间约束和聚合计算留给 Answer。

### 建议采用

```text
L0 原文消息库
  + L1 数字/时间/实体候选提及
  + L2 事件-对象-数值-时间绑定
  + L3 实体、偏好、更新和关系
  + L4 FTS/结构化检索投影
  + QuerySpec + 程序计算
  + 原文 evidence Answer
```

用户提出的 LLM-A/LLM-B 可以作为 L1/L2 的辅助实现，但必须满足：

1. LLM-A 使用固定 schema 问题，不自由发散；
2. LLM-B 只输出合法 message_id/span；
3. 记忆文本由程序从原文复制；
4. 跨窗口关系单独建表，不靠 Answer 临时联想；
5. 数值和日期运算由程序完成；
6. 每个记录都能反查到原始消息。

这条路线的目标不是保存更多内容，而是让 Answer 每次只看到与当前问题相关、可验证、带关系
和时间约束的少量原文证据。
