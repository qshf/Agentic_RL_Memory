# Memory Sidecar V5 Selective Sentence Memory 方案

## 1. 目标

放弃 V5-Demo 的“每个 turn 改写成 flat fact”方案，改为保存原文证据句：

```text
原始历史窗口（2048 / 4096 tokens）
    -> LLM-A：生成需要检查的事实问题
    -> LLM-B：从带编号的原文消息中选择相关消息序号
    -> 程序：只保存被选中的原文句子
    -> Answer LLM：根据已保存的原文记忆回答当前问题
```

核心原则：

1. LLM-A 负责扩大候选范围，不负责回答最终问题。
2. LLM-B 负责证据定位，只能输出当前窗口中存在的消息序号。
3. 程序不改写、不归一化、不推断、不计算，只保存原文。
4. Answer 只使用保存的原文记忆和当前问题。

## 2. 为什么替换 V5-Demo

旧方案让模型把每个 turn 改写成事实，再把全部 flat facts 交给 Answer。24 条 pilot 的准确率只有 8/24，主要问题是：

- 改写会丢失原始措辞、时间角色和上下文；
- 每个 turn 独立抽取，没有前后事实状态；
- Answer 必须在大量无结构 flat facts 中自行筛选；
- 事实数量随 turn 数线性膨胀；
- 输出 JSON 较长，容易被截断。

新方案把最重要的中间结果降级为“原文证据句”，减少语义改写和不可审计推理。

## 3. 处理单位与消息编号

### 3.1 历史窗口

沿用现有 chronological history，将消息按完整 `user -> assistant` turn 组织，再按 token budget 切成窗口：

- `context_tokens=2048`：低成本基线；
- `context_tokens=4096`：主实验；
- 一个超长 turn 不拆分，避免破坏问答关系；
- 每个窗口独立保留 session header 和原始 role。

### 3.2 稳定消息序号

窗口内为每条消息注入稳定的本地序号，同时保留全局 `unit_ordinal`：

```text
[m=0][global=120][user][session=7] I paid $160 for the bike.
[m=1][global=121][assistant][session=7] Got it.
```

LLM-B 只返回本窗口的 `m` 序号。程序根据窗口原文映射回完整消息，保存 `global=unit_ordinal`、role、session、原文和窗口编号。

## 4. LLM-A：问题生成器

### 4.1 输入

LLM-A 接收当前窗口的完整原文，不接收：

- 当前 LongMemEval question；
- reference answer；
- gold evidence；
- 其他窗口已保存的答案或判断。

这样可以避免问题驱动抽取只寻找当前答案相关信息。

### 4.2 输出

LLM-A 输出短问题列表，每个问题带类型和必要的证据范围：

```json
{
  "questions": [
    {"question_id":"q1", "kind":"amount", "question":"Which messages mention money paid or spent, and what was it for?"},
    {"question_id":"q2", "kind":"count", "question":"Which messages state a quantity or total count?"},
    {"question_id":"q3", "kind":"date_time", "question":"Which messages contain dates, times, relative dates, or schedules?"},
    {"question_id":"q4", "kind":"duration_frequency", "question":"Which messages mention duration, frequency, percentage, or a range?"},
    {"question_id":"q5", "kind":"ambiguity", "question":"Which messages contain an ambiguous event, status, update, or conflicting value?"}
  ]
}
```

问题生成器可以根据窗口内容返回 0 个或多个问题，但建议保留固定类型集合，便于跨数据集统计。问题不能包含答案，不能把数字改写为规范化值。

### 4.3 问题类型

- `amount`：金额、货币、花费、价格、预算；
- `count`：数量、计数、排名、总数；
- `date_time`：日期、时间、相对日期、目标时间、实际时间；
- `duration_frequency`：持续时间、百分比、范围、频率；
- `event_ambiguity`：计划/完成、旧值/新值、否定、多个事件、状态冲突；
- `other_salient`：窗口中明显重要但不属于以上类别的个人事实，限制数量。

## 5. LLM-B：证据消息选择器

### 5.1 输入

LLM-B 接收：

1. 当前窗口的带序号消息；
2. LLM-A 生成的问题列表；
3. 明确的选择规则。

### 5.2 输出协议

只输出序号，不输出改写内容：

```json
{
  "selected_message_ids": [0, 3, 8],
  "rejected_message_ids": [1],
  "selection_notes": []
}
```

生产实现建议只信任 `selected_message_ids`，其余字段仅作审计，避免模型通过 notes 偷渡新事实。更严格的最小协议为：

```json
{"selected_message_ids":[0,3,8]}
```

选择规则：

- 序号必须存在于当前窗口；
- 只能选择包含证据的原始消息；
- 优先选择 user 消息；
- assistant 消息只有在用户明确引用、确认或要求记住其具体信息时才允许保留；
- 不因为消息与当前问题相似就选择，选择依据只能是 LLM-A 的窗口问题；
- 无证据时返回空数组；
- 不输出句子、摘要、数字转换或答案。

程序对模型输出做严格校验：非法序号丢弃并记录 `invalid_message_id`，重复序号去重但保留原始响应，空选择是合法结果。

## 6. 程序保存格式

只保存原始消息，不保存模型改写的 fact：

```text
v5_selective_windows
  sample_id, window_ordinal, input_text, questions_json,
  selector_response, parse_status, input_hash, created_at

v5_selective_memory
  sample_id, memory_ordinal, window_ordinal, global_unit_ordinal,
  role, session_index, source_text, source_hash,
  selected_for_json, validation_status, created_at
```

