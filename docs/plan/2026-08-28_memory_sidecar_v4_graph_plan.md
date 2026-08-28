# Memory Sidecar V4 图谱化事实记忆实施与验证计划

## 1. 目标与边界

V4 用一个受限的事实图谱替代 V3 的自由格式 `memory_type + key + value` 记录，验证以下想法。图谱 schema 是程序内部契约，不要求 Manager 一次生成完整图谱边：

> 在模型能力有限、需要处理多事实、时间和实体关系的场景中，固定的节点/关系 schema，加上可追溯证据和确定性合并，是否比自由 key 更稳定。

V4 是验证性实验，不立即替换 V3。第一阶段只改变 memory representation、merge 和 query projection；Answer 模型、Answer 提示词、样本、raw tail 和 judge 均保持不变。

V4 不尝试一次解决以下问题：

1. 让 LLM 自动补全原文没有表达的事实。
2. 用图谱推理替代 Answer 模型的聚合、排序和日期计算。
3. 用 embedding、模糊相似度或外部图数据库自动猜测实体同一性。
4. 用一次全局 LLM reconciliation 修复抽取错误。
5. 在第一轮同时引入图谱摘要、GraphRAG 和新的 Answer prompt。

### 1.1 2026-08-29 实现修订

两条在线 smoke 轨迹确认了以下实现缺口，均在正式 24 条评测前修复：

1. 同 occurrence 的后续 claim 如果补充金额、provider、location、time 或 scope，必须合并非空字段、合并 provenance；不能仅因 identity 相同而丢弃后续字段。非空字段冲突写入 `attribute_conflicts` 审计字段。
2. SQLite edge upsert 必须写回上述合并后的 attributes、time、scope、provenance、normalization actions 和 conflicts；只更新 status 会使进程内结果与回放结果不一致。
3. V4 Manager 不再读取不断增长的全图状态。occurrence 去重和 functional 路由由程序执行，给抽取模型重复注入图状态只会放大 prompt、成本和噪声。
4. Manager 只从 user evidence 提取受控关系中的 durable facts；不抽取问题、请求、助手文本、假设或备选方案，并设置每 chunk 最多 12 条 claim，降低 raw claim 和截断 JSON。
5. Answer projection 的 provenance 由稳定的 `unit_ordinal` 表示，不能使用 chunk-local `evidence_id`，否则跨 chunk 的 `e0` 没有可解释性。

## 2. V3 暴露的问题与 V4 假设

V3 的 24 条实验显示，完整 memory 不一定能让 Answer 正确，summary 还可能删除回答所需的 observation、金额明细和日期链。轨迹中有四类适合用结构化表示验证的问题：

| 问题 | 轨迹表现 | V4 要验证的修复 |
| --- | --- | --- |
| 事件字段位置不稳定 | 购买金额出现在顶层而不是 `value`，导致 purchase item 被拒绝，`$25` 丢失 | 金额、货币、日期、动作使用固定属性字段，解析失败保留原始 claim，不静默丢弃 |
| 不同实体被同 key 覆盖 | edX 的 8 门课程与 Coursera 的 12 门课程共用 key，后者 supersede 前者 | provider、对象和关系进入图谱身份；不同 provider 默认追加，不允许跨实体 UPDATE |
| target 与 observation 混淆 | `7:15` 工作日目标覆盖/竞争 `7:30` 周六观察 | 用不同 predicate 和不同 occurrence 节点表达目标、观察、完成事件 |
| 同类事件无法聚合 | 多个商店购买、多个日期事件进入平面记录，Answer 自己猜哪条或如何求和 | 每次 occurrence 单独成边，聚合只对明确时间/实体范围做确定性查询 |

关键验证样本优先使用：`gpt4_d84a3211`、`67e0d0f2`、`dad224aa`、`gpt4_2ba83207`。它们分别覆盖金额遗漏、provider 覆盖、target/observation 混淆和多条购买记录选择。

