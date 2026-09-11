# Memory Sidecar V5 原始事实抽取方案

## 1. 当前决策

V4 的完整图谱方案在 120 条实验中低于 V3，不能继续把“图谱存储、数值推理、Answer 选择”
放在同一个迭代里。V5 先拆出最小的中间层，只验证一件事：

> 模型能否从 user evidence 中稳定找出金额、数量、日期、时间等原始表达，并保留可追溯证据。

V5 是 shadow extraction：

```text
历史 chunk（chunk=2048）
        ├── V3 Memory Manager（现有主链路，不变）
        └── V5 Raw Fact Extractor（旁路，只落库）

Answer 仍只使用 V3 memory + 原有 raw tail
```

V5 的抽取结果本轮不参与 V3 路由、不参与去重、不参与 Answer 上下文、不参与最终答案。
完整的规范化、typed ledger、occurrence 去重、QuerySpec、程序聚合和专用 Answer 上下文
统一延期到 V6。

## 2. 为什么先做这一层

V3 的数字类失败常见于金额/数量漏记、时间表达遗漏和跨 session 事实不完整；V4 虽然把
部分事实写进图谱，却无法证明模型抽取本身稳定，且 `graph-all` 又引入了 Answer 选择噪声。

如果现在直接实现 V6，最终准确率下降时无法判断原因是：

```text
原文没有抽到 -> 字段规范化失败 -> 去重错误 -> 查询筛选错误 -> Answer 选择错误
```

V5 先把第一段单独测量，失败只影响旁路审计，不影响 V3 基线。

## 3. 范围

### 3.1 抽取对象

只保留原文中明确出现的表达：

- 金额和货币：`$25`、`120 USD`、`€40`；
- 数量和计数：`3 books`、`three courses`、`38 coins`；
- 日期：`May 20, 2023`、`2023-05-20`、`last Saturday`；
- 时间：`7:30 AM`、`18:30`、`around noon`；
- 持续时间、百分比、范围、频率：`5 days`、`20%`、`7-10 days`、`twice a week`；
- 其他可能有事实意义的数字表达，标记为 `other_numeric`，不在 V5 中解释。

### 3.2 明确不做

- 不把原始值转换成整数、日期、分钟或标准货币；
- 不判断事实是 snapshot、occurrence、target 还是 completed；
- 不做 occurrence/snapshot 去重和更新；
- 不进行 count/sum/min/max/date difference；
- 不根据问题、答案或 gold evidence 反向决定抽取内容；
- 不修改现有 V3 Answer prompt 和 Answer 输入；
- 不因为抽取失败而 reject 整个 chunk。

## 4. 输入与调用边界

### 4.1 处理单位

处理单位是与 V3 相同的 2048 token chunk。每个 chunk 都执行一次独立的 V5 抽取，避免只
对“看起来像数字题”的问题做选择性实验。抽取器不接收当前问题、reference answer 或
数据集 evidence 标注。

V3 和 V5 使用相同的 user/assistant 历史，但 V5 只允许引用 role 为 `user` 的 evidence。
assistant 中出现的数字可以保留在原始输入审计中，但不得成为 V5 fact。

### 4.2 模型提示词原则

提示词只要求“复制原文表达”，不要求模型推理：

1. 找出所有可能表示金额、数量、日期、时间、持续时间、百分比、范围或频率的原文片段；
2. 每条片段必须引用当前 chunk 的本地 evidence ID；
3. `raw_text` 必须逐字出现在对应 user message 中；
4. 不补全缺失年份、时区、货币、单位和事件次数；
5. 普通编号、产品型号和代码可以标记 `other_numeric`，不要擅自解释。

## 5. 最小模型协议

模型只输出原始事实和证据，所有字段都尽量短：

```json
{
  "facts": [
    {
      "raw_text": "$25",
      "kind_hint": "amount",
      "context_text": "I paid $25 for the chain",
      "evidence_ids": [2]
    }
  ]
}
```

字段约束：

