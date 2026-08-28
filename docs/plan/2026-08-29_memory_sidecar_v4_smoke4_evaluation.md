# V4 修复后四样本 Smoke 评测

## 配置

```text
run_id: sidecar-v4-e2e-smoke4-20260829
chunk: 2048 tokens
Manager: Qwen qwen3.8-27b, stateless minimal-claim prompt
Manager max output: 1024 tokens
Answer: 原 V1 Answer prompt
Context: graph-all + V1 raw tail
Judge: deepseek-v4-pro / LongMemEval rubric
```

轨迹数据库：

```text
results/memory_sidecar/sidecar-v4-e2e-smoke4-20260829/trajectory.sqlite3
```

## 结果

| question_id | 题型 | Gold | Answer 关键结论 | Judge |
| --- | --- | --- | --- | --- |
| `gpt4_d84a3211` | multi-session 金额聚合 | `$185` | `$25 + $40 + $120 = $185` | yes |
| `67e0d0f2` | multi-session provider 计数 | `20` | `8 edX + 12 Coursera = 20` | yes |
| `dad224aa` | knowledge-update wake time | `7:30 am` | 同时展示 `7:30/8:00/8:30/9:30`，明确包含 `7:30` | yes |
| `gpt4_2ba83207` | multi-session 商店金额排序 | Thrive Market | Walmart | no |

DeepSeek judge 准确率为 `3/4 = 75%`。这只是 smoke，不能作为总体效果结论。

## 成本与噪声变化

| 样本 | Manager input | Manager output | Answer input | raw claims |
| --- | ---:| ---:| ---:| ---:|
| `gpt4_d84a3211` | 127,139 | 14,198 | 28,322 | 9 |
| `67e0d0f2` | 129,240 | 14,939 | 36,340 | 2 |
| `dad224aa` | 129,049 | 14,496 | 28,374 | 3 |
| `gpt4_2ba83207` | 129,520 | 16,170 | 31,664 | 2 |
| 合计 | 514,948 | 59,803 | 124,700 | 16 |

Qwen 总计 `700,179` token；DeepSeek judge 共 `1,361` token。

旧版两个样本的 Manager input 分别为 `2,044,329` 和 `997,033`；本次对应两个样本为 `127,139` 和 `129,240`。无状态 Manager 消除了随着图状态增长而出现的输入膨胀。第一个样本的 raw claim 也由旧版 `83` 降为 `9`，第二个由 `394` 降为 `2`。

## 已验证修复

1. 重复 occurrence 的后续金额字段不再被直接丢弃。头盔先被记录为无金额购买，后续 `$120` claim 通过 `deduplicated_merged` 回填，最终正确得到 `$185`。
2. `has_completed -> COMPLETED` 使 edX 的 `8` 和 Coursera 的 `12` 成为独立 typed edge，不再被 provider 覆盖。
3. Manager 不携带全图 state，单 batch 输入稳定在约 1.4k-2.3k token；raw claim 显著下降。
4. target、observation 与 preference 均同时保留，不再由路由直接覆盖。但 Answer 仍可能按“最近 observation”解释问题，需在后续 query projection 中验证。

## 新发现的问题

### P0：graph-all 仍使 Answer 漏掉已经提取到的关键事实

失败样本 `gpt4_2ba83207` 的图中已经存在：

```text
user --PURCHASED--> organic and sustainable products
amount=150 USD; provider=thrive market; time=last month
```

还存在一个同来源、对象文本略不同的 `$150 Thrive Market` edge。Answer context 有 241 行、约 31.7k input token，最终只比较了 Walmart `$120`、Trader Joe's `$80` 和 Publix `$60`，遗漏 Thrive `$150`，回答 Walmart。

根因不是金额没有抽取，而是：

1. 同一 purchase 被模型以不同 `object_text` 重复提取，provider/amount/time 相同却没有合并；
2. graph-all 没有受控的聚合/筛选投影，Answer 在大而杂的上下文中自行挑选金额 occurrence；
3. 当前相对时间 `last month`、`last week` 仍是 raw 文本，尚未解析为可确定比较的 interval。

## 24 条 pilot 前的 V4 修复项

1. **同 occurrence 的对象规范化/同 evidence 合并**：若 `subject + predicate + provider + amount + normalized time + evidence source` 相同，允许将 `object` 的 provider 后缀或轻微表述差异合并；保留全部 object alias 和 source refs，不能只靠 object 文本判断是新购买。
2. **相对时间解析**：以 evidence session date 为基准，解析 `last week`、`last month`、`the week before last`、`last Saturday` 等为 day/interval；解析失败必须标为 unknown，不得伪造日期。
3. **受控 numeric projection**：先仍保留 graph-all 对照，但新增只读程序 projection，对 typed purchase edge 输出 `sum/rank` 明细和 `input_edge_ids`。它不能依赖 gold，也不能由 Manager 生成 aggregate claim。
4. **query-specific projection 延后单独对照**：在 graph-all 和 numeric projection 验证后，再引入问题解析/子图筛选，避免把 representation、聚合和 query parser 混为一个变量。

