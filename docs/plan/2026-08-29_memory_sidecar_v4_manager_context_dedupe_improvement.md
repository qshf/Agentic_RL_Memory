# V4 Manager 分层上下文与 Occurrence 去重改进方案

## 1. 目的与边界

本修订只处理 V4 的两个 Manager 侧问题：

1. 在不把完整图谱重新塞回 prompt 的前提下，为 Manager 提供有用、稳定的历史参考；
2. occurrence 是否重复完全由程序判定，模型不承担去重、合并或生成 occurrence identity 的职责。

`chunk=2048`、Manager claim 协议、Answer 提示词、Answer context、raw tail、query projection 和 V5 混合路由均不在本次范围内。完整 graph state 继续只保存在内存状态和 SQLite trajectory 中，不能因 Manager 上下文截断而丢失。

本方案不引入 embedding、实体模糊匹配、全局 LLM reconciliation 或新的 alias 表。它验证的是：同样的 64 条 edge 预算下，分层展示是否比单一的“词面相关度后按新近度”排序更能帮助 Manager 在跨 chunk 抽取时保持稳定。

### 2026-08-29 评审修订

本节以下规则优先于本文早期的简写示例。实现必须先完成 evidence role 校验、schema migration 和 replay API，再进入 LLM smoke；现有只覆盖旧 `edges/truncated` 协议的测试不能作为本方案通过依据。

## 2. 已观察到的问题

`bf659f65` 的在线轨迹说明，扩大单层 Manager 图谱窗口不能直接改善抽取质量。

| Manager edge 上限 | 平均 Manager 输入 tokens | 最终 edge 数 | `applied` | `applied_ambiguous_occurrence` |
| --- | ---: | ---: | ---: | ---: |
| 32 | 3,162 | 161 | 112 | 46 |
| 64 | 3,899 | 164 | 110 | 49 |
| 128 | 4,686 | 145 | 97 | 46 |

128 条不仅增加约 20% 的 Manager 平均输入，还没有带来更好的 occurrence 路由结果。原因不是完整图谱不存在，而是当前单一排序混合了两种不同目的：

- 当前 chunk 出现旧实体时，需要少量相关历史事实用于识别重复表述；
- 当前 chunk 没有明显旧实体时，仍需要最近事实作为有限的连续性参考。

两者混在一个排序中时，通用词可能挤占名词实体，较旧但相关的 edge 又会随窗口增大变成更多噪声。

## 3. 总体原则

```text
完整 graph state / SQLite
       |
       +-- 程序 occurrence router：读取全量 active edge
       |
       +-- Manager context renderer：只输出分层的最多 64 条 compact edge
                                      |
                                      +-- Manager：只抽取当前 evidence 中的事实
```

1. Manager context 是参考，不是写入依据。每个新 claim 必须引用当前 chunk 的 user evidence；不得仅因历史图谱存在而重述或补造事实。
2. Manager 不输出 `occurrence_key`、重复标记、合并动作、edge ID 或 update intent。
3. occurrence 去重只读取程序保存的全量 active edge，不能受 64 条 Manager context 选择结果影响。
4. 无法安全识别为同一 occurrence 时追加并标记 `applied_ambiguous_occurrence`，不为了减少 edge 数而强合并。
5. 上下文 renderer、router 和 SQLite 回放必须使用相同的规范化字段和稳定排序，保证同一输入得到同一结果。

## 4. 分层 Manager 上下文

### 4.1 固定预算

`manager_graph_max_edges` 固定为 **64**。本轮不再测试 128，也不把 64 作为 Answer graph projection 的预算。

edge 预算的逻辑分层如下：

| 层 | 上限 | 作用 | 选取规则 |
| --- | ---: | --- | --- |
| `relevant_edges` | 48 | 帮助识别当前 chunk 中重述、补充字段或明确更新的旧事实 | 与当前 chunk 的 user evidence 有实体字段命中 |
| `recent_edges` | 补足至 64 | 在没有足够实体命中时提供有限连续性 | 排除 `relevant_edges` 后，按首次写入 batch/edge 插入顺序倒序 |
| `raw_claims` | 保持现有 8 条，独立于 edge 预算 | 只保留 unknown relation 的低优先级审计线索 | 保持现有行为，不参与 occurrence 路由 |

若 `relevant_edges` 不足 48，剩余名额全部交给 `recent_edges`。若相关 edge 超过 48，只保留得分最高的 48 条；不得因为相关 edge 很多而突破 64。