## 3. 总体架构

```text
历史 turns
  -> 按 evidence 时间顺序切 chunk
  -> Manager 输出受限 claims JSON
  -> schema parser + evidence 校验
  -> SQLite graph nodes/edges + evidence ledger
  -> 确定性 identity / conflict / occurrence router
  -> 问题相关 subgraph projection
  -> 可选 summary projection + raw tail
  -> 原 V1 Answer prompt
```

第一轮实验不需要 Neo4j。SQLite 的邻接表足以支持插入、冲突审计、按 predicate/entity/date 检索，并且能复用当前 trajectory 数据库和回放工具。V4 在进入 Phase 1 前必须先冻结下面的 typed claim、时间和 predicate contract；否则不开始实现。模型只负责识别“谁、做了什么、涉及什么对象、原文证据在哪里”，节点类型、provider 边、时间规范化、occurrence identity 和路由全部由程序完成。

## 4. 图谱数据模型

### 4.1 节点

节点只表示实体或事件实例，不把当前值直接写进节点 ID。

| `node_type` | 示例 | 说明 |
| --- | --- | --- |
| `person` | `user`、`rachel` | 人物/主体 |
| `entity` | `edx`、`walmart` | 机构、商店、地点、服务 |
| `item` | `bike.chain.replacement` | 可购买或被提及的对象 |
| `event`、`observation`、`goal` | 预留，不在第一轮创建 | 第一轮直接把发生、观察、目标作为 edge 的 predicate/status/attributes 表达 |
| `value` | 首轮不使用 | 金额、计数、时间统一放在 typed edge attributes，避免两套表示 |

节点字段：`node_id`、`node_type`、`canonical_name`、`attributes_json`、`created_batch`、`evidence_ids`。`node_id` 由程序依据规范化规则生成；LLM 不生成数据库 ID。首轮只允许显式同名或预置 alias，不用 embedding 推断节点同一性。

节点类型由程序的有限规则表生成：主体文本默认 `person`；provider/location 命中的实体进入 `entity`；第一轮不创建独立 occurrence node，购买、观察和目标都直接落为 subject -> object 的 edge。无法判断的 object 一律使用 `unknown_entity`，不猜成 `item`、`entity` 或 `value`。`unknown_entity` 仍可作为 edge 的对象和文本检索结果，但不能参与需要明确类型的聚合或跨实体 alias 合并。

### 4.2 边

边是回答可用的最小事实单元。每条边都必须带 provenance。

字段：

```text
edge_id
edge_key
subject_node_id
predicate
object_node_id 或 object_literal
attributes_json
time_json
normalized_valid_start
normalized_valid_end
reported_time_json
scope_json
status
confidence
evidence_ids
supersedes_edge_id
```

首轮只允许固定 predicate 集合：

```text
PURCHASED, OWNS, ATTENDED, COMPLETED,
TARGET_WAKE_TIME, OBSERVED_WAKE_TIME,
LOCATED_IN, PREFERS, PLANS,
MENTIONS, SUPERSEDES, CONTRADICTS
```

`status` 只允许 `observed`、`completed`、`planned`、`active`、`superseded`、`contradicted`。未知 predicate 不得被猜测映射，统一落为 `UNKNOWN_RELATION`；原始 claim 仍写入 audit 表。语义不确定与结构不完整都不得静默丢弃。

金额、货币、计数、时间首轮只允许出现在 `attributes` 的 typed fields，不再同时创建 `HAS_AMOUNT` 或 value node：

```json
{"amount": 25, "currency": "USD", "count": null, "time": null}
```

四个字段是唯一的模型必填项：`subject_text`、`relation`、`object_text`、`evidence_ids`。`claim_text` 是可选原文摘录；`hints` 整体可省略，内部各字段也全部可省略。若不提供 `claim_text`，程序以 evidence message/unit text 作为 raw claim，不要求模型重写原文。

