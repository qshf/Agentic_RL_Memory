# Memory Sidecar V5 问题驱动证据选择方案

## 1. 方案结论

放弃当前 V5-Demo 的“每个 turn 改写成 flat fact”方案，改成两阶段的原文证据选择：

```text
原始历史
  -> 按 2048/4096 token 窗口切分
  -> LLM-A：从窗口生成数字、时间和事件歧义问题
  -> LLM-B：根据问题选择需要的原文消息序号
  -> 程序复制被选中的原文句子
  -> 跨窗口合并、去重、按原始顺序保存
  -> Answer：只使用已保存的原文记忆回答当前问题
```

核心原则：

1. LLM 不负责改写事实，记忆中的文本全部来自原始消息的连续句子。
2. LLM-A 只根据历史窗口生成“应当保留什么”的检索问题，不接收当前问题、答案或 gold evidence。
3. LLM-B 只输出消息序号，不输出事实、不输出摘要、不输出数值推理。
4. 程序负责序号边界校验、原文复制、去重、排序和审计。
5. Answer 才根据用户当前问题从已保存的原文记忆中作答。

这不是传统的 query-aware retrieval：问题生成阶段不使用当前问题；它是面向长期记忆字段的
schema-aware evidence selection。

## 2. 为什么替换当前 V5-Demo

当前 demo 存在三个结构性问题：

- 每个 turn 生成一段新的事实文本，容易发生遗漏、幻觉和数值改写；
- 所有 flat facts 最后一次性塞给 Answer，相关性筛选和跨 session 合并全部交给 Answer；
- 512 token 的事实输出经常截断，解析失败后整个 turn 的事实丢失。

新方案只保存输入中的原文句子，因此可以把错误分成“问题生成漏检”“消息选择错误”“原文
记忆不足”“Answer 推理错误”，而不是把抽取改写和回答混在一起。

## 3. 处理单位和窗口

### 3.1 全局消息编号

先将 LongMemEval session 按日期排序，构建一个连续历史流。每条消息分配不可变的全局编号：

```text
message_id: 0, 1, 2, ...
role: user | assistant
session_index
session_date
content
```

窗口内另有从 0 开始的 `local_id`，仅用于降低模型输出难度。程序维护：

```text
local_id -> global_message_id -> 原始消息全文
```

LLM-B 只能输出当前窗口中存在的 `local_id`，不能输出自由文本作为证据。

### 3.2 窗口大小

首轮同时测试两个配置：

| 配置 | 用途 | 取舍 |
|---|---|---|
| 2048 tokens | 与旧 V3/V5 实验保持可比 | 调用更多，局部关系较少 |
| 4096 tokens | 主候选配置 | 调用更少，跨 turn 绑定更完整 |

窗口必须在完整 turn 边界切分；单个超长 turn 允许独立成为窗口。相邻窗口可保留一个完整
turn 的 overlap，但 overlap 只用于提升跨边界召回，最终按原文 hash 去重。

每个窗口输入：

```text
[local_id=0][global_id=381][session=12][role=user] ...
...
[local_id=17][global_id=398][session=12][role=assistant] ...
```

不输入当前 question、reference answer、answer_session_ids 或 evidence 标注。

## 4. LLM-A：问题生成器

### 4.1 目标

LLM-A 不抽取答案，只提出窗口中值得保留的长期记忆问题。问题必须覆盖以下类别：

- 金额和货币；
- 数量和计数；
- 日期和时间点；
- 持续时间；
- 频率和周期；
- 多个数字之间的绑定关系；
- 事件边界、状态更新和歧义。

问题应当能由当前窗口中的一条或多条消息回答。没有相关内容时返回空数组。

### 4.2 输出协议

```json
{
  "questions": [
    {
      "question_id": "w03-q01",
      "question_text": "What amounts, objects, and action are explicitly mentioned in this window?",
      "category": "amount",
      "priority": "high"
    }
  ]
}
```

允许的 `category`：

```text
amount | quantity | date | time | duration | frequency |
binding | status_update | event_ambiguity
```

约束：

- `question_text` 不得包含当前 benchmark question 的措辞；
- 不要求模型给答案，不允许输出事实值、规范化值或证据序号；
- 每个窗口最多 8 个问题，优先保留能覆盖多个消息的高价值问题；
- 普通年份、产品型号、编号和代码只有在上下文明显具有事实意义时才生成问题；
- 对歧义表达生成澄清问题，例如“哪些金额属于同一次购买？”、“这是计划时间还是实际时间？”；
- 问题生成失败时记录原始响应，当前窗口仍可继续，不拒绝整个样本。

