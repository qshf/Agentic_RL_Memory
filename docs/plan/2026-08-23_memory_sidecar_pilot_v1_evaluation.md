# Memory Sidecar-Strong V1 Pilot 对比报告

## 1. 实验对象

本报告比较同一 24 条冻结样本上的两个实验：

| 实验 | run | 记忆方法 | 最终回答模型 | judge |
| --- | --- | --- | --- | --- |
| Rolling V1 baseline | `rolling-summary-eval120-v1-atomic-c2` | Rolling Summary V1 | **Qwen `qwen3.8-27b`** | DeepSeek `deepseek-v4-pro` |
| Sidecar-Strong pilot | `sidecar-strong-pilot-24-v1-c2-selected` | Qwen 逐 chunk 输出 `ADD/UPDATE/NOOP` 结构化记忆事件 | **Qwen `qwen3.8-27b`** | DeepSeek `deepseek-v4-pro` |

两组使用相同的 LongMemEval-S 原始数据、相同问题和相同的 24 条 pilot manifest：

`data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv`

Sidecar 参数为：chunk `2048` tokens、manager context `12288`、active memory `8192`、update ledger `2048`、recent tail `16384`、样本并发 `2`。本报告严格比较同一个 Qwen `qwen3.8-27b` 在两种记忆方案下生成的最终答案：

```text
Rolling Summary V1 → Qwen final answer
Memory Sidecar-Strong → Qwen final answer
```

DeepSeek `deepseek-v4-pro` 只负责对两组最终答案执行同一 judge，不参与方案生成。

注意：Rolling baseline 评估文档另外做过一次“上下文充分样本的 DeepSeek 重答”实验。那是 baseline 的诊断实验，不是本报告中的 Rolling-Qwen 与 Sidecar-Qwen 对照；本报告不把那次 DeepSeek 重答结果混入准确率或上下文归因。

## 2. 准确率

### 2.1 总体结果

| 实验 | 正确 | 总数 | 准确率 |
| --- | ---: | ---: | ---: |
| Rolling V1 | 6 | 24 | 25.00% |
| Sidecar-Strong | 10 | 24 | **41.67%** |
| 变化 | +4 | 24 | **+16.67 个百分点** |

Sidecar-Strong 在这组 pilot 上比 Rolling V1 多答对 4 条，但 24 条样本只用于方向判断，不能替代 120 条或全量 benchmark。

### 2.2 按预设分组

| 样本组 | Rolling V1 | Sidecar-Strong | 变化 |
| --- | ---: | ---: | ---: |
| 上下文充分但答案错误（12 条） | 0/12（0.00%） | **5/12（41.67%）** | +5 |
| 明确 summary omission（6 条） | 0/6（0.00%） | **1/6（16.67%）** | +1 |
| V1 原本正确（6 条） | 6/6（100.00%） | 4/6（66.67%） | **-2** |

这三组标签沿用 pilot manifest 中基于 Rolling baseline 的预先诊断，只用于分层观察，不能直接当作 Sidecar 的上下文充分性标签。Sidecar 自身的上下文归因见第 2.4 节。原本正确样本出现 2 条回归，说明结构化记忆当前仍有抽取、状态表达或最终证据选择问题。

### 2.3 逐题迁移

Rolling 错误、Sidecar 正确的 6 条：

```text
4f54b7c9, 67e0d0f2, 69fee5aa, 852ce960, d24813b1, dad224aa
```

Rolling 正确、Sidecar 错误的 2 条：

```text
6cb6f249, gpt4_59149c77
```

两组都正确 4 条，都错误 12 条。Sidecar 没有造成“原本错误全部修复”，而是部分改善了旧值选择、跨 session 汇总和偏好/事实提取。

### 2.4 Sidecar-Qwen 错误的上下文归因

本节只检查 Sidecar-Qwen 最终实际收到的 `sidecar_memory + recent_tail`。如果所需事实不在其中，才计为上下文失败；事实已经存在但 Qwen 没有计算、排序、选择或解释正确，记为“答案模型错误”；数据集/gold 或 judge 的口径疑点单独记录。

Sidecar 的 14 条 judge=no 归因如下：

| 归类 | 数量 | question_id |
| --- | ---: | --- |
| 上下文缺失 | **8/14** | `gpt4_d84a3211`, `bf659f65`, `ccb36322`, `3a704032`, `dd2973ad`, `gpt4_d6585ce9`, `81507db6`, `6cb6f249` |
| 上下文充分，Qwen 最终回答错误 | 5/14 | `gpt4_2ba83207`, `0edc2aef`, `09d032c9`, `778164c6`, `gpt4_59149c77` |
| 数据集/gold 口径疑点 | 1/14 | `0a995998` |