第一轮不创建独立 occurrence node，也不写 `PROVIDED_BY`/`OCCURRED_AT` 辅助边。provider、location、time、scope 全部写入 occurrence edge 的规范化 `attributes`/`time_json`/`scope_json`，并参与 occurrence identity；renderer 将它们内联展示。若未来需要多跳图查询，再单独设计 event node 版本，不与本轮实验混用。

### 4.3 Predicate contract

每个 predicate 在代码中注册一条 contract，禁止由模型自由决定更新语义：

| predicate | kind | identity key | 不同值处理 | 允许的 context |
| --- | --- | --- | --- | --- |
| `PURCHASED`、`ATTENDED`、`COMPLETED`、`OBSERVED_WAKE_TIME` | occurrence | subject + predicate + object + occurrence_key | 追加；同 occurrence 同值去重，不同值建 conflict | provider、location、time |
| `TARGET_WAKE_TIME`、`LOCATED_IN`、`PREFERS`、`PLANS` | functional snapshot | subject + predicate + scope | `update_intent=replace` 时 supersede；否则冲突保留双方 | scope、time |
| `MENTIONS` | occurrence | subject + predicate + object + occurrence_key | 追加或同值去重 | time |

这里的 `object` 是否进入 identity 由 contract 决定；不能再出现“同 key 但 object 不同却意外 UPDATE”的隐含行为。

### 4.4 证据账本

每个 chunk 先生成局部 evidence map，保留 `evidence_id -> session_id/message_index/unit_ordinal/text`；如果底层消息可提供字符范围，则同时保存 `char_start`、`char_end` 和 `evidence_quote`。边上保存局部 ID 和解析后的 `source_refs`，不得把整个 chunk 当成唯一来源。没有字符范围时，`unsupported_inference_count` 只按 message/unit-level 审计，不宣称有 span-level 精度。

## 5. Manager 抽取协议

### 5.1 最小 claim 协议：降低模型负担

Manager 不直接生成 `node_id`、节点类型、`occurrence_key`、规范化日期、`status` 或 `update_intent`。它只输出事实原子和证据；金额、日期、provider、地点等只是可选 raw hints：

```json
{
  "claims": [
    {
      "subject_text": "I",
      "relation": "bought",
      "object_text": "bike chain replacement",
      "claim_text": "I bought a bike chain replacement for $25 at Bike Shop on April 20, 2023.",
      "hints": {
        "amount_text": "$25",
        "time_text": "April 20, 2023",
        "provider_text": "Bike Shop",
        "location_text": null,
        "scope_text": null
      },
      "evidence_ids": [2]
    }
  ]
}
```

协议边界固定为：Manager 的 `hints.*_text` 是可选原始提示；程序生成的 edge `attributes`、`time_json`、`scope_json` 才是规范化 typed 字段；`claim_text`（若有）和 evidence message/quote 是原始证据。normalized 字段不得回填到 Manager prompt，也不得把 hints 当成已解析数值。

模型只需输出原文中最接近的关系短语，不要求记住大写 predicate 名称。程序 alias 表将 `bought/purchased`、`went to/attended`、`wants to wake/target` 等归一化；无法归一化时保留 `UNKNOWN_RELATION`，不拒绝 claim。

程序接管以下工作：

1. `subject_text/object_text` 和可选 hints 的空白、大小写、货币符号、常见 alias 和安全 canonicalization。
2. 从可选 `claim_text`、evidence message/quote 及 hints 确定性解析金额、计数、日期；解析失败时保留 raw text，边标记 `unparsed_attribute`，不能凭常识补值。
3. 从 claim_text/hints 解析 provider、location 和 scope；解析不确定时分别落为 `unknown_provider`、`unknown_location`、`scope_parse_status=unknown`。
4. 将已确认的 provider/location 写入 occurrence edge 的 normalized attributes，将 scope 写入 `scope_json`；未知值保留在 raw 字段，不用于精确 query 或 supersede。
5. 根据归一化后的 relation 和 `claim_text`/evidence 原文中的有限 update phrase 规则推导 predicate contract、状态和是否可能是 replace；无法确认更新意图时走 conflict/append，不 supersede。
6. 生成节点 ID、`occurrence_key`、`edge_key`、normalized time 和审计字段。

