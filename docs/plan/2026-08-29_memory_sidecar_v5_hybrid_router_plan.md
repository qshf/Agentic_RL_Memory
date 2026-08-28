# Memory Sidecar V5 混合路由实验计划

## 1. 核心想法

V4 图谱适合保存和推理金额、数量、日期、时间、provider 等结构化事实；V3 的 key/value 路由对普通偏好、计划、描述性事实更轻量。V5 验证一个混合方案：

```text
有数字/日期/金额/时间/数量的 evidence unit -> V4 图谱 Manager
没有上述信号的 evidence unit -> V3 Manager
V4 memory + V3 memory + raw tail -> 原 V1 Answer prompt
```

第一轮不改变 Answer 提示词，不引入新的摘要模型，不使用 embedding 或外部图数据库。V5 的目标是减少 V4 的无关 raw claim 和上下文噪声，同时保留数字事实的确定性合并和聚合能力。

## 2. 实施顺序

### Phase 0：完成 V4 修复并重新测评

先不做 V5。完成以下 V4 修复后，重新运行 V4：

1. occurrence 去重时合并后续 claim 补充的非空金额、数量、provider、location、time 和 scope；冲突值进入审计字段，不静默覆盖。
2. 扩充真实模型输出中出现的 predicate alias，例如 `has_completed`、`plans_to`、`is_attending`。
3. unknown relation 的 graph projection 使用结构化 `subject -> relation -> object`，而不是只输出原始句子。
4. graph-all 的重复 edge 和 raw claim 稳定合并，并记录合并前后数量。
5. 保持 `chunk=2048`，Answer prompt 和 raw tail 预算不变。

V4 结果必须同时保存：Manager 原始响应、normalized claim、route result、edge 状态、projection 正文、Answer 响应和 DeepSeek judgment。

### Phase 1：V4 24 条 pilot

使用冻结的 24 条分层样本，覆盖：

- 金额/数量求和；
- provider/entity 区分；
- 日期和时间推理；
- target/observation；
- 重复 occurrence；
- knowledge-update；
- 普通偏好和计划。

输出 V4 基线指标：

```text
Answer accuracy
numeric/temporal accuracy
normalized claim coverage
unknown relation rate
quarantine rate
deduplicated_merged rate
graph edge/raw claim count
Answer input tokens
Manager input/output tokens
latency and failed calls
```

发现的问题先在 V4 中修复，再冻结 V4 代码和数据库 schema。修复后需要重新跑受影响样本，不能直接把修复前后的结果混在一个统计中。

### Phase 2：V4 扩大数据集

V4 pilot 稳定后扩大到 120 条确认集。120 条只用于估计总体趋势，不再改变 schema 或路由规则。所有 V5 样本必须使用同一份 120 条 manifest，并保留 V4 的逐样本结果作为 paired baseline。

## 3. V5 路由规则

### 3.1 检测粒度

检测单位使用完整的 `user -> assistant` turn；一个 turn 中任一消息命中数字特征，就把整个 turn 送入 V4，避免拆开问答导致 evidence 丢失。没有命中的完整 turn 送入 V3。

不要按整个样本或“只要一个 chunk 含数字就全部走 V4”路由，否则普通聊天会污染图谱。

### 3.2 正则类别

第一版只检测明确的结构化信号：

```text
金额：$25、25 USD、€40
整数/小数：12、3.5
日期：2023-05-20、May 20, 2023
时间：7:15、8:30 AM
数量单位：12 courses、5 nights、347 miles
百分比/范围：20%、7-10 days
```

纯序号、代码名或普通年份可能是噪声。检测命中只决定 Manager 路由，不直接断言该数字是事实；事实仍必须由 Manager 提取并引用 evidence。

### 3.3 两套 memory 的合并

V5 保留两个独立状态：

```text
v4_graph_state：数字/时间相关 claim
v3_memory_state：非数字 claim
```

最终 Answer projection 按以下顺序合并：

1. V4 active/completed/observed edges；
2. V3 active records；
3. raw tail。

相同规范化事实只保留一份；V4 的金额、数量、日期字段优先作为 typed representation。不同来源或不同时间的 occurrence 不合并。所有投影必须记录来源 state、输入 edge/record IDs 和去重数量。

## 4. 对照实验

V5 至少包含三个 paired arm：

| 组 | Manager 路由 | Answer 上下文 |
| --- | --- | --- |
| V3 | 全部 turn 走 V3 | V3 memory + raw tail |
| V4 | 全部 turn 走 V4 | V4 graph-all + raw tail |
| V5 | 数字 turn 走 V4，其余走 V3 | merged V4 + V3 + raw tail |

三个组必须共用：同一 manifest、同一 `chunk=2048`、同一模型参数、同一 raw tail、同一 Answer prompt、同一 DeepSeek judge。V5 不能同时修改 Answer 提示词，否则无法归因。

## 5. 重点验证假设

### H1：数字事实准确率提升

V5 在金额、数量、日期、时间题上不低于 V4，并减少重复事件导致的字段丢失。

### H2：非数字事实不被图谱噪声污染

V5 的普通偏好、计划、描述性事实不低于 V3，且 graph raw claim 数明显低于 V4。

### H3：上下文成本下降

V5 的 Answer 输入 token、graph raw claim 数和 Manager 总输入 token 低于 V4；如果准确率不变但成本下降，也记录为有效结果。

### H4：路由错误可审计

每个 turn 记录 `route_reason`、命中的 regex 类别、最终 Manager、输入 hash 和输出状态。数字误判、无数字但实际应结构化的事实必须能从轨迹中定位。

## 6. V5 必备数据库记录

新增或复用以下审计信息：

```text
route_decisions
  sample_id, turn_ordinal, route, matched_patterns, input_hash

v4 batches/claims/edges
v3 batches/events/memory states

merged projections
  v3_record_ids, v4_edge_ids, duplicate_count, content, token_count

answer call
deepseek judgment
```

V5 不复制 raw tail 正文，只保存其 hash、token 数和来源 ordinal；raw tail 继续从 V1 基线数据库复现。

## 7. 评估门槛

24 条 pilot 用于发现实现问题，不能宣称泛化。120 条确认集使用逐样本 paired comparison：

- V5 相对 V4：总体准确率、数字/时间子集不得下降；
- V5 相对 V3：非数字子集不得下降；
- 报告 accuracy、95% paired bootstrap interval 或 McNemar 检验；
- 同时报告 token、调用次数、失败率和延迟；
- 任意一组出现大量 route decision 丢失、projection 截断或 raw tail 丢失时，先修复轨迹协议，再比较答案。

## 8. 当前执行顺序

```text
V4 occurrence/alias 修复
  -> V4 4 条 smoke 重跑
  -> V4 24 条 pilot
  -> 根据轨迹修复 V4
  -> V4 120 条确认集
  -> 冻结 V4 baseline
  -> 实现 V5 regex route + 双 memory merge
  -> V3/V4/V5 paired experiment
  -> DeepSeek judge 与按题型分析
```

当前两个 V4 在线样本只作为实现 smoke，不作为最终 V4 准确率结论。修复后的 V4 需要重新运行受影响样本，之后再进入 24/120 条正式测评。
