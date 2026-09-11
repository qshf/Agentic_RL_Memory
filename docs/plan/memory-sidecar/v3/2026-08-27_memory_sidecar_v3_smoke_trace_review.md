# Memory Sidecar V3 Smoke 轨迹审计与整改说明

## 1. 这份文档解决什么

本文分析 V3 的四条定向 smoke run，而不是重新设计 Answer。目标是直接回答三个问题：

1. 轨迹中实际发生了什么？
2. 哪些问题是 router 的问题，哪些是 Manager 给出了错误的 memory schema？
3. 下一版应改哪一个协议字段、哪一个校验、哪一个测试？

审计对象：

```text
results/memory_sidecar/sidecar-v3-smoke-4-20260827b/trajectory.sqlite3
```

运行配置为 V3、8192-token chunk、4096 Manager output、80K shared context、串行处理。
四条均完成；每条均为 14 个 terminal leaf Manager batch 和 1 个 Answer completion，未发生
output truncation、HTTP failure 或 duplicate current key。

Answer 结果仅作旁证，**不作为本文的整改对象**。这次 run 在 Answer prompt 回退到 V1
之前启动，因此不能与 V1 Answer 进行严格比较；但 Answer 调用发生在所有 Manager batch
和 memory state 已经提交之后，不影响本文对 memory 轨迹的判断。后续 V3 已固定复用 V1
Answer prompt，不再附加 V3 指令。

## 2. 先给结论

V3 的持久化和基本 UPDATE 机制通过了，但“模型输出任何符合 JSON 外形的记录都被接受”
仍然过宽。四条中有两条状态语义错误，另外金额指标没有可解释的口径。

| 项目 | 轨迹结果 | 判断 |
| --- | --- | --- |
| 原子 batch、evidence refs、顺序回放 | 4/4 正常；无 duplicate current key、无截断 | 保持 |
| Wells Fargo `$350k -> $400k` | 旧 record superseded，新 record current | 通过 |
| Rachel `Chicago -> suburbs` memory 更新 | location record 正确 supersede | 通过；Answer 受旧 Chicago visit plan 干扰，非本轮 Answer 改动范围 |
| 新增 1915-S quarter | quarter 被保留，但 count 从 37 被推导为 38 | 已记录；本轮不改动 count 行为 |
| 周末 wake target / observation | `8:00` target 与多次实际起床时间混在一个 plan value | 失败：记录不可直接回答状态问题 |
| 金额事件 | 多数金额被写成字符串；一个事件使用无效 `memory_type=purchase` | 失败：金额协议未落地 |
| current memory 大小 | 每条 65-95 条 current record，Answer 输入约 31K-34K tokens | 未超预算，但质量风险高 |

下面的整改中，A、C 是下一次 smoke 前必须做的协议改动；B 只保留为轨迹记录，本轮不改动；
D 暂不处理，先验证最终摘要对 Answer 的影响。

## 3. 已证明正确的部分

### 3.1 UPDATE 与追加式历史正常

`852ce960`：

```text
b1-i5   user.loan.preapproval.amount = "$350,000"  superseded by b13-i8
b13-i8  user.loan.preapproval.amount = "$400,000"  active
b1-i6   user.loan.preapproval.lender = "Wells Fargo" active
```

`830ce83f`：

```text
b11-i2  user.friend.rachel.location = "Rachel lives in Chicago."  superseded by b13-i1
b13-i1  user.friend.rachel.location = "Rachel lives in the suburbs." active
```

这两条说明：batch 按时间串行、event ID、source ref、UPDATE 的整体替换、SQLite
同步和 current-only projection 的基础实现没有问题。不要为修复下面的问题重新引入
V2 的 PATCH、target_ref 或 field merge。

### 3.2 运行不变量正常

| question_id | Manager leaf | Answer | split parent | duplicate-key reject | current records |
| --- | ---: | ---: | ---: | ---: | ---: |
| `830ce83f` | 14 | 1 | 0 | 0 | 95 |
| `852ce960` | 14 | 1 | 0 | 0 | 74 |
| `69fee5aa` | 14 | 1 | 0 | 0 | 65 |
| `dad224aa` | 14 | 1 | 0 | 0 | 78 |

因此下一步不应优先修改 chunk split、transaction 或 batch ordering。问题是 Manager
分类和 V3 parser/router 对该分类的约束不够。

## 4. 问题 A：计划、目标、观察没有程序级区分