程序辅助不是静默纠错：每次 alias、数字/日期解析、字段迁移和 fallback 都写入 `normalization_actions`，保留 `model_claim_json` 与 `normalized_claim_json`，便于区分“模型没抽到”和“程序解析失败”。

抽取规则：

1. 只抽取原文明确表达的事实；不根据常识补值。
2. 一次购买、一次观察、一次参加活动分别是独立 claim，即使 subject 和 predicate 相同。
3. 金额、货币、计数、名称、时间优先保留在 evidence 原文；hints 和 `claim_text` 都可选，模型不需要生成 ISO 日期或数值类型。
4. 目标、观察和完成事件在 `relation` 中使用最接近的原文短语；程序归一化为不同 predicate。
5. `evidence_ids` 必须是当前 chunk 内合法的非空整数数组；越界 claim 独立拒绝。
6. provider、location、scope 不作为必填字段；能识别就写 raw hint，不能识别就留空。

### 5.2 程序辅助与容错边界

解析器按以下顺序工作：提取 fenced JSON -> 顶层 `claims` alias -> 字段 alias（如 `subject` 转 `subject_text`）-> 类型转换 -> evidence 校验。relation 可归一化且 subject/object/evidence 完整时进入 graph；relation 未知但 claim 完整时写入 `sidecar_v4_raw_claims`，默认作为 graph-all 最低优先级 raw 文本行，不参与聚合、精确 query 或 supersede；subject、object 或 evidence 缺失时只进入 quarantine/audit，不进入 graph。三类状态分别记为 `normalized`、`unknown_relation`、`incomplete`，不再混用 reject。程序不得凭空生成缺失事实，也不得把低置信的猜测当成规范化成功。

时间解析由程序输出 `time_json`，包括 `value`、`granularity`、`timezone`、`interval_end`、`recurrence`、`relative_to` 和 `parse_status`。相对时间的 `reference_date` 使用 evidence 对应的 message/session date，而不是全局样本日期；多条 evidence 的 session date 不一致时设置 `relative_time_ambiguous`，不强行解析。失败时保留 `time_text` 和 `parse_status=unparsed`，查询只能把它当文本证据，不能假定具体日期。

下面是程序规范化后的时间对象示例，不是 Manager 必须生成的格式：

```json
{
  "value": "Saturday",
  "granularity": "recurrence",
  "timezone": null,
  "interval_end": null,
  "recurrence": {"days_of_week": ["SAT"], "period": "weekly"},
  "relative_to": {"reference_date": "2023-05-27", "expression": "previous Saturday"}
}
```

`reference_date` 来自当前样本定义的会话日期，不由 Answer 或 router 猜测。`scope` 是正式字段，可取 `weekday`、`weekend`、实体 ID、时间区间或样本定义的其他枚举；未知 scope 保留 claim，设置 `scope_parse_status=unknown`，禁止用它做精确 query 或 supersede。默认 timezone 为 `null`；只有原文明确给出时才填写，不使用数据集所在地或模型常识补全。

## 6. 确定性路由、身份与冲突规则

### 6.1 节点身份

程序使用规范化后的 `node_type + canonical_name` 建立实体节点。首轮只接受显式同名或预置 alias 表；不使用 embedding 猜测 `edX`、`Coursera`、`Data Analysis` 是否相同。

### 6.2 occurrence 与 snapshot