| 字段 | 必填 | 约束 |
|---|---:|---|
| `raw_text` | 是 | 原文连续片段，不得改写 |
| `evidence_ids` | 是 | 当前 chunk 的本地整数 ID，至少一个 |
| `kind_hint` | 否 | `amount/count/date/time/duration/percentage/range/frequency/other_numeric` |
| `context_text` | 否 | 对应 user evidence 的连续子串，用于审计 |

V5 不要求 `subject`、`predicate`、`object`、`claim_text`、`provider`、`status`、`normalized_value`
或 `occurrence_key`。这些字段若在后续 V6 需要，由程序或另一个专门阶段处理。

## 6. 程序校验

程序只做边界校验和审计，不做语义推断：

1. JSON 结构无效：记录 `parse_error`，保留原始 response；
2. evidence ID 不存在、不是 user 或 `raw_text` 不是严格子串：该 fact 标记
   `invalid_evidence`，不静默修正；
3. `kind_hint` 不在受控集合：改为 `unknown_hint`，仍保留 raw fact；
4. `context_text` 不匹配时只丢弃 context 字段，不丢弃 `raw_text`；
5. 同一 chunk 内重复输出不去重，只记录 `duplicate_observation=true`；
6. assistant/question 中的数字不进入有效 facts，但原始响应和拒绝原因必须落库。

V5 的“抽取成功”只表示原文片段和 user evidence 可验证，不表示它已经可以用于计算或回答。

## 7. SQLite 追溯

沿用现有 `samples`、`calls` 和 `sidecar_batches` 记录调用、token、延迟和失败重试，新增
以下 V5 专用表：

```text
v5_raw_routes
  sample_id, batch_ordinal, route, matched_signal, router_version, input_hash

v5_raw_batches
  sample_id, batch_ordinal, input_text, evidence_json, raw_response,
  parse_status, parser_version, input_hash, created_at

v5_raw_facts
  sample_id, batch_id, fact_ordinal, raw_text, kind_hint, context_text,
  evidence_ids_json, evidence_role_status, substring_status,
  duplicate_observation, validation_status, created_at
```

写入要求：

- `sample_id + batch_ordinal + input_hash + extractor_version` 幂等；
- 原始 response、解析失败和校验失败不可覆盖；
- 相同 batch 重放必须得到相同的 raw facts 和校验状态；
- 并发 2 时使用现有 SQLite WAL、busy timeout 和文件锁；
- 最终可以从 `sample -> batch -> raw fact -> evidence` 完整反查。

V5 的结果只写入上述审计表，不写入 V3 `sidecar_memory`，不改变 V3 的 memory state。

## 8. 记录格式示例

同一段原文可以产生多条独立 raw fact：

```text
User evidence [2]:
I paid $160 for the bike and $25 for the chain on Saturday.
```

```json
{
  "facts": [
    {"raw_text":"$160", "kind_hint":"amount", "context_text":"I paid $160 for the bike", "evidence_ids":[2]},
    {"raw_text":"$25", "kind_hint":"amount", "context_text":"$25 for the chain", "evidence_ids":[2]},
    {"raw_text":"Saturday", "kind_hint":"date", "context_text":"on Saturday", "evidence_ids":[2]}
  ]
}
```

V5 不判断两个金额是否属于同一次购买，也不把两个金额相加；它只证明原始表达是否被捕获。

## 9. 泛化性约束

为避免针对错误样本写特例：

1. 规则只按 `kind_hint`、evidence role 和 substring 校验组织，不按 question ID 或实体名
   积累规则；
2. 不把问题文本传给 extractor，防止问题引导抽取范围；
3. 不读取 reference answer、gold evidence 或 DeepSeek judgment；
4. 每条新提示词规则必须添加跨数据集正例和反例；
5. 无法判断类型时保留 `unknown_hint`，不能强制分类；
6. 不能因为数字看起来像年份或金额就修改原文；
7. 评估使用未参与提示词调试的 holdout 样本。

## 10. 评估设计

### 10.1 标注集

