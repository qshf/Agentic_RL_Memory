# Memory Sidecar V3 / V4 全量 120 条实验报告

## 1. 实验目的

本实验在同一份 LongMemEval-S 120 条冻结样本上，对比：

1. Rolling Summary baseline；
2. V3 `chunk=2048`，结构化 memory + raw tail；
3. V4 `chunk=2048`，图谱提取、程序规范化/去重、`graph-all + raw tail`。

目标是判断图谱化 memory 是否能够提升上下文管理和最终回答准确率，并通过 SQLite 轨迹确认：

- Manager 是否完整处理历史证据；
- 程序规范化、路由、去重是否稳定；
- Answer 看到的上下文是否足以支持正确回答；
- 失败是否来自实现，还是来自上游模型接口临时错误。

## 2. 实验配置

| 项目 | Rolling Summary | V3 | V4 |
| --- | --- | --- | --- |
| 数据集 | LongMemEval-S 120 | 同左 | 同左 |
| Manager 模型 | Qwen `qwen3.8-27b` | Qwen `qwen3.8-27b` | Qwen `qwen3.8-27b` |
| Answer 模型 | Qwen，原 V1 prompt | Qwen，原 V1 prompt | Qwen，原 V1 prompt |
| Judge | DeepSeek `deepseek-v4-pro` | 同左 | 同左 |
| Judge 版本 | `longmemeval-official-rubric-deepseek-v2` | 同左 | 同左 |
| chunk | rolling trigger 80K | 2,048 tokens | 2,048 tokens |
| Answer 上下文 | rolling summary + tail | canonical memory + raw tail | graph-all + raw tail |
| raw tail | baseline 机制 | 16K 配置预算 | 16K 配置预算 |
| 并发 | 2 | 2 | V4 runner 串行 |

V3 运行还使用了 12,288 token manager context、8,192 token active memory、2,048 token
update ledger、81,920 token shared Answer budget，compactor 关闭。V4 使用程序生成节点/边
ID、时间和字段规范化、occurrence 去重、冲突路由，并将每个 batch、claim、edge、projection
和 Answer 写入 SQLite。

## 3. 全量准确率

DeepSeek judge 对每个版本的 120 条最终答案进行判定，三组使用相同的问题、reference answer
和 rubric，所有 360 条 judge 请求均成功。

| 方案 | 正确 | 总数 | 准确率 | 相对 baseline |
| --- | ---: | ---: | ---: | ---: |
| Rolling Summary baseline | 73 | 120 | 60.83% | - |
| V3 `chunk=2048` | **81** | 120 | **67.50%** | **+6.67 个百分点** |
| V4 graph-all | 68 | 120 | 56.67% | -4.17 个百分点 |

### 3.1 按题型

| 题型 | baseline | V3 | V4 |
| --- | ---: | ---: | ---: |
| `knowledge-update` | 13/19 (68.42%) | 13/19 (68.42%) | 12/19 (63.16%) |
| `multi-session` | 16/32 (50.00%) | 15/32 (46.88%) | **18/32 (56.25%)** |
| `single-session-assistant` | 4/13 (30.77%) | **6/13 (46.15%)** | 3/13 (23.08%) |
| `single-session-preference` | 4/7 (57.14%) | **5/7 (71.43%)** | 4/7 (57.14%) |
| `single-session-user` | 15/17 (88.24%) | **16/17 (94.12%)** | 15/17 (88.24%) |
| `temporal-reasoning` | 21/32 (65.63%) | **26/32 (81.25%)** | 16/32 (50.00%) |
| **合计** | **73/120 (60.83%)** | **81/120 (67.50%)** | **68/120 (56.67%)** |

### 3.2 配对迁移

逐题对齐 DeepSeek 判定后：

- V3 相对 baseline：23 条由错变对，15 条由对变错，净增加 8 条；
- V4 相对 baseline：17 条由错变对，22 条由对变错，净减少 5 条；
- V3 相对 V4：V3 有 23 条正确而 V4 错误，V4 有 10 条正确而 V3 错误。