### 4.2 相关性规则

相关性只服务于 Manager context 选择，不参与 router 的事实同一性判定。

程序从**当前 chunk 的 user evidence**生成 `current_entity_terms`，并只与 edge 的下列规范化字段比较：

```text
object
attributes.provider
attributes.location
```

不得把 `subject`、`predicate`、`status`、通用停用词或 assistant 文本作为命中依据。这样可以避免 `purchased`、`user` 等高频词把无关购买记录错误拉入相关层。

评分和排序固定为：

1. 完整规范化对象短语命中；
2. provider 或 location 完整短语命中；
3. 长度至少 2 的 token 交集数量；
4. edge 是否有 `occurrence_key`；
5. edge 的插入顺序倒序；
6. `edge_id` 升序作为最终稳定 tie-breaker。

只要第 1 至 3 项任一项大于零，edge 才能进入 `relevant_edges`。无法从当前 user evidence 得到有效实体词时，`relevant_edges=[]`，全部使用 `recent_edges`；不能回退到 assistant 文本或问题文本。这里的 `current_text` 只允许从 `role=user` 的 evidence 拼接；若没有 user evidence，query terms 为空，而不是把完整 chunk 当作匹配文本。

### 4.3 输出结构

Manager 看到的 JSON 改为显式分层，不再输出单个混合 `edges` 数组：

```json
{
  "version": 4,
  "manager_graph_edge_budget": 64,
  "relevant_edges": [
    {
      "subject": "user",
      "predicate": "PURCHASED",
      "object": "midnight sky",
      "status": "completed",
      "attributes": {"provider": "..."},
      "time": null,
      "scope": null
    }
  ],
  "recent_edges": [],
  "raw_claims": [],
  "active_edge_count": 161,
  "relevant_edge_count": 3,
  "relevant_edge_truncated": false,
  "recent_edge_truncated": true
}
```

渲染顺序固定为 `relevant_edges` 再 `recent_edges`。每层内部使用上述稳定排序，不能依赖 Python 容器的偶然顺序。edge 内容继续仅含 compact 字段：subject、predicate、object、status、amount/currency/count/provider/location、解析成功的 time/scope；不新增 evidence 全文、claim_text、alias 列表或 model response。

### 4.4 Schema migration

这是一次原子协议迁移，不能让调用方同时猜测新旧字段。`render_v4_manager_state()`、`manager_v4_messages()`、`run_v4_e2e.py`、`run_v4_claim_smoke.py`、`replay_v4_graph_answer.py` 和相关测试必须在同一个提交中切换到以下字段：

```text
relevant_edges, recent_edges
relevant_edge_count, recent_edge_count
relevant_edge_truncated, recent_edge_truncated
manager_graph_edge_budget
```

旧的 `edges`、`truncated` 不再作为 Manager 协议字段。batch 审计可额外保存 `manager_context_schema="v4.1"`，但不能用旧字段驱动逻辑。runner 和 smoke 的 `--manager-graph-max-edges` 默认值统一改为 `64`；所有离线 replay 也显式写入该参数。测试必须断言新字段，而不是仅修改断言路径后继续验证旧语义。

### 4.5 不改变的模型约束

Manager prompt 继续声明：历史 graph reference 是程序规范化结果，**不得没有当前 evidence 就复制历史事实**。它仍只输出：

```json
{
  "subject_text": "...",
  "relation": "...",
  "object_text": "...",
  "evidence_ids": [1]
}
```

`claim_text` 和 raw hints 继续可选。分层上下文只帮助当前 evidence 的抽取与表述一致性，不增加模型输出字段。

## 5. 程序侧 Occurrence 去重

### 5.1 路由输入与责任边界

处理顺序必须固定：

```text
Manager claim
  -> evidence 校验
  -> relation / subject / object / amount / provider / location / time / scope 规范化
  -> 生成 occurrence_key
  -> 在完整 active edge 集中执行 occurrence router
  -> 写入或合并 edge，并落 SQLite 审计
```

分层 Manager context 不能改变上述任一步。尤其 router 必须遍历完整 active graph，而不是 `relevant_edges + recent_edges`。

### 5.2 Evidence role 与可选字段约束

`evidence_ids` 的校验不是“ID 存在”就算通过。每个 ID 必须存在于当前 `CompiledEvidence`，且 `compiled.evidence[id].role == "user"`；assistant、system、question 或 unknown role 一律使 claim 进入 `incomplete` quarantine，不进入 graph。