## 修复实现状态（2026-08-29）

上述前三项已经落地并有离线回归覆盖：

1. 同证据，或同一 session 内相邻（unit ordinal 差值不超过 32）的 evidence，若 subject/predicate/provider、已解析时间、amount/count 等数值特征一致，且 `object` 仅存在 `from/at <provider>` 后缀差异，occurrence 会合并；其余情况保持分开，避免误并两次真实事件。
2. `last month`、`last week`、`the week before last`、`last Saturday` 以 evidence 所在 session 日期为基准，规范化为 day 或 interval，并保留 `relative_to`；未覆盖表达仍保留为 `unparsed`。
3. `render_v4_numeric_projection` 会输出所有 typed numeric edge 及其确定性 `SUM` / `SUM_COUNT`，每个 aggregate 都记录输入 edge ID。runner 与 replay 脚本新增 `--projection numeric-all`，它与 `graph-all` 使用相同 raw tail，但不混入整个图谱。

这不是线上效果结论。原有 4 条运行使用的是旧 router/time parser，必须重新构图后，分别运行 `graph-all` 与 `numeric-all` 才能比较修复后的准确率和 token 成本。

## 修复后四条端到端结果（2026-08-29）

重构运行目录：`results/memory_sidecar/sidecar-v4-e2e-smoke4-rebuild-20260829/`。四条均使用 `chunk=2048`、相同的 V1 Answer 提示词、16K raw tail，并重新执行 V4 Manager。四条状态均为 completed，Manager 调用数分别为 62、65、63、64。

| question_id | graph-all | numeric-all | 观察 |
|---|---:|---:|---|
| `gpt4_d84a3211` | yes | yes | 自行车费用题，两组均答 `$185` |
| `67e0d0f2` | yes | no | 课程数量题，numeric-only 没有保留课程事实 |
| `dad224aa` | yes | no | 起床时间题，numeric-only 没有保留时间/计划关系 |
| `gpt4_2ba83207` | no | no | grocery provider 聚合仍把 Walmart 判为最高，Thrive 两条 `$150` 来源不同而未触发同 evidence alias merge |

DeepSeek `deepseek-v4-pro` 按官方 rubric 评测：`graph-all=3/4 (0.75)`，`numeric-all=1/4 (0.25)`。因此 numeric projection 不能替代全图上下文；后续 V5 只能在问题类型和上下文中都检测到“数值聚合意图”时启用，并保留非数值事实的最小投影。

本次 numeric 回放曾以两个进程并发写同一 SQLite，首轮出现一次 `database is locked`。已将 `TrajectoryStore` 的连接 timeout/busy timeout 提高到 60 秒；补跑后四条 projection 均成功落库。并发实验应继续使用不同 sample 分片，避免多个 worker 写同一 sample。

## 24 条 pilot 结果（2026-08-29）

在上述优化后，24 条冻结 pilot 使用 `chunk=2048`、V1 Answer prompt、16K raw tail 和 `graph-all` 重新运行。为控制执行时间，分成两个各 12 条的独立 SQLite shard 并发运行，目录为：

- `results/memory_sidecar/sidecar-v4-pilot24-shard1-20260829/`
- `results/memory_sidecar/sidecar-v4-pilot24-shard2-20260829/`

两路共 24/24 sample completed，无 sample 级失败。DeepSeek `deepseek-v4-pro` judge 结果为 **12/24（50.00%）**，按题型统计如下：

| question_type | correct | total | accuracy |
|---|---:|---:|---:|
| `knowledge-update` | 2 | 4 | 50.00% |
| `multi-session` | 4 | 10 | 40.00% |
| `single-session-assistant` | 2 | 2 | 100.00% |
| `single-session-preference` | 1 | 4 | 25.00% |
| `single-session-user` | 2 | 2 | 100.00% |
| `temporal-reasoning` | 1 | 2 | 50.00% |

与 V3 2048 `summary-off` 的 `12/24 (50.00%)` 相比，V4 当前是持平，不足以证明图谱表示带来准确率提升。优化确实降低了 Manager 状态输入膨胀，并修复了相对时间解析和部分 occurrence 合并，但 Answer 仍会在全图中混淆 provider、计划/观察与真实购买。

因此下一步不应直接扩大到 120 条。应先针对 24 条失败轨迹增加 query-aware 的程序投影：数值比较只保留 `PURCHASED` occurrence，时间问题同时保留 `TARGET/PREFERS/OBSERVED`，并将无法可靠归属 provider 的金额显式标为 unknown，禁止 Answer 把它归因到邻近商店。该投影应作为 V5 独立实验臂，继续保留 V4 graph-all 对照。