先建立一个只用于离线评估的原始 span 标注集，不参与运行时：

| 阶段 | 样本 | 目的 |
|---|---:|---|
| Smoke | 4 条 | 检查协议、落库、重放和失败记录 |
| Pilot | 24 条 | 每类覆盖金额、数量、日期、时间和歧义表达 |
| Holdout | 120 条分层抽样 | 验证跨数据集泛化，不调规则 |

每条标注只标出“原文中明确出现的事实表达”和其 user message，不标注规范化值和答案。

### 10.2 指标

- `span_recall`：标注片段被 raw fact 覆盖的比例；
- `evidence_precision`：raw fact 是否引用正确的 user message；
- `kind_hint_accuracy`：可判断类型的分类准确率；
- `invalid_evidence_rate`：引用 assistant 或不存在 evidence 的比例；
- `false_positive_rate`：把普通编号、代码当成事实的比例；
- `duplicate_observation_rate`：重复输出比例，仅作诊断，不在 V5 去重；
- 输入/输出 token、每 chunk 延迟、失败重试和 SQLite 写入冲突。

本阶段不使用最终 Answer accuracy 作为 V5 成功指标，因为 V5 尚未接入 Answer；可以同时
记录 V3 Answer 结果作为“不受影响”的回归证明。

### 10.3 进入 V6 的门槛

- 4 条 smoke 的 facts、校验状态和重放结果全部可解释；
- 24 条 pilot 的 `span_recall` 达到预设目标（建议不低于 90%），且
  `invalid_evidence_rate` 接近 0；
- holdout 上没有依赖单个数据集实体的规则；
- V3 Answer 结果与不启用 V5 的对照一致；
- 失败可以明确归因于“模型漏抽”“证据非法”或“类型提示错误”。

达不到门槛时只修改抽取协议、提示词或校验，不提前实现 V6 的规范化和查询逻辑。

## 11. 实施阶段

### Phase 0：离线协议和标注

1. 固定 chunk=2048、模型参数和 extractor prompt 版本；
2. 实现 JSON parser、evidence/substring validator 和 SQLite schema；
3. 建立 24 条 pilot 的原始 span 标注；
4. 使用合成案例覆盖金额、数字词、日期、时间、assistant 污染和重复输出。

### Phase 1：4 条 shadow smoke

1. V3 正常构建 memory；
2. V5 对同一批 chunk 旁路抽取；
3. 并发 2 写入 SQLite；
4. 删除进程内缓存后重放，比较 raw facts 和 validation status；
5. 不调用 V5 context，不改变最终答案。

### Phase 2：24 条 pilot

1. 运行全量 chunk 的 V5 shadow extraction；
2. 统计 span recall、evidence precision、类型提示和 false positive；
3. 逐条查看抽取轨迹，修复只能泛化的协议问题；
4. 冻结 extractor version，保留 V3 作为 answer regression。

### Phase 3：120 条 holdout

仅在 Phase 2 达标后执行。该轮不再根据单题结果改 prompt，输出跨数据集泛化报告，为 V6
typed ledger 设计提供真实输入分布。

## 12. V6 的边界

V6 才实现以下能力：

```text
raw fact -> 程序规范化 -> typed ledger -> occurrence/snapshot 去重
          -> QuerySpec -> 程序计算 -> 专用 Answer context
```

V6 必须复用 V5 的原始 evidence 和 SQLite 轨迹，不能重新从答案或 gold evidence 构造事实。
如果 V5 的 span recall 不足，V6 不得通过常识补全来掩盖抽取缺陷。

## 13. 最终判断

V5 的成功标准不是最终准确率立即提升，而是建立一个可靠的、与 V3 解耦的事实采集层：

- 原文片段抽得全；
- 证据角色可验证；
- 失败和重复可追溯；
- 不影响 V3 综合效果；
- 能为 V6 的规范化、去重和查询提供真实、可回放的输入。

只有这一层稳定后，才有必要讨论金额求和、数量统计、日期排序和时间推理。