`claim_text` 和每个 hint 都不能成为模型绕过 evidence 的第二事实通道：

1. `claim_text` 若存在，必须是所引用 user evidence 内容的严格子串；否则丢弃该字段并记录 `invalid_claim_text_hint`，raw claim 回退到 evidence 原文；
2. `hints.amount_text`、`count_text`、`time_text`、`provider_text`、`location_text`、`scope_text` 若存在，必须分别是所引用 user evidence 拼接文本的严格子串；
3. 未通过子串校验的 hint 不参与 `_parse_attributes()`、`_parse_time()` 或 scope 规范化，只保留在 model audit 中；
4. 正规化的金额、时间、provider、location 和 scope 只能来自 user evidence 原文，或来自程序明确记录的格式转换。

这样可以保证“当前 user evidence 是事实来源”成为程序约束，而不只是 prompt 要求。

### 5.3 精确去重与同日优先级

对于 `PURCHASED`、`ATTENDED`、`COMPLETED`、`OBSERVED_WAKE_TIME`、`MENTIONS` 等 occurrence predicate，先执行“是否可安全判定 occurrence”的判定，再计算 key。**ambiguous 判定优先于 exact key 合并**，解决同日两次同类事件字段完全相同的冲突。

唯一的 canonical serializer 为：

```text
canonical_json({
  subject,
  predicate,
  canonical_object,
  time: {value, granularity, interval_end, recurrence},
  provider,
  location,
  scope
})
```

predicate 先经过唯一 alias 表归一化；`observed_wake_time`、`observed wake time` 等输入统一为 `OBSERVED_WAKE_TIME`。时间 key 必须包含 `value`、`granularity`、`interval_end` 和 `recurrence`；scope 使用规范化值及 parse status。`claim_text`、evidence quote、chunk ordinal、模型自由文本 hash 均不得进入 key。

discriminator 规则如下：

1. 精确到 timestamp 的时间，或具有明确开始/结束区间且同时有 provider/location/scope 之一时，可作为强 discriminator；
2. 只有 day 粒度日期，且 predicate 属于可重复事件时，不能单独证明同日只有一个 occurrence；
3. 只有 provider/location/scope 而没有时间时，可以生成候选 key，但若同一 subject/predicate/object/context 已存在于不同 source unit，必须先标记 ambiguous；
4. 没有任何 discriminator 时不生成 key；
5. 同一 source unit 的重复 extraction，或后续 claim 明确补全同一 source unit 的字段，可以合并；不同 source unit 的 day-only 同 key claim 默认追加。

只有通过上述判定的完整 key 相同 edge 才视为同一 occurrence：

1. 合并新增的非空 amount、currency、count、provider、location、time、scope；
2. 合并去重后的 `source_refs` 和 `normalization_actions`；
3. 已有字段与新字段冲突时，保留原值，并把新值追加到 `attribute_conflicts`；
4. 返回 `deduplicated` 或 `deduplicated_merged`，不创建第二条 edge。

### 5.4 受限的对象表述修复

精确 key 不同但可能是同一模型抽取时，只允许现有的严格 `provider suffix` 合并。必须同时满足：

1. subject、predicate、规范化 provider、解析成功的 time 相同；
2. amount/count、currency、location 等已解析数值/上下文字段相同；
3. 两条 evidence 来自同一 source unit，或同一 session 内 unit ordinal 距离不超过 32；
4. object 去掉 `from <provider>` 或 `at <provider>` 后相同。

命中时返回 `deduplicated_alias` 或 `deduplicated_alias_merged`，并在 route result 中记录命中规则 `provider_suffix_same_source`。不扩大为编辑距离、语义相似度或跨 session 模糊合并。

### 5.5 不确定时追加

出现下列任一情况时，不合并：

- time/provider/location/scope 全部缺失，无法生成 occurrence key；
- 仅有 day 粒度日期，且同一规范化事实来自不同 source unit，无法证明是同一次事件；
- object 不同，且不满足受限 provider suffix 规则；
- 数量、金额、时间或 provider 冲突且不能解释为“后续补全”；
- evidence 相距较远，无法证明是同一 source event。

程序写入新 edge，`occurrence_key=null`，route status 为 `applied_ambiguous_occurrence`。这是一种保守保留，不表示 Manager 出错。

## 6. 审计与测试

本轮只增加与上述两项直接相关的记录：