- `PURCHASED`、`ATTENDED`、`OBSERVED_WAKE_TIME` 等发生类事实默认追加 occurrence edge。
- 程序为每条 occurrence 生成 `occurrence_key`，只能使用规范化字段：`subject + predicate + object + normalized_time + normalized_provider + normalized_location + normalized_scope`；不使用 `claim_text`、evidence quote 或自由文本 hash。
- 同一 occurrence 在不同 chunk 重复陈述时，只有上述字段完整且相同才去重；缺少任何能区分实例的字段，或同一天可能存在多次同类事件时，标记 `ambiguous_occurrence`，使用本次 claim 的审计 key 追加，不强行 deduplicate。
- `TARGET_WAKE_TIME`、`LOCATED_IN`、`PREFERS` 等状态类事实按 predicate contract 的 `subject + predicate + scope` 更新；object 是值，不是默认 identity。

程序只对有限、可测试的明确更新短语设置 `update_intent=replace`：`changed X to Y`、`updated to Y`、`now lives in Y`、`no longer X; instead Y`、`corrected X to Y`。`still likes X`、`used to plan X`、`actually completed X` 等只表达状态或时间变化，不自动视为 replace；没有命中词表时一律 `update_intent=unknown`，追加并建立 conflict（若 predicate 为 occurrence 则直接追加）。词表、命中位置和未命中案例必须进入单元测试。

### 6.3 不允许的隐式合并

1. 不同 provider、商店、地点或 item 不得因为 predicate 相同而互相 UPDATE。
2. `planned`、`observed`、`completed` 不得共用一个 snapshot key。
3. 两个不同数值的同一 functional scope 不直接互相 supersede；新边追加，并建立 `CONTRADICTS` 与 conflict 记录。只有带有明确“改为/更新为/现在是”等更新语义且命中 contract 的 claim 才能 supersede。
4. 聚合 count/sum 只能由查询程序对显式 occurrence 集合计算，不能由 Manager 生成一个覆盖明细的 aggregate UPDATE。
5. 任一 claim 无法规范化时，原始 JSON、原因和 evidence 仍写入 `sidecar_v4_raw_claims` 或 quarantine/audit，保证错误可追溯；不使用单一 reject 状态掩盖 `unknown_relation` 与 `incomplete` 的区别。

## 7. Answer 上下文投影

首轮不把原始图 JSON 直接交给 Answer，而是由程序渲染为稳定文本，保持 V1 Answer prompt 不变：

```text
[PURCHASED] user bought bike chain replacement; amount=25 USD; time=2023-04-20; source=e2
[PURCHASED] user bought bike light; amount=40 USD; time=2023-04-20; source=e3
[TARGET_WAKE_TIME] user target=08:00; scope=weekend; source=e5
[OBSERVED_WAKE_TIME] user observed=07:30; time=2023-05-20; source=e7
```

Projection 规则：

1. 第一轮先运行 `graph-all`：返回该样本的全部 active/observed/completed/planned edges，按 `normalized_valid_start`、predicate contract 顺序稳定排序，不引入问题解析变量。occurrence 主边内联 provider/location 文本和 evidence；`unknown_relation` raw claim 作为最低优先级文本行保留，不参与聚合或精确 query，避免“审计存在但 Answer 完全看不到”。
2. `query-specific` 作为后续独立实验，只使用规则解析器识别显式实体、predicate、时间范围和 scope；无法可靠解析时回退到 `graph-all`，并记录 `question_signature`、`filter_reason` 和 `fallback=true`。第一轮不使用 LLM 解析问题。
3. 输出 occurrence 明细，不提前把多条记录压成一个模型生成的总数；同时提供确定性聚合 API：`count(edges)`、`sum(attributes.amount)`、`rank_by(time|amount)`，每个结果附 `input_edge_ids`。是否把聚合行交给 Answer 作为单独 projection mode 记录，不能与 graph representation 混为一个变量。
4. 默认排除 `superseded`，保留 `contradicted` 的双方及来源，避免静默丢失。
5. 与 V3 一样，最终上下文由 `graph projection/summary + raw tail` 组成，并与滚动式摘要共用预算。graph projection 设独立 `max_graph_tokens`，并为 occurrence、functional snapshot、conflict、amount/count/date 四类设置最小保留配额；超限时先满足配额，再按“问题明确命中的 predicate/entity（仅 E 组）> typed amount/count/date/provider/time 字段 > normalized_valid_start 新近度 > edge_id”裁剪。C/D 的 `graph-all` 不读取 gold/reference，也不使用运行后才知道的 gold-critical 标记。每条保留边必须带 evidence；分别记录 `graph_truncation_count`、`raw_tail_truncation_count` 和总 `truncation_count`。smoke 和 24 条 pilot 只要发生 `graph_truncation_count > 0`，结构门槛即失败并先调整预算/投影，不继续比较答案。
6. V4 的 summary 是可选实验变量。先验证 `graph + raw tail`，再复用最后一次摘要做 `graph-summary + raw tail` 对照；summary 不把维护后的 memory 再次作为输入。