### 4.1 轨迹里发生了什么

`dad224aa` 最终留下的是一条 current plan：

```text
key:    user.plans.sleep.schedule.weekends
type:   plan
status: active
value:  "Desired wake-up time: 8:00 am. Recent wake-up times:
         7:30 am (previous Saturday), 9:30 am (last Saturday), 8:30 am (Saturday)."
```

它把三种不同的状态塞在一个 string 里：

```text
目标 target:                 8:00
已发生观察 observation #1:  7:30
已发生观察 observation #2:  9:30
已发生观察 observation #3:  8:30
```

这不是 Answer 推理失败，而是 memory 本身缺少可路由的状态边界。任何后续强模型都会
看到冲突描述，而不是看到一个 plan 和三个 occurrence。

同一个问题还出现在 `830ce83f`：

```text
plan / active: user.plan.visit.friend.rachel
               "planning to visit ... Rachel ... new apartment in Chicago"

fact / active: user.friend.rachel.location
               "Rachel lives in the suburbs"
```

`plan` 被写成 `active`，因此旧 Chicago 仍是 current context 的一部分。

### 4.2 直接修改协议：让程序从一个语义字段派生 type/status

不要继续让 Manager 同时自由填写 `memory_type` 和 `status`。新增一个更小、可验证的
`semantic_kind`，由 router 派生持久化的 `memory_type` 和 `status`：

| Manager `semantic_kind` | router 写入 `memory_type` | router 写入 `status` | 用途 |
| --- | --- | --- | --- |
| `snapshot` | `fact` | `active` | 当前属性，例如 location、贷款金额 |
| `target` | `plan` | `planned` | 目标、计划、已承诺但未完成的动作 |
| `observation` | `event` | `completed` | 一次已发生的观察值 |
| `occurrence` | `event` | `completed` | 一次购买、旅行、实体新增等事件 |
| `preference` | `preference` | `active` | 长期偏好或习惯 |
| `assistant_snapshot` | `assistant_fact` | `active` | 明确需要记住的助手提供事实 |

Manager proposal 由下面的字段组成：

```json
{
  "action": "ADD",
  "semantic_kind": "target",
  "key": "routine.weekend.wake_target",
  "value": "08:00",
  "event_date": null,
  "source": {"evidence_ids": [24]},
  "confidence": 0.95,
  "qualifier": null
}
```

`memory_type` 和 `status` 不再是模型决定的 wire fields；router 生成它们。这样既不会
把 plan 写成 active，也不能把 observation 写成 plan。

### 4.3 target / observation 的正确案例

原始表达：

```text
我希望周末 8:00 起床。上周六实际 7:30 起床。
```

正确的两条 event：

```json
{
  "action": "ADD",
  "semantic_kind": "target",
  "key": "routine.weekend.wake_target",
  "value": "08:00",
  "event_date": null,
  "source": {"evidence_ids": [0]}
}
```

```json
{
  "action": "ADD",
  "semantic_kind": "observation",
  "key": "routine.saturday.wake_observed.2023-05-20",
  "value": "07:30",
  "event_date": "2023-05-20",
  "source": {"evidence_ids": [1]}
}
```

下一周 `8:30` 是另一个 `ADD observation`，不是对上周 `7:30` 的 UPDATE。只有同一日期、
同一 occurrence 的更正才允许 UPDATE。

### 4.4 router 要做的确定性校验

在 `memory_sidecar/v3.py` 中：

1. `_parse_item()` 只接受上表六个 `semantic_kind`。
2. 由 `semantic_kind` 生成 `ParsedEvent.memory_type/status`；模型即使多传
   `memory_type/status`，也不使用它们。
3. `target` 的 key 必须以 `.target` 结束；`observation` 的 key 必须包含
   `.observed.` 和一个 date/entity/ordinal discriminator。违反时独立拒绝
   `rejected_semantic_key_shape`。
4. `occurrence` 与 `observation` 继续沿用 V3 的“不可区分实例拒绝”规则。
5. 旧数据库记录不回写；新 run 使用新 `prompt_version` 和 run ID。

必须补的测试：

```text
target -> plan/planned，模型传 active 也不能改变持久化状态
observation -> event/completed，两个日期形成两条 current occurrence
target key 没有 .target -> rejected_semantic_key_shape
observation key 没有实例 discriminator -> rejected_ambiguous_occurrence_key
```

## 5. 问题 B（记录，不改动）：模型把“新增一个成员”推导成 aggregate count UPDATE