因此，按本报告口径，Sidecar 的上下文失败为 **8/24（33.33%）**，或 **8/14（57.14% of Sidecar judge=no）**。这个数字只描述 Sidecar-Qwen 的最终上下文，不使用 baseline 文档中“用 DeepSeek 重答”的诊断结果。

### 2.4.1 数据库逐题 provenance 审计

已对 Sidecar `sample_id`、对应 baseline `states.raw_text`、`sidecar_events` 和最终 `sidecar_memory` 做只读回放。下面的 step 是 baseline 清洗后的 `states` 行；event 是 Sidecar manager 的 chunk 事件。重点不是看原始未清洗索引，而是确认该事实是否进入 manager 输入、manager 输出了什么、规则路由是否落库。

| question_id | 具体源消息 | manager 操作 | 数据库结果 | 结论 |
| --- | --- | --- | --- | --- |
| `gpt4_d84a3211` | `answer_2880eb6c_1 message[6]`：Bell Zephyr helmet **$120**；baseline step `118` | event `15`，chunk `113-122`，输出 `ADD user.bike.mileage.current` | `applied`；既有 `user.gear.helmet` 仍只有“Bell Zephyr”，没有 `$120` | chunk 中事实被其他事实抢占，形成属性级 provenance 缺失 |
| `bf659f65` | `answer_7726e7e9_3 message[0]`：Tame Impala vinyl；step `329` | event `39` 输出 Tame 事件，但只写“签名 vinyl”；Whiskey EP 在 event `40`，Billie 下载在 event `4` | 三个实体都有记录，但 Tame 记录没有 `purchased/downloaded` 动作属性 | 不是实体完全缺失，而是计数所需的购买/下载语义缺失 |
| `ccb36322` | `f1fbb330 message[2]`：使用 Spotify；step `45` | event `7`，chunk `40-45`，输出 `ADD user.event.concert.attended=The 1975` | `applied`；没有 Spotify 记录 | 同一 chunk 的后一条事实未被抽取 |
| `3a704032` | `c2204106_3 message[2]`：snake plant；step `141`；后续 `c2204106_1 message[0/4]` 再次确认 | event `18/21/22` 分别输出 `ADD user.plants.owned` | 三次均为 `rejected_add_conflict`，原因是 active key 已存在，最终仍为“peace lily and succulent” | manager 没按协议输出 `UPDATE`，规则层正确拒绝后续 ADD，导致记忆状态不更新 |
| `dd2973ad` | `f9de4602_2 message[0]`：doctor appointment；step `250`；`f9de4602_1 message[0]`：2 AM；step `480` | event `31` 输出 cholesterol；event `58` 输出 data-analysis plan | 两个事件均 `applied`，但没有 doctor/bedtime 事实或关系 | 跨 session 关系和时间事实均被 chunk 中其他事实覆盖 |
| `gpt4_d6585ce9` | `f999b05c_3 message[0]`：Queen + parents + “last Saturday”；step `235` | event `35` 输出 `user.event.queen_concert` | `applied`；value 有 parents，但 `event_date=null` | 人物 provenance 保留，relative-date provenance 丢失，无法回答“last Saturday” |
| `81507db6` | `da3c1266_2 message[0]`：Rachel 的 graduation；step `314` | event `40` 输出 digital-marketing certification `UPDATE` | `applied`；Emma、Alex、Jack 有记录，Rachel 没有 | chunk 中 graduation 事实被无关计划更新占用 |
| `6cb6f249` | `a4204937_2 message[0]`：10-day break；step `248`；`a4204937_1 message[0]`：week-long break；step `260` | event `32/34` 均输出 journaling 相关记录 | `applied`，但两个 break duration 都没有记录 | 两个累计事件均被同 chunk 的 journaling 主题覆盖 |

这 8 条中，**5 条是“一个 chunk 只落一个事件，其他事实没有进入 memory”**（`gpt4_d84a3211`、`ccb36322`、`dd2973ad`、`81507db6`、`6cb6f249`）；`3a704032` 是 **ADD/UPDATE 协议与路由冲突**；`bf659f65` 和 `gpt4_d6585ce9` 是 **事件落库但属性/provenance 不完整**。因此第一修复对象是 memory 事件结构和 provenance，不是 Answer Model。

审计原始产物：

`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/context_failure_audit.{json,md}`

审计脚本：`scripts/audit_sidecar_context_failures.py`。脚本只读数据库，不修改实验结果。

修复顺序固定为：