## 8. SQLite 与审计

建议新增以下表，保留现有 V3 表和数据库不变：

```text
sidecar_v4_nodes
sidecar_v4_edges
sidecar_v4_evidence
sidecar_v4_conflicts
sidecar_v4_quarantine_claims
sidecar_v4_raw_claims
sidecar_v4_normalization_actions
sidecar_v4_projections
sidecar_v4_metrics
```

幂等约束：每个 batch 具有 `run_id + sample_id + batch_ordinal + input_hash` 唯一键；每条 claim 具有 `claim_id`，每条 occurrence 具有唯一 `occurrence_key`，edge 使用 `edge_key` 唯一约束。重复提交同一 batch 必须返回原路由结果，不得新增重复 edge；输入 hash 不同则作为新 batch 审计。回放测试必须至少执行“同一 batch 两次”和“删除缓存后重放”两种场景。

每个 batch 还需记录：原始 prompt、原始 response、finish reason、chunk 范围、claim ordinal、parser 状态、router 决策、写入前后 edge 快照。这样可以回答“模型没抽到”“解析丢了”“路由覆盖了”还是“Answer 选错了”。

## 9. 实施阶段

### Phase 0：冻结基线与 schema

- 固定 `chunk=2048`、V1 Answer prompt、raw tail 和 DeepSeek judge。样本采用分阶段规模，不把 24 条 pilot 误当成最终结论：4 条 smoke -> 24 条 pilot -> 120 条确认实验（4 条 smoke + 20 条 pilot 其余样本 + 96 条新增确认样本）。
- 为四条关键样本写结构断言：金额 `$25/$40/$120` 均存在；edX/Coursera 分开；target/observation 同时存在；不同商店购买边不互相覆盖。
- 建立 V4 run manifest，记录模型、prompt hash、代码 commit 和预算。

样本选择按题型分层，而不是简单取前 N 条。至少覆盖 `knowledge-update`、`multi-session`、`temporal-reasoning`、金额/计数聚合、provider/entity 区分、target/observation 和重复 occurrence。4 条 smoke 用于定位实现错误；24 条 pilot 用于决定 schema、parser 和 projection 是否可继续；120 条才用于报告总体趋势。若当前只能取得 24 条，结论只能写成 pilot 观察，不能宣称 V4 泛化或显著提升。

### Phase 1：抽取与解析

- 新增 `memory_sidecar/v4.py`：固定 predicate、节点、边和 claim schema。
- 新增 `memory_sidecar/process_v4.py`：复用 V3 的 chunk/evidence/trajectory 流程。
- 新增 parser 单元测试：字段缺失、未知 predicate、非法 evidence、截断 JSON、同 batch 多 occurrence、缺少 claim_text、unknown scope 和相对时间多 session date。
- 增加模型负担指标：JSON 可解析率、claim 完整率、字段 alias 修复率、relation alias 命中率；这些指标与 Answer 准确率分开报告。