### 5.1 轨迹里发生了什么

`69fee5aa` 的已有 state 为：

```text
owned.collection.coins.pre1920.count = "37 coins"
```

后续 evidence `e16` 的原文只说：

```text
I just added a new coin to my collection of pre-1920 American coins -
a 1915-S Barber quarter.
```

但 Manager 输出：

```text
UPDATE owned.collection.coins.pre1920.count = "38 coins"
qualifier: increasing the count from 37 to 38
```

这条 `38` 不存在于 evidence，而是模型根据 `37 + 1` 做出的推导。该现象在本文中保留，
用于后续分析 Manager 与 Answer 的能力边界；本轮不增加 count 专用 parser、引用校验、
自动拒绝或修复逻辑，也不把它作为下一轮 smoke 的通过门槛。Answer 是否根据两条事实做
`37 + 1`，仍属于后续 Answer 实验观察项。

## 6. 问题 C：金额协议既没有被模型遵守，也没有被审计成可行动信号

### 6.1 轨迹里发生了什么

`69fee5aa` 的唯一 parse reject 是：

```json
{
  "action": "ADD",
  "memory_type": "purchase",
  "key": "purchase_bike_lock_kryptonite",
  "value": {"item":"Kryptonite U-Lock","amount":80,"currency":"USD"}
}
```

`purchase` 不在 V3 允许的 memory type 中，因此被 `rejected_parse`。其他样本虽然有
`$120`、`$350`、`$1,200` 等金额，却通常落成字符串 value，例如：

```text
user.purchase.furniture.amount = "1200 USD"
user.car.service.brake.pads.cost = "$350"
```

因此 `money_coverage_missing` 为 11-31，但当前指标把“任何 user evidence 中出现美元”都
算进分母，混入了价格范围、咨询和不应持久化的提及；它不是可直接比较的错误率。

### 6.2 直接修改协议与 prompt

金额购买/支付必须使用已有的 `semantic_kind=occurrence`，而不是新造 `memory_type=purchase`：

```json
{
  "action": "ADD",
  "semantic_kind": "occurrence",
  "key": "expense.bike.lock.2023-04-27",
  "value": {
    "item": "Kryptonite U-Lock",
    "action": "purchased",
    "amount": 80,
    "currency": "USD"
  },
  "event_date": "2023-04-27",
  "source": {"evidence_ids": [10]}
}
```

`occurrence` 由 router 持久化为 `memory_type=event,status=completed`。Manager prompt 要放入
上面这个完整正例，并明确列出允许的 `semantic_kind`，不再让模型猜 `purchase` 是否是 type。

### 6.3 金额指标改为可审计的两层结果

不要恢复 V2 money repair，也不要因为一个金额漏抽丢弃同 batch 的正确事件。

新增 `sidecar_v3_money_coverage` 审计表，每个 user 货币提及一行：

```text
batch_id
evidence_id
amount_text
classification          completed_payment | quoted_price | range_or_option | unknown
covered_by_item_ordinal
route_status
reason
```

第一阶段只报告，不拦截：

1. 用冻结的简单规则把 `paid/bought/purchased/spent/was charged` 标成
   `completed_payment`；价格范围、`considering`、用户咨询标为其他类别。
2. 仅 `completed_payment` 进入正式 coverage denominator。
3. 对被引用的金额 event 检查 object 的 `amount/currency/action/item`，缺字段记
   `invalid_money_schema`。
4. 在 24 条 pilot 中报告 completed-payment coverage，不使用现在的 11-31 总数作为
   准确率判断。

等观察到稳定模型仍把已完成支付写成 string 后，再决定是否把 `invalid_money_schema` 升级为
router reject。当前不要用 key 字符串猜测“这是购买”，否则会误伤同一 evidence 中的其他事实。

## 7. 问题 D（暂不处理）：current memory 过密，但现在不应粗暴截断

每条 current record 数为 65-95 条，最终 memory JSON 为 3.8K-6.1K tokens，结合 raw tail
后 Answer prompt 为 30.9K-34.1K tokens，尚低于 80K shared budget。这里没有预算故障，不能
以“删最早 memory”修复。

真正的问题是 durable threshold 太低。例如以下内容都可能成为 current plan：

```text
正在考虑的购买选项
询问推荐的节目或工具
助手给出的一般建议
尚未承诺的价格范围
```