推荐系统提示：

```text
You generate retrieval questions for durable memory from one conversation window.
Do not answer the questions. Do not use the current user question. Generate only
questions about explicit amounts, quantities, dates, times, durations, frequencies,
bindings, status changes, and ambiguous event boundaries. Return JSON only.
```

## 5. LLM-B：消息选择器

### 5.1 输入

LLM-B 接收同一个窗口和 LLM-A 生成的问题列表。它不接收 reference answer，也不接收任何
已经选择的事实文本。问题列表可以按窗口一次性传入，避免每个问题单独调用模型。

### 5.2 输出协议

```json
{
  "selections": [
    {
      "question_id": "w03-q01",
      "message_ids": [2, 3],
      "reason": "same purchase and its two amounts"
    },
    {
      "question_id": "w03-q02",
      "message_ids": [7]
    }
  ]
}
```

实际记忆只使用 `message_ids` 对应的原文；`reason` 仅用于审计，可不进入 Answer context。

选择器约束：

1. 只能输出当前窗口的 `local_id`；
2. 空选择合法，表示问题在窗口中没有足够证据；
3. 选择完整消息，不允许输出模型改写的句子；
4. 可以选择 user 或 assistant 消息，因为 LongMemEval 中有些偏好和推荐只出现在 assistant；
5. 选中的消息必须与问题有直接证据关系，不能因为包含普通数字就选择；
6. 对跨消息事件选择全部必要消息，不要只选择含金额的孤立消息；
7. 不能选择窗口外序号，不能输出负数、重复序号或不存在的 question_id。

程序校验：

```text
invalid_question_id
invalid_message_id
duplicate_message_id
empty_selection
```

校验失败的条目保留 raw response，但不复制非法内容到记忆。

## 6. 原文记忆结构

新增实验专用表，沿用现有 `runs`、`samples`、`calls`：

```text
v5_selective_windows
  sample_id, window_ordinal, token_budget, input_text,
  message_map_json, input_hash, created_at

v5_selective_questions
  window_id, question_ordinal, question_id, question_text,
  category, priority, raw_response, parse_status, created_at

v5_selective_selections
  window_id, question_id, selected_local_ids_json,
  selected_global_ids_json, validation_status, raw_response, created_at

v5_selective_memory
  sample_id, memory_ordinal, global_message_id, session_index,
  role, source_text, source_hash, source_window_ordinals_json,
  selection_question_ids_json, created_at
```

`v5_selective_memory.source_text` 必须由程序从原始消息复制，禁止使用 LLM-B 的自由文本。

唯一约束和幂等键：

```text
sample_id + window_ordinal + input_hash + pipeline_version
sample_id + global_message_id + source_hash
```

同一原文消息被多个问题或 overlap 窗口选中时，只在最终 memory 中保留一份，并记录所有来源
窗口和问题 ID。

## 7. 跨窗口合并和历史记忆

这个方案有“历史记忆”，但形式是**已选择的原文句子**，不是上一轮模型生成的事实摘要：

```text
窗口 1 -> 选择原句 A/B
窗口 2 -> 选择原句 C/D
窗口 3 -> 选择原句 B/E
最终 memory -> A/B/C/D/E，按原始 message_id 排序
```

LLM-A 和 LLM-B 默认只看当前窗口，不把之前窗口的选择结果作为输入。这能保持窗口结果
可审计、可重放，也避免早期错误摘要污染后续抽取。跨窗口的重复和更新由程序按原文 hash
去重，但不做语义合并；同一对象的历史值和新值都保留。

如果后续实验显示跨窗口绑定召回不足，再增加一个可选的 `memory_reviewer` 阶段：只接收已
选原句，输出需要补看的 message_id，仍然只保存原文，不允许生成事实。

## 8. Answer 阶段

Answer 输入为：

```text
# Selected original memory
[global_id=381][session=12][role=user] 原文句子
[global_id=382][session=12][role=assistant] 原文句子
...

# Current date
...

# Question
...
```

Answer 必须遵守：

- 只能使用 selected original memory；
- 保留原文中的金额、数量、日期、时间和单位；
- 需要计算时先列出直接证据，再进行简单计算；
- 信息不足时明确说无法确定，不得用常识补全；
- 不输出 message_id、窗口编号或内部审计字段。