建议唯一约束：

```text
sample_id + window_ordinal + input_hash + extractor_version
sample_id + global_unit_ordinal + source_hash
```

同一原文消息跨窗口重复出现时不改写，程序可以按 `source_hash` 去重；实验中同时记录 `duplicate_observation`，方便比较“保留重复”和“程序去重”两种 Answer 输入。

## 7. 跨窗口与历史记忆

LLM-A 和 LLM-B 每次只处理当前 2048/4096 token 窗口，不把以前抽取的摘要传给下一窗口。跨窗口记忆由程序保存的原文句子承担：

```text
window 1 -> selected source sentences -> memory store
window 2 -> selected source sentences -> memory store
window 3 -> selected source sentences -> memory store
```

这保留了跨窗口证据，但避免了“抽取结果再次被模型改写”。如果需要减少选择漂移，可以给 LLM-A 一个只含已保存原文句子的 `existing_memory_index`，但第一版不启用，避免引入二次筛选偏差。

## 8. Answer 阶段

Answer 输入：

1. 当前问题；
2. question date；
3. 所有已保存的原文证据句，按历史顺序排列；
4. 每句的 session/date/role/source ordinal 元数据。

Answer 规则：

- 只能依据保存的原文句子；
- 保留原始金额、数量、日期、时间、单位和状态；
- 需要求和、计数、日期差时，先列出使用的证据，再计算；
- 无充分证据时明确说明无法确定；
- 不把 assistant 建议当成用户事实；
- 不引用没有保存的历史消息。

如果保存句子超过 Answer context budget，程序先按以下顺序裁剪：

1. 同一 source hash 去重；
2. 保留 user 消息；
3. 保留包含数字/时间/状态词的消息；
4. 按历史顺序保留，绝不截断单条消息。

第一版不增加第三个 LLM 做记忆压缩，避免再次引入事实改写。

## 9. 失败与审计

每个窗口分别记录：

- question generator raw response；
- selector raw response；
- 两阶段 parse status；
- 非法序号及重复序号；
- 被选原文的 hash；
- 输入 token、输出 token、延迟和重试；
- 最终 Answer 输入中的 source ordinal 列表。

单个窗口失败不阻塞整个样本：

- LLM-A 失败：该窗口没有候选问题，记录 `question_parse_error`；
- LLM-B 失败：该窗口没有新增记忆，记录 `selector_parse_error`；
- Answer context 超限：按程序裁剪后重试一次，仍超限则标记 `not_runnable`。

## 10. 评估设计

### Phase 0：协议测试

使用合成窗口覆盖：金额绑定、多个金额、数字词、相对日期、时间角色、计划/完成冲突、assistant 污染、非法序号和重复选择。

### Phase 1：4 条真实 smoke

比较三种设置：

1. `2048 + selector`；
2. `4096 + selector`；
3. 旧 V5-Demo flat facts baseline。

检查：保存句子是否为原文连续文本、选择序号是否可反查、重放是否一致、Answer 是否能看到跨窗口证据。

### Phase 2：24 条 pilot

记录：

- LongMemEval judge accuracy；
- 每题保存消息数；
- source sentence precision；
- 数字/日期/时间问题覆盖率；
- selector 空选择率和非法序号率；
- Answer 输入 token、总调用数和耗时；
- 2048 与 4096 的准确率差异。

进入下一版本的建议门槛：

- 24 条准确率明显高于旧 V5-Demo 的 33.3%；
- 非法序号率接近 0；
- 保存文本 100% 可从源历史反查；
- Answer context 不因记忆膨胀而频繁超限；
- 时间和数字类问题不再全部依赖 Answer 自己扫描完整历史。

## 11. 推荐实施顺序

1. 先实现稳定消息编号和原文 memory store；
2. 实现 LLM-A 问题生成器及 JSON parser；
3. 实现 LLM-B 序号选择器及边界校验；
4. 实现 Answer prompt 和 context budget；
5. 加入 replay、幂等和原文 hash 校验；
6. 先跑 4 条 smoke，再跑 24 条 pilot；
7. 与旧 V5-Demo、V3 结果并列比较，不直接替换基线。

## 12. 关键风险

### 风险 A：LLM-A 问题过于宽泛

固定问题类型、限制每窗口问题数量，并要求问题只描述“要定位什么证据”，不要求回答。

### 风险 B：LLM-B 选择过多消息

要求最小充分证据集，设置每个问题的最大消息数；程序按原文 hash 去重。

### 风险 C：只保存局部句子导致跨句信息丢失

第一版保存完整消息而非句法切片。后续若单条消息过长，再增加连续句子范围字段，但仍由原文复制。

### 风险 D：Answer 仍无法做跨消息计算

在 Answer prompt 中要求先列证据再计算；如果仍不稳定，再单独增加程序计算层，不回到 fact 改写。

### 风险 E：问题生成器与当前题目泄漏

运行时完全不传 question、reference answer、gold evidence；评估脚本与抽取脚本严格分离。

## 13. 最终判断标准

这个方案成功，不是因为保存了更多句子，而是因为满足以下闭环：

```text
窗口中的重要数字/时间/状态
    -> 被问题生成器提出检查项
    -> 被选择器定位到真实消息序号
    -> 以原文句子保存
    -> Answer 能据此给出正确答案
```

如果 24 条 pilot 仍低于 V3，下一步优先检查“问题覆盖”和“消息选择”两个中间指标，而不是继续增加 Answer prompt 的复杂度。