本问题目前只记录现象，不修改 Manager prompt、durability 策略、生命周期规则或截断逻辑。
原因是当前 memory 尚未超过 80K shared budget，且优先需要知道最终摘要是否已经能改善
Answer 上下文。后续若摘要消融显示压缩效果不足，再单独处理本问题。

### 7.1 后续候选方向（本轮不实施）

增加正负例：

```text
保留：明确当前属性、已完成动作、已承诺计划、长期偏好、明确未来安排。
不保留：单纯提问、寻求推荐、多个候选方案、"considering/might/maybe" 的购买、
助手的一般性科普或建议、没有被用户确认的助手推测。
```

不要在 router 中用 `considering`、`recommendation` 等字符串硬拒绝。那类文本判断本质上仍是
语义分类，硬编码会制造难以审计的漏记忆；V3 应让 Manager 分类、让程序验证结构。

### 7.2 后续再加度量

为每个 sample 增加：

```text
current_record_count_by_semantic_kind
current_memory_tokens
manager_items_by_semantic_kind
empty_events_batch_count
```

同时在审计报告中列出每个 plan 的 source role 和 key。这些指标留待摘要消融完成后再启用，
不作为本轮 smoke 的通过条件，也不设定武断的 record 上限。

## 8. Answer 不改，为什么 Rachel 仍值得记录

用户要求 V3 与 V1 使用相同 Answer prompt，这个约束应保持。

`830ce83f` 的 Answer 输出 `Chicago`，但 memory 已有 current `Rachel lives in the suburbs`。原因是
current memory 同时还含有一个 active plan：“准备拜访刚搬到 Chicago 的 Rachel”。在 V1 Answer
模板下，这个旧计划会成为竞争证据。

正确处理不是添加 Answer 指令，而是实施第 4 节的派生状态：该记录会变成
`plan/planned`，而 location 是 `fact/active`。这样后续强模型看到的 memory context 本身就不再
把旧旅行计划表示成 current factual location。

## 9. 具体实施顺序

### Phase 1：先改协议和 router，必须完成

修改文件：

| 文件 | 修改 |
| --- | --- |
| `memory_sidecar/v3.py` | 增加 `semantic_kind` parser；由 router 派生 type/status；实现 target/observation key shape；更新 Manager examples |
| `memory_sidecar/process_v3.py` | 记录 semantic-kind 分布；count 推导仅作为观察指标 |
| `tests/test_sidecar_v3.py` | 加入第 4 节列出的正反例；回归贷款和 Rachel UPDATE；不新增 count 行为断言 |
| `scripts/run_memory_sidecar_strong.py` | 更新 V3 prompt version，避免把新旧协议结果混在一个 run ID |

验收：只跑 `69fee5aa` 与 `dad224aa`。

```text
dad224aa: wake_target 为 plan/planned；每次 wake_observed 为 event/completed；
           不得有一个 value 同时包含 Desired 与 Recent wake-up times。

69fee5aa: 记录 37 -> 38 count 推导现象及其 evidence；本现象不作为失败条件。
           1915-S quarter 是否独立保留只做观察，不增加 count 规则。
```

### Phase 2：金额审计改造，不增加 LLM 调用

修改文件：

| 文件 | 修改 |
| --- | --- |
| `utils/store.py` | 新增 `sidecar_v3_money_coverage` 及 `record_v3_batch()` 内的同事务写入 |
| `memory_sidecar/v3.py` | completed-payment 分类与 object schema observation |
| `memory_sidecar/process_v3.py` | 写 coverage audit、汇总可解释的 denominator |
| `tests/test_sidecar_v3.py` | 已完成支付、价格范围、string money value、无效 `purchase` type |

验收：所有 `completed_payment` 都有一行 coverage 结果；不调用 repair；不会因一个金额审计失败回滚
同 batch 的其他正确 event。

### Phase 3：暂缓处理 current memory durability

不实施 current memory 生命周期、阈值、合并或截断逻辑。先完成第 11 节的 compactor
`on/off` 消融，观察最终摘要是否已经减少上下文干扰；只有效果不足时，才重新打开本阶段。

## 10. 下一轮四条 smoke 的通过门槛

1. 四条均无 `rejected_invalid_evidence`；证据 ID 仍使用 JSON 整数。
2. `852ce960`：`$400,000` current，`$350,000` superseded。
3. `830ce83f`：suburbs current；Chicago location superseded；Chicago visit 为 `plan/planned`。
4. `69fee5aa`：count 推导现象被记录，但不作为失败条件；不得因本项新增专用修复逻辑。
5. `dad224aa`：target 和 observed 至少两条独立 records，且 type/status 来自 router 的
   semantic-kind 映射。
