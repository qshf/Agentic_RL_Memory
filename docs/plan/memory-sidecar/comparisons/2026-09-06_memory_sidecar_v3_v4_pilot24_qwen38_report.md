# Memory Sidecar V3 / V4 强模型 24 条复测记录

## 1. 结论

本次在冻结的 24 条 LongMemEval-S pilot 上，使用 Qwen `qwen3.8-27b` 完成 V3 与 V4
端到端重测，并由 DeepSeek `deepseek-v4-pro` 按官方 rubric 判定。

| 方案 | 正确 | 总数 | 准确率 |
| --- | ---: | ---: | ---: |
| V3 `chunk=2048` | 10 | 24 | 41.67% |
| V4 `graph-all + raw tail` | 12 | 24 | 50.00% |

V4 在这组固定样本上多答对 2 条，但样本量很小，且 V4 经过两次独立重跑完成，**不能据此推翻
120 条正式实验中 V3 优于 V4 的结论，也不能将其解释为数字/时间能力提升**。

这轮复测的有效价值是：V4 的 SQLite 图谱维护链路能够端到端运行，且完整图谱投影显著缩短
Answer 输入；但 `graph-all` 仍把筛选、时间状态选择和数值聚合交给 Answer。它是 V5 专用
结构化记忆实验的诊断基线，不是 V4 恢复为主线方案的证据。

## 2. 范围与配置

| 项目 | V3 | V4 |
| --- | --- | --- |
| 数据集 | LongMemEval-S 冻结 pilot 24 | 同左 |
| 样本清单 | `longmemeval_s_pilot_24_from_baseline_eval_20260822.csv` | 同左 |
| Manager / Answer | Qwen `qwen3.8-27b` | Qwen `qwen3.8-27b` |
| 评测 | DeepSeek `deepseek-v4-pro`，`longmemeval-official-rubric-deepseek-v2` | 同左 |
| temperature | 0 | 0 |
| chunk | 2,048 tokens | 2,048 tokens |
| Manager 输出上限 | 4,096 tokens | 4,096 tokens |
| Answer 输出上限 | 1,024 tokens | 1,024 tokens |
| Answer 上下文 | V3 canonical memory + raw tail | V4 graph-all + raw tail |
| raw tail 配置 | 16,384 tokens | 16,384 tokens |
| V4 Manager 图谱窗口 | - | 64 edges |

V3 还启用了 `12,288` token Manager context、`8,192` token active memory、`2,048` token
update ledger 和 `81,920` token shared Answer budget；compactor 关闭。

目录名称中含有 `numtime`，仅表示该轮实验为数字/时间问题诊断准备。**这 24 条 manifest 是既有
通用分层 pilot，并非按金额、数量、日期、时间问题重新筛选的数值题集。**因此本报告不报告
“数值题准确率”。后续 V5 必须使用按问题操作和证据内容离线标注的目标子集。

## 3. 有效运行与重跑

V3 使用一个完整 run，24/24 completed。

V4 首次以两个 12 条 shard 运行时，服务 `http://117.186.43.62:5027/v1/chat/completions`
持续返回 HTTP 502，两个 shard 均未产生有效最终答案。其中 `gpt4_d84a3211` 与 `ccb36322`
已分别构建 43、37 条 edge 后失败，其余样本通常在首个请求即失败。该失败属于上游服务，不计入
算法正确率。

随后以相同 V4 配置分成两个 12 条 retry run 重跑：

| V4 run | 完成 | 正确 | 准确率 |
| --- | ---: | ---: | ---: |
| `...-retry1` | 12/12 | 4 | 33.33% |
| `...-retry2` | 12/12 | 8 | 66.67% |
| 合并有效集 | 24/24 | 12 | 50.00% |

重跑分片的配置指纹一致，均为 `364832b8...`，均使用 `v4.1` 分层 Manager context 和
`graph-all` projection。不过 V4 不是单个原子 run，服务时间漂移仍是实验噪声来源。

## 4. 配对结果

按 `question_id` 对齐两组 24 条 DeepSeek judgment：

| 配对结果 | 数量 |
| --- | ---: |
| V3、V4 都正确 | 7 |
| V3、V4 都错误 | 9 |
| V3 错，V4 对 | 5 |
| V3 对，V4 错 | 3 |

V4 相对 V3 净增 2 条。转正样本为 `gpt4_d84a3211`、`0a995998`、`67e0d0f2`、`6cb6f249`、
`75832dbd`；转负样本为 `09d032c9`、`852ce960`、`gpt4_d6585ce9`。8 条 discordant pairs
不足以支持统计显著性结论。

按原 manifest 题型汇总如下：

| 题型 | V3 | V4 |
| --- | ---: | ---: |
| `knowledge-update` | 2/4 | 1/4 |
| `multi-session` | 1/10 | 5/10 |
| `single-session-assistant` | 2/2 | 2/2 |
| `single-session-preference` | 2/4 | 2/4 |
| `single-session-user` | 2/2 | 2/2 |
| `temporal-reasoning` | 1/2 | 0/2 |
| **总计** | **10/24** | **12/24** |