为避免上下文再次溢出，Answer 采用两档输入：

1. `selected_memory_full`：全部选中原句；
2. 若超过模型上限，按原始顺序保留并裁剪低优先级窗口，优先级由命中问题类别和选择次数
   计算，不能按当前 question 做额外隐式筛选。

## 9. 调用和成本控制

每个窗口默认两次调用：一次问题生成、一次批量选择。相比当前 demo 的每个 turn 一次调用，
调用量应从约 250 次/样本降到约 60–130 次/样本，具体取决于 2048/4096 窗口和 overlap。

建议参数：

| 参数 | Smoke | Pilot/Holdout |
|---|---:|---:|
| window tokens | 2048, 4096 | 固定单一候选后再扩展 |
| questions/window | <= 8 | <= 8 |
| selector output tokens | 256 | 384 |
| answer output tokens | 512 | 1024 |
| concurrency | 2 | 2–4 |

选择器只输出序号，因此输出 token 应远低于 flat fact 摘要；问题生成也应限制问题数量，避免
把大量问题本身变成新的上下文噪声。

## 10. 评估方案

### Phase 0：协议测试

使用合成窗口覆盖：

- 两个金额对应两个对象；
- 数量词和数字词；
- 绝对日期、相对日期、时间点和持续时间；
- 频率表达；
- 计划值与实际值；
- user/assistant 数字污染；
- 跨两条消息的同一事件；
- 非事实编号、型号和代码。

必须验证：非法序号被拒绝、保存文本是原文子串、重复选择去重、重放结果一致。

### Phase 1：4 条 smoke

对每条样本运行 2048 和 4096 两档，比较：

- selected message precision；
- 关键证据 recall；
- 非相关消息比例；
- 选择器非法序号率；
- Answer 输入 token；
- 最终答案是否受证据选择影响。

### Phase 2：24 条 pilot

冻结问题生成 prompt 和选择器协议后，运行 24 条真实样本。至少报告：

- `question_generation_parse_rate`；
- `selection_valid_rate`；
- `selected_message_count`；
- `memory_token_count`；
- 数字/时间证据覆盖率；
- LongMemEval judge accuracy；
- 相比当前 V5-Demo 的调用量、耗时和准确率。

### 进入下一阶段的建议门槛

- 问题生成和选择 JSON 成功率 >= 99%；
- 非法 message id 接近 0；
- selected memory 中原文校验 100% 通过；
- 关键数字/时间 evidence recall >= 90%；
- 24 条 judge accuracy 明显高于当前 V5-Demo 的 33.33%；
- Answer context 不因记忆膨胀而超过模型上限；
- 任意单个窗口失败不会丢失其他窗口已保存的原文记忆。

## 11. 实施顺序

1. 新增 `memory_sidecar/v5_selective.py`，实现窗口构建、问题解析、选择解析和原文校验。
2. 在 `utils/store.py` 增加四张 V5 selective 表和幂等写入接口。
3. 新增 `scripts/run_v5_selective.py`，支持 `--window-tokens 2048|4096`、断点续跑和并发。
4. 新增离线协议测试，禁止测试依赖真实 API。
5. 运行 4 条 smoke 的双配置重放。
6. 选择 2048 或 4096 的主配置后运行 24 条 pilot。
7. 使用现有 LongMemEval DeepSeek judge 统一评估，不直接用字符串匹配代替正式指标。
8. 只有在 evidence recall 和答案准确率达标后，才考虑增加跨窗口 reviewer 或语义去重。

## 12. 明确不做的事情

- 不让 LLM 直接写入 fact_text、normalized_value 或金额计算结果；
- 不把当前 question 传给 LLM-A，避免只抽取答案相关内容；
- 不使用 reference answer、gold evidence 或 judge 结果参与运行时选择；
- 不在 V5 实现完整 typed ledger、occurrence/snapshot 去重或 QuerySpec；
- 不把选择器的 reason 当作最终记忆；
- 不在原文之外拼接未经验证的新句子。

## 13. 预期结果

这条路线的成功标准不是“抽取出更多事实”，而是：

```text
给定一个问题，Answer 能在较小的原文记忆中看到正确证据，
且每条记忆都能反查到原始消息，不依赖模型改写。
```

如果 24 条 pilot 仍然低于 V3，失败原因将更容易定位到：问题覆盖不足、消息选择过宽/过窄、
窗口边界丢失，还是 Answer 本身推理错误。