### Phase 2：图存储与确定性路由

- 新增 `memory_sidecar/v4_graph.py`，实现 node upsert、occurrence append、snapshot supersede、conflict append 和 transaction rollback。
- 所有拒绝都落库，不因单条 claim 失败而丢弃同 batch 其他合法 claim。
- 增加回放测试：从 SQLite 删除进程内缓存后，重放 batch 仍得到相同 edge 集合。

### Phase 3：投影与 Answer 接口

- 先实现 `graph-all` 和明确的 `count/sum/rank` projection API，再实现规则化的实体、predicate、时间范围和 scope query；所有 query 都输出 `question_signature`、筛选条件、fallback 原因和 input edge IDs。
- 实现稳定文本 renderer，并适配现有 Answer 调用；Answer prompt 文本不改。
- 实现 graph-only、graph-summary 两种 projection，二者共享 raw tail 和预算计算器。

### Phase 4：四样本 smoke

- 先跑 `gpt4_d84a3211`、`67e0d0f2`、`dad224aa`、`gpt4_2ba83207`。
- 逐层检查 Manager JSON、nodes/edges、conflicts、projection、Answer 输入和答案。
- 未通过结构断言前，不扩大到 24 条。

`chunk=2048` 在 smoke、pilot 和确认实验中保持不变。这样可以隔离“图谱表示”变量；8192 只在 V4 结构和准确率通过后作为独立 chunk 消融，不与首轮样本规模同时改变。

### Phase 5：24 条 pilot 与 120 条确认实验

建议按以下顺序运行，减少变量混淆：

| 实验臂 | Memory projection | Summary | 目的 |
| --- | --- | --- | --- |
| A | V3 canonical JSON | off | V3 baseline |
| A0 | V3 flat-text control，使用与 C 相同的稳定文本 renderer | off | 控制 JSON/text、排序和 provenance 展示变化 |
| B | V3 summary | on | V3 压缩对照 |
| C | V4 graph-all projection | off | 首要假设，隔离图谱路由收益 |
| D | 与 C 相同的 V4 graph snapshot + graph summary | on | 只改变 summary，判断摘要是否仍损失结构事实 |
| E | V4 query-specific projection | off | C 通过后，单独验证查询筛选是否有收益 |

A/B 可以复用已落盘结果；A0 必须使用与 C 相同的文本 renderer、排序、evidence 展示和预算，但输入事实仅来自 V3 flat records。C 只构建一次 V4 graph snapshot，D 必须从 C 的同一 snapshot 读取并生成最后一次 summary，不得重新调用 Manager。E 仅在 C 完成后运行。每条完成样本执行一次 Answer，再统一调用 DeepSeek judge，不能边跑边改变 rubric。24 条 pilot 通过结构门槛后，再用同样的 `chunk=2048`、prompt 和 judge 扩展到 120 条；确认实验不得重新调整 schema 或 alias 表，否则另起 run 并标记为新版本。

## 10. 评估指标

### 主指标

1. DeepSeek judge accuracy：总准确率及 `knowledge-update`、`multi-session`、`temporal-reasoning` 分项。
2. 四条关键样本的结构断言通过率。

### 诊断指标