因此 V3 的提升不是由少数单一题型造成，而是主要来自 temporal reasoning 和
single-session-user/assistant；V4 的 multi-session 有改善，但时间推理和 assistant 题回退
抵消了收益。

## 4. 运行完成与失败重试

首次全量运行中，部分请求因上游服务 `http://117.186.43.62:5027` 返回 HTTP 502 而失败：

- V3 首次为 90 完成、6 失败；
- V4 首次为 47 完成、49 失败。

失败样本及其关联 calls、batch、claim、edge、projection 和 state 轨迹被事务删除后重新调用，
已完成样本未修改。最终结果为：

- V3：96 条新样本全部完成，加上旧 pilot 组成 120 条评分集；
- V4：96 条新样本全部完成，加上两个 12 条 pilot shard 组成 120 条评分集；
- 最终 V3/V4 数据库均无 `failed` 或 `running` sample；
- `PRAGMA foreign_key_check` 清理后通过，未发现 SQLite 写锁冲突。

因此最终准确率不包含因 HTTP 502 导致的缺失样本，失败主要是基础设施噪声，不应解释为 V3/V4
算法失败。

## 5. V3 轨迹与成本

以下是本次新 96 条 V3 Manager 构建运行的统计；旧的 24 条 V3 pilot 是从已落盘 memory
进行 Answer-only replay，不能与新 96 条的 Manager 成本直接相加。

| 指标 | V3 新 96 条 |
| --- | ---: |
| completed samples | 96 |
| Manager batches | 5,955 |
| 总 calls | 6,051 |
| 输入 tokens | 52,208,375 |
| 输出 tokens | 2,115,322 |
| 平均 Answer 输入 | 36,084.6 |
| 平均总输入 | 543,837.2 |
| 平均总输出 | 22,034.6 |
| 平均 sample latency | 1,295.8 秒 |

V3 运行统计文件为：

`results/memory_sidecar/sidecar-v3-eval120-c2048-20260830-new96/run_summary.json`

## 6. V4 图谱维护轨迹

以下统计来自 V4 新 96 条数据库；24 条 pilot 的轨迹分别保存在两个 shard 中。

| 指标 | V4 新 96 条 |
| --- | ---: |
| completed samples | 96 |
| V4 batches | 5,955 |
| claims | 17,955 |
| normalized edges | 17,750 |
| graph projections | 96 |
| projection truncation | 0 |
| program deduplicated | 111 |
| ambiguous occurrence | 5,262 |
| quarantined | 89 |
| unknown relation | 4 |
| conflicts | 59 |
| 输入 tokens | 26,620,564 |
| 输出 tokens | 1,244,228 |
| 平均 Answer 输入 | 20,839.1 |
| 平均总输入 | 277,297.5 |
| 平均总输出 | 12,960.7 |
| 平均 sample latency | 440.2 秒 |

V4 的 claim 路由分布为：

| route status | 次数 |
| --- | ---: |
| `applied` | 12,429 |
| `applied_ambiguous_occurrence` | 5,262 |
| `deduplicated_merged` | 111 |
| `deduplicated_alias_merged` | 1 |
| `conflict` | 59 |
| `quarantined` | 89 |
| `raw_unknown_relation` | 4 |

这些数据说明 V4 的程序级图谱维护链路本身是可运行、可追溯的：claim 大部分进入 graph，
重复事实由程序合并，无法确定的事实被 quarantine/raw claim 保留，且 graph-all projection
没有发生预算截断。问题主要出现在 Answer 如何从完整图谱中选择、排序和聚合事实，而不是数据库
写入失败。

## 7. V4 失败轨迹分析

### 7.1 图谱有事实，但 Answer 选择错误

在 grocery 金额样本中，图谱已经抽取了多个 provider 和金额，但 `graph-all` 同时展示了
目标 grocery、其他消费和计划事实，Answer 将非目标金额混入排序，导致选择错误。说明：