V4 的净变化主要来自 `multi-session`，而不是 temporal reasoning；恰好说明这轮样本不能用于
证明专用数值/时间设计的收益。

## 5. 上下文与成本观察

| 指标 | V3 | V4（两个 retry 合并） |
| --- | ---: | ---: |
| completed samples | 24 | 24 |
| Manager calls | 1,478 | 1,478 |
| Answer calls | 24 | 24 |
| 总调用 | 1,502 | 1,502 |
| 总输入 tokens | 12,732,543 | 6,587,351 |
| 总输出 tokens | 528,518 | 312,985 |
| 平均 Answer 输入 | 35,844 | 20,828 |
| 平均 memory / graph 文本 | 9,314 | 4,264 |
| 平均 raw tail | 26,351 | 16,384 |
| 平均 sample latency | 1,112 秒 | 667 秒 |

V4 的 Manager context 被限制为最多 64 条图谱边，因而输入 token 与 Answer context 都小于 V3。
这是表示压缩与有限 Manager window 的共同结果，**不表示 V4 的信息召回更好**。V3 本轮记录的
raw tail 均值高于其配置预算，属于历史 baseline tail 记录的实际落库口径；不能把两组
`raw_tail_tokens` 直接当成严格受控变量。

## 6. 轨迹观察

V4 的每个 sample 都在 SQLite 中保存 batch 输入、原始 Manager 输出、normalized claim、route、
edge、Answer projection 与最终 hypothesis。两个有效 retry 共 24 条 sample、1,478 个 Manager
batch；所有最终 sample 均为 `completed`。

代表性成功样本 `gpt4_d84a3211` 问“年初以来 bike-related expenses 的总额”：

- V3 的最终 memory 只显示 `$40` bike lights 和 `$120` helmet，漏掉 `$25` bike chain，Answer
  输出 `$160`；
- V4 投影中保留 bike chain `$25`、bike lights `$40`、helmet `$120`，Answer 输出 `$185`，与
  reference 一致；
- V4 同时注明 rack 是 planned、tune-up 没有金额。这说明图谱记录与 provenance 足以支持该题，
  且结构化呈现有助于排除 planned fact。

但失败模式仍然存在：

1. V4 的 `graph-all` 把全部 current edge 交给 Answer，问题相关的实体、provider、状态和
   时间范围仍由 Answer 自行选择；记录正确不等于聚合正确。
2. `temporal-reasoning` 从 1/2 降到 0/2。计划、偏好、观察和完成事实在全图中并列，Answer
   仍可能错选时间状态或旧版本。
3. V3 在 10 条 `multi-session` 上仅 1 条正确，说明普通 key/value memory 的跨 session
   检索和累积事实保留仍是核心问题；V4 在这类样本的方向性改善值得保留为诊断线索，但不足以
   直接恢复 V4 主线。

## 7. 与 120 条结果的关系

此前的全量 120 条结果为 V3 `81/120 (67.50%)`、V4 `68/120 (56.67%)`。本次 24 条复测不改变
该版本选择：当前通用 Memory Manager 仍以 V3 为 baseline，V4 `graph-all` 作为可审计的负向
对照和结构化抽取/去重的实现基础。

V5 不应继续尝试用完整图谱改善 Answer；应把变量收窄为：

```text
V3 通用事实路由
  + 仅对直接证据支持的金额、数量、日期、时间建立 typed records
  + 程序按受控 QuerySpec 筛选并在条件满足时计算
  + 紧凑 numeric-temporal context + V3 memory + raw tail
```

这需要另建按 `sum/count/rank/latest/before/after` 操作分层的目标样本，并逐题记录 evidence
coverage、typed-record parse status、query 命中/排除原因及计算状态。

## 8. 可复核文件

- [V3 config](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-numtime-pilot24-qwen38-c2048-20260904/config.json)
- [V3 run summary](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-numtime-pilot24-qwen38-c2048-20260904/run_summary.json)
- [V3 judgments](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-numtime-pilot24-qwen38-c2048-20260904/deepseek_judgments.jsonl)
- [V3 SQLite](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-numtime-pilot24-qwen38-c2048-20260904/trajectory.sqlite3)
- [V4 first shard output](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-numtime-pilot24-qwen38-c2048-20260904-shard1/e2e.json)
- [V4 second shard output](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-numtime-pilot24-qwen38-c2048-20260904-shard2/e2e.json)
- [V4 retry 1 judgments](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-numtime-pilot24-qwen38-c2048-20260904-retry1/deepseek_judgments.jsonl)
- [V4 retry 1 SQLite](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-numtime-pilot24-qwen38-c2048-20260904-retry1/trajectory.sqlite3)
- [V4 retry 2 judgments](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-numtime-pilot24-qwen38-c2048-20260904-retry2/deepseek_judgments.jsonl)
- [V4 retry 2 SQLite](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-numtime-pilot24-qwen38-c2048-20260904-retry2/trajectory.sqlite3)
- [冻结 manifest](/Users/qshf/my-project/Agentic_RL_Memory/data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv)
- [120 条对照报告](2026-09-01_memory_sidecar_v3_v4_eval120_report.md)