6. `manager_completion_requests == terminal_leaf_manager_batches + truncated_parent_requests`，
   每个 completed sample `answer_completions == 1`。
7. Answer prompt 的字节内容继续与 V1 `answer_messages()` 完全相同。

只有通过这些状态断言后，才运行 24 条的 2048/4096/8192 三组 chunk-size 对照。

## 11. 最终维护摘要与复现实验

在一个 sample 的所有 Manager batch 完成、事务全部提交后，可以额外调用一次
Compactor LLM，对最终 current memory 做一次整理。它负责摘要、合并同义或重复记录，
并标记低价值记录；但这次调用不应直接覆盖 canonical memory。数据库中的原始记录、
evidence 引用、supersede 历史仍是唯一事实来源。

### 11.1 推荐的数据流

```text
Manager 多批次更新
        |
        v
SQLite canonical current memory
        |
        +--> compactor off: 直接生成 Answer context
        |
        +--> compactor on: 一次 LLM -> 最终摘要文本 -> Answer
```

Compactor 的输入只有最终 current memory，负责把它压缩为摘要；其输出不直接覆盖 canonical
memory。`compactor_on` 路径中，Answer 用 `summary_text + raw tail`，不再接收维护后的 memory
JSON。`compactor_off` 用同一 raw tail，只是把 `summary_text` 换成 current memory。若需要审计删除，
让模型同时返回候选项和原因，由程序记录为 `delete_candidate`，下一阶段再决定是否实际
删除。这样摘要错误不会破坏后续重新生成上下文的能力。

### 11.2 必须记录维护结果

每次 Compactor 调用保存一份不可变的维护快照，至少包含：

```text
compaction_run_id
question_id / sample_id
memory_snapshot_hash（current memory）
input_current_record_ids
model / prompt_version
summary_text
merged_record_ids
delete_candidate_record_ids
input_tokens / output_tokens / latency_ms
created_at
```

`memory_snapshot_hash` 用于证明摘要对应的是哪一版 current memory；
`input_current_record_ids` 用于从数据库复现同一输入。维护结果写入成功后，后续 Answer
实验可以直接读取该快照的 `summary_text`，不再重复调用 Compactor LLM。

摘要文本应保留必要的来源标记，例如：

```text
用户当前贷款预审批额度为 $400,000（来源：e13）。
Rachel 目前住在 Chicago suburbs（来源：e16）。
```

来源标记不是让 Answer prompt 发生变化，而是让摘要中的结论仍能回溯到 canonical
record 和原始 evidence。

### 11.3 消融实验设计

同一份 Manager 轨迹、同一份 canonical memory、同一 Answer 模型和同一 V1 Answer prompt
分别运行两组：

```text
A / compactor_off:
    canonical current memory -> Answer

B / compactor_on:
    canonical current memory -> Compactor（只调用一次） -> summary_text
    summary_text + 同一 raw tail -> Answer
```

B 组的摘要只生成一次，然后用同一份 `summary_text` 对所有复现实验重复回答。这样可以
把“摘要模型的随机性/额外调用”与“摘要上下文本身的效果”分开，节省实验次数。

两组保持以下条件一致：Manager 输出、chunk size、问题、Answer prompt 字节内容、Answer
模型参数和随机种子（若接口支持）。A 组使用 current memory + raw tail；B 组使用
`summary_text` + 同一 raw tail，不能把 B 组的 canonical memory 偷渡给 Answer。记录并比较：

1. Answer 准确率和关键事实遗漏率。
2. 过期事实、计划/观察混淆、金额和计数错误率。
3. Answer 输入 token、总延迟和 LLM 调用次数。
4. 摘要后仍可由 `input_current_record_ids` 找回的事实比例。

首轮可先用四条 smoke 样本验证流程，不能据此下最终准确率结论；确认快照复用无误后，
再在完整评测集上比较 A/B。若 B 的准确率提升但关键事实遗漏增加，不应直接启用删除；
优先收紧 Compactor 的合并规则并保留 canonical memory。

### 11.4 实施阶段