1. 允许一个 manager chunk 返回**多个结构化事件**，并在 `sidecar_events` 中保存事件批次及每个子事件的 route 结果，避免一条事实覆盖同 chunk 其他事实。
2. 对同一 key 的新增属性使用显式 `UPDATE` 或字段级合并，不能让 `ADD` 冲突后事实永久丢失；保留 `rejected_add_conflict` 作为审计记录。
3. 强制事件 value 保存计数所需的动作、金额、日期、相对时间和关系 provenance，例如 `purchased/downloaded`、`$120`、`last Saturday`、`with parents`。
4. 修复后只重跑这 8 条样本，重新检查数据库 sufficiency，再决定是否修改提示词或 Answer Model。

上下文缺失的具体类型：

- `gpt4_d84a3211`：保留了链条 `$25` 和车灯 `$40`，但头盔价格 `$120` 没有保留；属于“实体在、关键数值属性缺失”。
- `bf659f65`：保留 Billie Eilish 专辑和 Whiskey Wanderers EP，漏掉 Tame Impala 的 vinyl；属于早期事件遗漏和枚举不完整。
- `ccb36322`：答案 session 中的 Spotify 没有进入最终 memory 或 tail；属于早期事实完全遗漏。
- `3a704032`：保留 peace lily 和 succulent，漏掉从姐姐处获得的 snake plant；属于实体事实遗漏。
- `dd2973ad`：医生预约与“前一天凌晨 2 点睡觉”跨 session 的事实及关系没有进入最终 memory；属于关系/provenance 遗漏。
- `gpt4_d6585ce9`：保留 Queen 与父母、Brooklyn festival 与朋友，但 Queen 事件没有日期；“last Saturday”无法可靠定位，属于时间 provenance 缺失。
- `81507db6`：保留 Emma、Alex 和 Jack 未参加，漏掉 Rachel 的毕业典礼；属于列表成员遗漏。
- `6cb6f249`：没有保留 10 天和 7 天两次 social media break；属于多事件累计信息遗漏。

不计入上下文失败的错误：

- `gpt4_2ba83207` 的 Thrive Market `$150`、Walmart `$120`、Trader Joe's `$80`、Publix `$60` 均已在 memory 中，错误是时间范围/比较选择。
- `0edc2aef` 已保留 Miami 酒店偏好；Qwen 错误声称没有偏好信息。
- `09d032c9` 已保留 portable power bank 和 wireless charging pad；Qwen 没有使用已有事实。
- `778164c6` 的 memory/tail 同时包含 Escovitch Fish 和 Grilled Snapper with Mango Salsa；Qwen 选择错误菜名。
- `gpt4_59149c77` 已保留 MoMA 与 Ancient Civilizations exhibit 的日期；Qwen 将日期差算错。
- `0a995998` 的原始标注有 3 条 `has_answer` 消息，但 boots 的取回/退换描述指向同一物品；memory 有 blazer 和 boots 两个实体，`gold=3` 存在计数口径疑点，暂不计入上下文失败。

### 2.5 DeepSeek Answer Model 替换对比（仅上下文不缺失样本）

这是一个独立的答案模型诊断，不替换第 2.1 节的主结果。选取双方在第 2.4 节检查后都被认为上下文充分的 14 条样本；Rolling 和 Sidecar 使用同一 `answer_messages` 提示词、同一问题日期、同一 DeepSeek `deepseek-v4-pro` 生成答案，并统一使用 16K recent tail。两组唯一变化是 memory 表示，随后仍使用同一 DeepSeek judge 判定。

| 方案（DeepSeek 生成答案） | 正确 | 总数 | 准确率 |
| --- | ---: | ---: | ---: |
| Rolling V1 | 6 | 14 | 42.86% |
| Sidecar-Strong | 9 | 14 | **64.29%** |
| 变化 | +3 | 14 | **+21.43 个百分点** |

迁移关系：两组同时正确 6 条，同时错误 5 条；Rolling 错误而 Sidecar 正确 3 条（`d24813b1`、`852ce960`、`dad224aa`）；Sidecar 没有出现“Rolling 正确而 Sidecar 错误”的样本。该结果说明在证据已经存在时，Sidecar 的记忆表示对 DeepSeek 答案模型更友好，但不能证明 Sidecar 独立提升了记忆召回，也不能与第 2.1 节的 Qwen 结果混为一谈。由于答案模型和 judge 使用同一 DeepSeek 服务，这项结果仅作方向性诊断，后续应使用独立 judge 或人工复核。

## 3. 上下文和成本指标

以下统计来自两组 SQLite `samples` 记录，均只统计这 24 条样本。