```text
manager_context
  relevant_edges, recent_edges
  relevant_edge_count, recent_edge_count
  relevant_edge_truncated, recent_edge_truncated
  manager_graph_edge_budget

route_result
  route_status
  occurrence_key_present
  dedupe_rule: exact_key | provider_suffix_same_source | ambiguous_day | null
  merged_fields
  attribute_conflicts
```

必须新增或更新以下单元测试：

1. 64 条预算下，相关层最多 48、总 edge 最多 64，且相关层与最近层无重复；
2. 无有效 user 实体词时，不使用 assistant/question 文本，全部回退为最近层；
3. 相同 state 与 evidence 的 renderer 输出字节级一致；
4. 增加第 65 条无关 edge 不会挤掉已命中的相关 edge；
5. 完整 occurrence key 相同会合并 provenance 和缺失字段；
6. 同一 source unit 的 day-only 重复会合并，不同 source unit 的 day-only 同 key 会以 `ambiguous_day` 追加；
7. provider suffix 合并只在同 source 或相邻 unit 的严格条件下触发；
8. assistant evidence、非子串 claim_text/hints 不得进入 normalization；
9. Manager context 只保留 64 条时，router 仍可与第 200 条 active edge 做精确去重。

SQLite 轨迹中每个 batch 的 `memory_before_json.manager_context` 必须保存分层上下文快照。这样可以区分“Manager 没看到相关旧 edge”和“Manager 看到了但仍抽取异常”。

## 7. 验证顺序与通过条件

### Phase 1：离线 renderer/router/replay 回归

不调用 LLM。使用已有 V4 trajectory 的 `sidecar_v4_batches.raw_response` 和 `input_text` 作为 replay 输入，按 batch ordinal 重新执行 parser、normalizer 和 router；不从 `sidecar_v4_edges` 当前状态反推历史。每个 batch 的 `memory_before_json` 作为首次状态快照校验，replay 输出写入独立 SQLite run。确认分层上下文稳定、总 edge 不超过 64、occurrence 路由结果不因 context cap 改变。

实现最小 `replay_v4_manager_batches(source_db, source_run_id, target_db, target_run_id)` API：按 sample、batch ordinal 顺序读取 raw response，重建 state，复用同一 compiled evidence；对 offline smoke 中缺失完整前状态的旧记录，标记 `replay_status=unavailable`，不得声称 replay 通过。新的 smoke batch 必须保存完整 `memory_before_json` 和 raw response。

### Phase 2：四条 Manager smoke

固定 `chunk=2048`、`manager_graph_max_edges=64`，只观察 Manager/graph 轨迹，不以 Answer 准确率作为本轮主要结论。至少覆盖：

- `bf659f65`：跨 chunk 音乐购买的对象表述与重复 occurrence；
- `gpt4_d84a3211`：金额字段的后续补全与去重；
- `gpt4_2ba83207`：provider suffix 的严格合并边界；
- 一条没有明显实体重现的多 session 样本：验证最近层回退。

### 通过条件

1. 每次 Manager context 的 edge 数不超过 64；
2. 完整 graph 和 SQLite 重放的 edge 集合一致；
3. 已命中当前 user evidence 的相关 edge 不会因 recent edge 填充而被挤出；
4. exact/provider-suffix 以外的重复候选保持追加，不出现静默跨事件合并；
5. Manager 平均输入不高于当前 64 条单层实现的同样本基线约 3,899 tokens 的 5%；
6. 与单层 64 baseline 做逐 chunk 配对记录：`relevant_recall`、`false_positive_rate`、`exact_route_precision`、`ambiguous_route_precision`。这些是实现回归指标，不改变 Answer projection 或引入新的模型调用。

完成四条 smoke 后再决定是否运行 24 条。此次改动不改变 Answer projection，因此不将 Answer 的对错当作分层 Manager context 是否通过的唯一依据。

## 8. 不纳入本次修订

以下项目有价值，但明确延后：

1. Answer 的 event-count、sum/rank 或 query-specific projection；
2. entity alias 索引、embedding 召回和模糊实体链接；
3. unknown/raw claim 的新投影规则；
4. 统计显著性评估和扩大数据集；本次只记录逐 chunk 的最小回归指标；
5. V5 的数字/非数字混合路由。

这样可以把这次实验的因果关系限制为：**分层的 64-edge Manager reference 是否改善跨 chunk 抽取参考，而程序侧 occurrence router 是否仍保持保守且可回放的去重。**