1. `claim_extraction_coverage`：gold-critical 金额、日期、数量、实体是否有 edge 和 evidence path。
2. `cross_entity_collision_count`：不同 provider/item/location 被错误覆盖的次数。
3. `unsupported_inference_count`：除允许的字符串规范化、预置 alias、日期格式转换外，图谱中出现但没有 message/unit-level evidence 支持的节点/边数量，目标为 0；只有数据源提供字符 offset 或 quote 时才额外报告 span-level 子指标。
4. `conflict_preservation_rate`：冲突双方是否同时保留并可追溯。
5. `quarantine_claim_rate`、`unknown_relation_rate` 与原因分布；重点关注关键 claim 是否因 schema 失败无法进入 projection。
6. projection 中 evidence 覆盖率、Answer 输入 tokens、Manager/Answer 调用数、wall time。
7. `truncation_count`：记录而非默认认为发生错误；raw tail 只在实际超预算时截断。
8. `manager_json_parse_rate`、`claim_complete_rate`、`normalization_repair_rate`：确认改进来自程序辅助后的结构稳定性，而不是把模型格式错误混入图谱质量。
9. A0 与 C 的 `field_coverage_diff`：单独列出 provider、location、time、amount/count 等字段覆盖差异；A0 只控制 renderer/排序/展示格式，不宣称与 C 拥有完全相同的事实集合。

## 11. 通过门槛与决策

V4 进入完整实验的门槛：

1. 四条 smoke 样本结构断言全部通过。
2. 关键 claim 不得进入 `incomplete` quarantine，且不得发生跨实体错误 UPDATE；`unknown_relation` 可以存在，但必须出现在 raw projection 或明确记录为不可回答。
3. 回放结果与首次运行的 edge 集合一致。
4. Answer 输入中的每个 gold-critical 事实都能定位到至少一个 evidence ref。

V4 可作为 V3 的后续主线需同时满足：

1. graph-only 的 DeepSeek judge accuracy 不低于 V3 summary-off；
2. 四条关键结构错误至少减少两类，且没有新增跨实体覆盖；
3. `unsupported_inference_count == 0`，冲突保留率和 evidence 覆盖率显著高于 V3；
4. 成本和延迟不超过预注册上限：Manager logical completion 不超过同 chunk V3 的 1.5 倍，Answer 平均输入不超过 V3 canonical 的 1.2 倍，wall time 不超过 2 倍；超限必须单列原因。若 graph-only 通过而 graph-summary 失败，保留图谱、暂缓摘要；若两者都未通过，回到抽取 schema 和 Answer projection 诊断，不继续叠加 GraphRAG。

24 条样本只作为配对 pilot，不宣称统计显著。pilot 报告必须给出逐样本配对差异；预注册最低继续开发效果量为 `+2/24`（或保持准确率且关键结构错误至少减少两类），低于该阈值只暂停或修改方案，不宣布准确率提升。120 条确认实验再给出 95% bootstrap confidence interval，并补充 McNemar 检验；同时报告结构指标、成本、延迟、拒绝率和 chunk 调用数。若 120 条不可用，保留 bootstrap 作为描述性区间，不写“显著”。

## 12. 风险与后续实验

图谱只能保证“事实结构不乱”，不能保证弱 Answer 模型一定会正确求和、排序或解释时间。若 C 组结构指标明显改善但准确率不升，下一步应单独测试 query-specific aggregation/projection；不要把失败归因给图谱本身。

若固定 predicate 导致抽取覆盖下降，优先扩充受控 vocabulary 和 alias 表，并保留 raw/quarantine claim；不要退回自由 key。只有在 24 条结果证明图谱 representation 本身有收益后，才考虑更大的样本、8192 chunk、摘要提示词优化或更强 Answer 模型。

## 13. 可复现产物

- 计划文档：`docs/plan/2026-08-28_memory_sidecar_v4_graph_plan.md`
- V3 基线与结果：`docs/plan/2026-08-28_memory_sidecar_v3_2048_24_evaluation.md`
- V4 运行目录：`results/memory_sidecar/sidecar-v4-graph-*`
- V4 轨迹数据库：每个 run 独立保存 `trajectory.sqlite3`
- V4 judge：每个 run 保存 `deepseek_judgments.jsonl` 和汇总 JSON
- 固定样本：`data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv`
- 当前原型接口：`memory_sidecar.v4.render_v4_graph_all`、`memory_sidecar.v4.answer_v4_messages`；Answer 使用全图文本 projection，合并后的上下文记录在 `sidecar_v4_projections`。