> “事实进入 graph”不等于“Answer 能按问题正确筛选事实”。

### 7.2 时间关系没有问题级投影

V4 已将部分相对时间规范化，但 `TARGET`、`PREFERS`、`OBSERVED` 等关系仍以同一图谱文本呈现。
在 temporal reasoning 题中，Answer 需要自行判断计划、偏好和实际观察的区别，容易选择旧的
或不相关时间，因此 V4 temporal accuracy 只有 50.00%，明显低于 V3 的 81.25%。

### 7.3 图谱上下文过密且关系竞争

V4 新 96 条共有 17,750 条 edge。虽然每次 projection 没有程序截断，但 graph-all 仍可能让
Answer 面对大量辅助关系和跨时间事实。当前的 `graph-all` 实验验证了图谱存储稳定性，却没有
证明全图展示是最佳 Answer 上下文。

## 8. 结论

1. **V3 是当前最佳方案。** 在相同 120 条样本和 DeepSeek judge 下，V3 达到 67.50%，比
   baseline 高 6.67 个百分点。
2. **V4 当前不应替代 V3。** V4 达到 56.67%，低于 baseline 4.17 个百分点；图谱维护正常，
   但 `graph-all` 展示方式增加了事实竞争和聚合错误。
3. **V4 的失败主要是 projection/Answer 问题，不是去重或 SQLite 问题。** 轨迹显示 claim、
   edge、去重和审计均已落盘，且没有 projection truncation；后续应优先验证问题级投影。
4. **V3 的成本较高。** `chunk=2048` 需要大量 Manager batch，V3 新 96 条约 5,955 个
   batch；准确率提升来自更适合当前任务的 memory/Answer 上下文，但代价是运行时间和输入
   token 明显增加。
5. **当前结果不是严格单变量因果实验。** V3/V4 同时改变了 Manager schema、路由规则、
   memory representation 和 Answer 可见文本，结果适合做工程版本选择，不适合把某一项单独
   解释为因果收益。

## 9. 后续实验建议

1. 保留 V3 作为当前主 baseline，不再直接扩大 V4 `graph-all`。
2. 在同一份 V4 SQLite graph snapshot 上比较 `graph-all` 与 query-aware projection，避免
   重复 Manager 构建造成噪声。
3. 数值题只投影目标 predicate、明确金额和 provider，并由程序完成 count/sum/rank；不让
   Answer 从无关金额中自行聚合。
4. 时间题显式区分 `TARGET/PREFERS/OBSERVED`，同时保留 evidence/session date；不能把解析
   成功等同于问题已经可回答。
5. 对 projection 评估增加 relevant-edge recall、false-positive rate、聚合正确率和逐题
   paired accuracy，再决定是否形成 V5 hybrid router。

## 10. 可复核文件

- baseline judge：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_judgments_summary.json`
- V3 judge：`results/memory_sidecar/sidecar-v3-eval120-c2048-20260830-new96/deepseek_judgments-full120_summary.json`
- V4 judge：`results/memory_sidecar/sidecar-v4-eval120-c2048-20260830-new96/deepseek_judgments-full120_summary.json`
- V3 full hypotheses：`results/memory_sidecar/sidecar-v3-eval120-c2048-20260830-new96/hypotheses-full120.jsonl`
- V4 full hypotheses：`results/memory_sidecar/sidecar-v4-eval120-c2048-20260830-new96/hypotheses-full120.jsonl`
- V3 SQLite：`results/memory_sidecar/sidecar-v3-eval120-c2048-20260830-new96/trajectory.sqlite3`
- V4 SQLite：`results/memory_sidecar/sidecar-v4-eval120-c2048-20260830-new96/trajectory.sqlite3`
- 冻结样本清单：`data/samples/longmemeval_s_eval_120_seed_20260821.csv`
- 源数据：`data/official_longmemeval/longmemeval_s_cleaned.json`