| 指标 | Rolling V1 | Sidecar-Strong | 变化/说明 |
| --- | ---: | ---: | --- |
| 平均 full history tokens | 108,611 | 108,611 | 相同输入历史 |
| 平均最终 answer input tokens | 31,300 | **20,010** | 减少 35.1% |
| 平均 memory 表示 tokens | 4,617 summary | **3,446 sidecar memory** | Sidecar 更短 |
| 平均 recent tail tokens | 26,316 | **16,384** | Sidecar pilot 固定 16K |
| 总 API input tokens | 2,737,502 | 6,004,238 | Sidecar 约 2.19 倍 |
| 总 API output tokens | 112,515 | 227,865 | Sidecar 约 2.03 倍 |
| 模型调用次数 | 48 | **1,502** | Sidecar 逐 chunk 调用 |
| 记录总耗时 | 6,313.9 秒 | 10,616.9 秒 | Sidecar 约 1.68 倍 |

Sidecar 的最终 Answer 输入确实减少约 35%，达到 pilot 计划中的“至少减少 30%”目标；但 manager 每个 chunk 都调用一次强模型，导致总 token、调用次数和耗时显著上升。当前 Strong manager 不能代表小模型的成本表现。

## 4. 运行完整性

Sidecar run：

- 24/24 completed；无失败样本；
- 1,502 次调用全部完成；
- 结构化路由：`applied=1100`、`noop=368`、`rejected_add_conflict=9`、`deduplicated=1`；
- SQLite 保存 `sidecar_events`、`sidecar_memory`、状态快照和模型调用轨迹；
- 最终回答从数据库 `sidecar_memory(sample_id)` 恢复记忆后生成。

因此当前结果不是 runner、数据库或服务失败造成的。

## 5. 结论

### 5.1 已验证的部分

1. 在同一 24 条样本、Qwen 生成答案、DeepSeek judge 的条件下，Sidecar 总体准确率为 41.67%，Rolling baseline 子集为 25.00%。
2. Sidecar 最终输入减少约 35%，但不能据此证明准确率提升来自 memory recall；需要按第 2.4 节的上下文充分性单独归因。
3. SQLite 追加事件和当前记忆库可以支撑完整回放，未出现存储层失败。
4. 在双方上下文均充分的 14 条样本上，将答案模型统一替换为 DeepSeek 后，Rolling 为 6/14、Sidecar 为 9/14；这是答案模型敏感性诊断，不与 Qwen 主结果合并。

### 5.2 尚未解决的问题

1. Sidecar 对 summary omission 的修复有限（1/6），说明仅把每个 chunk 交给 manager 并不能保证早期关键事实进入最终 memory。
2. V1 正确样本回归 2 条，说明结构化事件可能存在漏抽取、错误 key、状态冲突或 Answer Model 不会正确使用 compact memory 的问题。
3. Strong manager 的调用成本过高：24 条样本 1,502 次调用，不能作为可部署方案。

### 5.3 下一步建议

当前不应直接进入 RL。建议按以下顺序继续：

1. 对第 2.4 节的 8 条上下文失败样本做逐题 memory sufficiency 和 event provenance 检查，优先修复记忆结构，而不是修改 Answer Model。
2. 对 2 条回归样本和 5 条仍错误的 context-sufficient 样本检查事件是否被拒绝、覆盖或 compact projection 丢字段。
3. 保持 schema、validator、Answer prompt 和 tail 不变，换 1B/2B manager 测试成本与准确率；先串行验证，再测并发。
4. 若 Small manager 保持 evidence sufficiency，再扩展到完整 120 条；若仍有明显回归，先修正更新规则和答案证据选择，不引入 compactor 或 RL。

## 6. 可复核文件

- Pilot manifest：`data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv`
- Sidecar run summary：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/run_summary.json`
- Sidecar hypotheses：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/hypotheses.jsonl`
- Sidecar judge：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/deepseek_judgments.jsonl`
- Sidecar judge summary：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/deepseek_judgments_summary.json`
- Sidecar trajectory：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/trajectory.sqlite3`
- DeepSeek 替换答案：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/deepseek_context_sufficient_{rolling,sidecar}.jsonl`
- DeepSeek 替换答案 judge：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/deepseek_context_sufficient_{rolling,sidecar}_judgments_summary.json`
- DeepSeek 替换脚本：`scripts/compare_deepseek_context_sufficient.py`
- Rolling V1 baseline evaluation：`docs/plan/2026-08-21_rolling_summary_baseline_v1_evaluation.md`

## 7. 可比性限制

这不是严格的因果对照。第 2.1 节主对比沿用历史 Rolling V1 run，该 run 的 raw tail 没有预先固定独立上限，而 Sidecar pilot 固定为 16K tokens，因此其 token 和准确率差异只能作为架构方向信号。第 2.5 节的 DeepSeek 替换诊断已对两种方案统一使用同一份 16K tail snapshot，但样本数只有 14 条，且答案模型与 judge 相同，仍不能作为最终结论。后续 120 条正式对照必须从同一 16K tail snapshot 重新运行两种方案。