| 阶段 | 内容 | 是否新增 LLM 调用 |
| --- | --- | ---: |
| Phase 4a | 增加 compaction 快照表和 `memory_snapshot_hash`，保存最终摘要 | 每个 sample 1 次 |
| Phase 4b | 实现 `compactor_on/off` Answer runner；B 组只读取已保存摘要 | 复现阶段 0 次 |
| Phase 4c | 四条 smoke 做流程验收，再跑完整集 A/B 消融 | 按实验配置 |

验收要求：同一个 `compaction_run_id` 的复现实验不得再次产生 Compactor 请求；
`summary_text` 缺失或 snapshot hash 不匹配时，程序应报错而不是静默使用其他 run 的摘要。

## 12. 摘要-only 实施计划

### 12.1 接口边界

定义两种明确的 Answer 输入模式：

```text
off:
    answer_messages(memory_json=current_memory, raw_tail=raw_tail, ...)

on:
    answer_messages(memory_json=summary_text, raw_tail=raw_tail, ...)
```

两种模式继续调用同一个 V1 `answer_messages()`，不修改 Answer prompt 文本。`on` 模式的
`memory_json` 参数虽然沿用旧接口名称，内容实际上是 Compactor 生成的纯文本摘要；不再
附加原始维护记忆、候选删除列表或合并记录；raw tail 与 off 组保持一致。

### 12.2 实施步骤

1. 在 `utils/store.py` 增加 compaction snapshot 读写接口，保存 `summary_text`、输入
   record ID 和 snapshot hash；复现时按 `compaction_run_id` 精确读取。
2. 在 `memory_sidecar/compact_v3.py`（或现有 V3 runner 内的独立函数）实现一次性
   Compactor 调用。模型只读取最终 current memory，并向下游返回 `summary_text`。
3. 在 `scripts/run_memory_sidecar_strong.py` 增加 `--compactor on|off|reuse`、
   `--compaction-run-id` 和 `--replay-source-*`。`reuse` 读取已保存摘要；replay 直接恢复
   canonical V3 memory 并跳过 Manager。摘要缺失或 hash 不匹配时明确失败，不能自动重调或
   回退到 current memory。
4. 保持 Answer 调用使用 V1 prompt，并在 `on` 模式断言传入的是 summary text 而非维护记忆，
   同时 raw tail 与 `off` 模式一致。
5. 增加单元测试：摘要快照复用不产生第二次 Compactor 请求；摘要缺失/hash 不匹配会失败；
   `on` 模式 payload 只有摘要文本作为记忆上下文。
6. 先对四条 smoke 各生成一次摘要，再使用同一批 `compaction_run_id` 做 A/B Answer
   复现；确认流程后再扩展到完整评测集。

### 12.3 结果解释

此实验测量的是“最终摘要文本能否独立承载回答所需记忆”，而不是“摘要加上原始 memory
是否更好”。如果 `on` 组准确率下降，优先检查摘要是否遗漏事实或错误合并；不要通过把
canonical memory 偷渡回 Answer 来修正实验结果。

## 13. 旧口径实验记录（不用于统计）

> 本节复用了旧的 `20260827b` canonical memory，且 Answer 为 summary-only、不带 raw tail。
> 当前正式口径已改为 `memory + raw tail` 对 `summary(memory) + raw tail`；本节仅保留为历史
> 诊断，不能用于当前统计。

### 13.1 配置

使用已有的 V3 smoke canonical memory，而非重新运行 Manager：

```text
canonical memory source:
results/memory_sidecar/sidecar-v3-smoke-4-20260827b/trajectory.sqlite3

A / compactor_off run:
sidecar-v3-summary-off-4-20260827

B / compactor_on run:
sidecar-v3-summary-on-4-20260827
compaction_run_id = final-summary-v1
```

两组均从 source SQLite 恢复同一份 canonical memory。A 只调用 Answer，输入为 current
memory + raw tail；B 每条调用一次 Compactor，输入为 current memory + raw tail，保存快照后，
Answer 只使用 `summary_text`，raw tail 为 0。Answer 继续使用未改动的 V1 模板。

### 13.2 结果

| question_id | 参考答案 | A: off | B: summary-only | A 输入 tokens | B 输入 tokens | 判断 |
| --- | --- | --- | --- | ---: | ---: | --- |
| `830ce83f` | suburbs | suburbs | 未知，且只看到 Chicago visit plan | 31,338 | 624 | B 失败 |
| `852ce960` | `$400,000` | `$400,000` | `$400,000` | 30,965 | 1,121 | 两组通过 |
| `69fee5aa` | `38` | `38 coins` | `38` | 30,889 | 686 | 两组通过 |
| `dad224aa` | `7:30 am` | `8:00 am` | 未知 | 34,041 | 524 | 两组失败 |

本组仅四条，不能作为总体准确率结论，但在相同样本上，A 为 4/4，B 为 2/4。B 的 Answer
输入从总计 127,233 降至 2,955 tokens（约 -97.7%）；把 Compactor 输入也计入，B 总输入为
39,115 tokens，仍比 A 少约 69.3%，但调用数从 4 次增至 8 次。

对照组执行期间曾在前一次 runner 尚未退出时续跑，SQLite 因此保留了 `69fee5aa`、
`dad224aa` 的重复 completed attempt。表格和上述 token/call 对比按每个 question 的最新
completed attempt 计算，即逻辑上的四个 A 样本；原始 attempt 历史保留在数据库中以便审计。

### 13.3 失败定位

`830ce83f` 的 canonical memory 明确包含：

```text
user.friend.rachel.location = "Rachel lives in the suburbs."  (active)
```

但 B 保存的 `summary_text` 只保留了“visiting friend Rachel in Chicago”的旧计划，遗漏了
最新地址。因此问题是 Compactor 的选择/合并错误，不是 router 没有写入新 location。

`dad224aa` 的 canonical memory 明确包含：

```text
Desired wake-up time: 8:00 am.
Recent wake-up times: 7:30 am, 9:30 am, 8:30 am.
```

B 摘要只保留“8:00 am bus”等背景，也遗漏了整条周末起床记录。A 输出 `8:00 am` 与参考
答案 `7:30 am` 不一致，则是现有 target/observation 混写的已知问题；B 进一步把相关记录
完全丢失，不能视为改善。

### 13.4 结论

摘要快照和复用机制工作正常，且显著降低了 Answer 输入；但当前通用 Compactor 不可直接
启用为唯一 memory context。下一步应先改 Compactor 的保留契约和审计指标，例如要求保留
每个 current key 的最终值、日期与 source record ID，并对摘要遗漏的 active record 记录
coverage。不要通过把 canonical memory 或 raw tail 回传给 B 组来掩盖这个失败。

## 14. 2026-08-27 正式结果：重跑 memory 后比较

### 14.1 配置与有效性

本轮使用当前代码重新执行四条样本的全部 Manager batch，并发数为 2：

```text
summary-on source run:
sidecar-v3-final-memory-summary-on-4-20260827-c2
compaction_run_id = final-memory-summary-c2

summary-off replay run:
sidecar-v3-final-memory-summary-off-4-20260827-c2
```

每条 summary-on sample 均为 `14 Manager + 1 Compactor + 1 Answer`，共 56 个 Manager
batch、64 次调用。Compactor 只读取新生成的 canonical current memory；off/on 两组 Answer
都接收同一份 raw tail，区别仅是 memory 使用原始 JSON 还是 `summary(memory)`。两组使用未
改动的 V1 Answer prompt。off 组不重新调用 Manager，只从 source run 恢复 canonical memory。

### 14.2 A/B 结果

| question_id | 参考答案 | off：memory + raw tail | on：summary(memory) + raw tail | off Answer 输入 | on Answer 输入 | 判断 |
| --- | --- | --- | --- | ---: | ---: | --- |
| `830ce83f` | suburbs | Chicago | suburbs | 32,494 | 26,423 | on 正确 |
| `852ce960` | `$400,000` | `$400,000` | `$400,000` | 31,332 | 27,663 | 两组正确 |
| `69fee5aa` | `38` | 37 coins | 37 | 30,812 | 27,045 | 两组失败 |
| `dad224aa` | `7:30 am` | 8:00 am | 7:30 am | 34,542 | 28,359 | on 正确 |

off 为 1/4，on 为 3/4。Answer 输入总量从 off 的 129,180 tokens 降至 on 的 109,490 tokens，
下降约 15.2%；raw tail 占主要输入，因此不会出现不带 raw tail 时的 97% 降幅。

on 组完整流水线总输入为 799,528 tokens，其中包含 Manager、Compactor 和 Answer；off
重放组只有 4 次 Answer、总输入 129,180 tokens。该差异反映了“是否支付一次摘要生成成本”，
不能把两者混成单次 Answer 成本。

### 14.3 归因

1. `830ce83f`：当前 Manager 已写入 suburbs location 和 Chicago visit plan；off 被旧计划
   干扰回答 Chicago，summary(memory) 去除了冲突计划后回答正确。
2. `852ce960`：两组都能从 memory/raw tail 得到 `$400,000`，摘要没有损失该事实。
3. `69fee5aa`：当前 Manager 仍保留 `37 coins`，本轮按约定不处理 count 推导问题；摘要
   也忠实保留 37，因此两组都失败。
4. `dad224aa`：当前 Manager 已有 weekend wake observation；summary 保留了 `7:30 am`，
   on 正确回答，说明摘要可以改善 target/observation 混写造成的干扰。

### 14.4 当前结论

在“summary(memory) + 同一 raw tail”的正式口径下，摘要组从 1/4 提升到 3/4，Answer
输入减少约 15.2%。这说明最终摘要有潜力作为 memory 的压缩投影，但四条样本仍不足以证明
全面收益；下一步应扩大评测集，并继续记录摘要是否遗漏 active record、是否错误合并地点/时间/
数值。摘要成本也必须单独计入端到端预算。

## 15. 2026-08-27 Chunk 2048 对照

### 15.1 配置

与第 14 节保持相同的 Answer、Compactor 和 raw tail 口径，只把 Manager chunk 从 8192 改为
2048；sample 内仍按 evidence 时间顺序串行，sample 间并发为 2。

```text
summary-on source run:
sidecar-v3-memory-summary-on-4-20260827-c2048
compaction_run_id = final-memory-summary-c2048

summary-off replay run:
sidecar-v3-memory-summary-off-4-20260827-c2048
```

### 15.2 A/B 结果

| question_id | 参考答案 | off：memory + raw tail | on：summary(memory) + raw tail | 判断 |
| --- | --- | --- | --- | --- |
| `830ce83f` | suburbs | suburbs | suburbs | 两组正确 |
| `852ce960` | `$400,000` | `$400,000` | `$400,000` | 两组正确 |
| `69fee5aa` | `38` | 38 | 38 | 见 15.3 限制 |
| `dad224aa` | `7:30 am` | `8:30 am` | `7:30 am` | 仅 on 正确 |

表面准确率为 off `3/4`、on `4/4`。Answer 输入从 141,105 降至 109,282 tokens（约 -22.6%）；
相比 8192 的 -15.2%，2048 的 memory JSON 更大，因而摘要节省的 Answer 输入更多。

### 15.3 轨迹限制与解释

2048 增加了 Manager 调用：四条分别为 62、62、55、62 个 batch，共 241 个 Manager batch；
加上 4 次 Compactor 和 4 次 Answer，共 249 次调用。完整 summary-on 的总输入为 1,995,137
tokens，约为 8192 正式 run 的 2.5 倍（8192 为 799,528）。因此 2048 不能仅因 4 条答案更好
就视为优选 arm。

`69fee5aa` 的 38 不是可接受的 protocol 改善。batch 38 的 cited user evidence 只说新增
`1915-S Barber quarter`，Manager 却 UPDATE 为“total of 38”并标记 `explicit_statement`。它是
`37 + 1` 的隐式算术，仍属于第 5 节记录但不修复的问题。因此按严格 evidence-only 口径，
该样本不应计为 2048 相对 8192 的有效胜利。

`dad224aa` 中，2048 memory 同时有 `7:30`、`9:30` 和后续 `8:30` 的周六观察；off 回答最新的
`8:30`，与数据集参考答案 `7:30` 不同。Compactor 摘要只保留 wake target，并未保留这些具体
观察；on 回答 7:30 是因为 raw tail 仍在，且摘要移除了后续 8:30 观察造成的竞争。这是去噪
效果的信号，但不是摘要 coverage 正确的证据。

`830ce83f` 中 2048 Manager 明确记录了 Rachel moved back to suburbs；两组均正确。该变化说明
更小 chunk 有助于把晚到的更新独立写入，但还不能与模型输出随机性完全区分，需要在更多样本
上重复。

### 15.4 当前判断

2048 值得保留为实验 arm：它改善了更新的隔离与 Answer 上下文压缩效果。但其成本约为 8192
的 2.5 倍，且更小 chunk 使 current record 数上升（64->148、112->140、73->107、91->108），
加重了 Compactor 的 coverage 风险。下一轮应以严格 evidence-only 指标单独计数，并对 2048、
8192 在同一批完整评测样本上对比，而非用此 4 条作最终选择。
