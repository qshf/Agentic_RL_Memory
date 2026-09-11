# Memory Sidecar V3 LongMemEval Oracle 500 实验记录

## 1. 实验结论

本次使用本地部署的 `Qwen2.5-7B-Instruct`，以 V3、`chunk=2048` 跑完
`longmemeval_oracle.json` 中冻结的 500 条样本。最终有效结果为：

- Memory build 和 Answer 均完成：500/500；
- DeepSeek judge 完成：500/500，无 judge 请求错误；
- 正确：301/500；
- 准确率：**60.20%**。

该结果是“V3 Memory Manager + Qwen Answer + DeepSeek judge”的端到端结果，不能单独解释为
Memory Manager 的抽取准确率。

## 2. 数据与运行范围

| 项目 | 配置 |
| --- | --- |
| 数据源 | `data/official_longmemeval/longmemeval_oracle.json` |
| 冻结清单 | `results/oracle/oracle500_manifest.csv` |
| 样本数 | 500 |
| 协议 | V3 `memory-sidecar-v3-multi-event-atomic-batch` |
| chunk | 2,048 tokens |
| Manager/Answer 模型 | `Qwen2.5-7B-Instruct` |
| Judge | `deepseek-v4-pro` |
| Judge rubric | `longmemeval-official-rubric-deepseek-v2` |
| Manager 并发 | 1 |
| Answer 并发 | 1（本次 oracle500 runner 配置为单并发） |
| temperature | 0.0 |
| thinking | disabled |
| Manager context | 12,288 tokens |
| active memory | 8,192 tokens |
| update ledger | 2,048 tokens |
| raw tail | 16,384 tokens |
| Answer shared context | 16,384 tokens |
| Manager max output | 4,096 tokens |
| Answer max output | 1,024 tokens |
| compactor | off |

每条样本先按历史顺序执行 V3 Memory build，再使用构造后的 memory 和 raw tail 生成答案。
Answer 使用原有 V3 Answer 提示词，没有为了本次实验修改 Answer 逻辑。

## 3. 样本题型分布与准确率

| 题型 | 正确 | 总数 | 准确率 |
| --- | ---: | ---: | ---: |
| `knowledge-update` | 58 | 78 | 74.36% |
| `multi-session` | 55 | 133 | 41.35% |
| `single-session-assistant` | 54 | 56 | 96.43% |
| `single-session-preference` | 15 | 30 | 50.00% |
| `single-session-user` | 61 | 70 | 87.14% |
| `temporal-reasoning` | 58 | 133 | 43.61% |
| **合计** | **301** | **500** | **60.20%** |

结果呈现出明显的题型差异：单 session assistant/user 题表现最好；multi-session 和
temporal-reasoning 明显较弱。这说明当前主要瓶颈仍是跨 session 事实组合、时间关系解析及
Answer 对多个记忆事实的选择，而不是单条事实是否能够进入数据库。

## 4. 运行成本与轨迹统计

以下数字来自最终 SQLite 轨迹中 500 条完成样本的汇总：

| 指标 | 汇总 | 平均/样本 | 中位数 | 最大值 |
| --- | ---: | ---: | ---: | ---: |
| 总 LLM calls | 2,344 | 4.69 | 5 | 15 |
| 输入 tokens | 6,862,963 | 13,725.93 | 13,516 | 54,227 |
| 输出 tokens | 968,694 | 1,937.39 | 1,553 | 14,855 |
| full history tokens | 2,890,064 | 5,780.13 | 5,809.5 | 22,460 |
| Answer input tokens | 3,028,127 | 6,056.25 | 6,116.5 | 15,371 |
| Answer output tokens | 29,381 | 58.76 | 28 | 686 |
| sample latency | 15,802,314 ms | 31,604.63 ms | 25,454 ms | 231,294 ms |
| V3 batches | 1,852 | - | - | - |
| V3 memory records | 1,205 | - | - | - |

`run_summary.json` 的 `execution.wall_time_ms=199,676` 是恢复/重试调用的运行窗口，不能作为
整个 500 条实验的真实墙钟耗时。该运行曾多次从 SQLite 断点续跑；样本级累计 latency 更适合
用于成本分析。

## 5. 失败重试与数据库状态

### 5.1 失败原因

早期运行和重试过程中出现的失败均为本地 sglang 服务暂时不可用，典型错误为：

```text
HTTPConnectionPool(host='127.0.0.1', port=30001):
Failed to establish a new connection: [Errno 61] Connection refused
```

失败发生在 `/v1/chat/completions` 或 `/tokenize` 请求阶段，属于服务/基础设施错误，不是
模型返回格式错误，也不是 V3 parser、memory 更新或 Answer 逻辑错误。服务恢复后，失败样本
被单独重试，已经完成的样本没有重新构建。

### 5.2 最终有效结果与历史审计行

SQLite 采用追加式 attempt 记录以保留重试轨迹，因此数据库行数大于 500：

| 状态/含义 | 数量 |
| --- | ---: |
| 最终完成样本 | 500 |
| 历史失败 attempt 行 | 32 |
| 历史遗留 `running` 标记 | 1 |
| `hypotheses.jsonl` 最终导出 | 500 |

因此评测结果按最终完成的 500 条 hypothesis 统计，而不是把历史失败行重复计入分母。后续如
需发布结果，建议在报告脚本中按 `question_id` 选择最后一个 `completed` attempt，并把旧的
failed/running 行作为审计信息单独展示。

## 6. DeepSeek judge 记录

DeepSeek judge 对最终 500 条 hypothesis 全部完成：

```json
{
  "judge_model": "deepseek-v4-pro",
  "judge_version": "longmemeval-official-rubric-deepseek-v2",
  "total": 500,
  "judged": 500,
  "correct": 301,
  "accuracy": 0.602,
  "errors": 0
}
```

Judge 输入 token 共 123,726，输出 token 共 3,180。该 judge 是 LLM 近似评估，不等同于
LongMemEval 官方 GPT-4o 评测复现；论文对比时需要确认题型、答案口径和 judge 指标一致。

## 7. 可追溯文件

- 运行结果目录：`results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/`
- 运行配置：[config.json](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/config.json)
- 最终运行汇总：[run_summary.json](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/run_summary.json)
- 最终答案：[hypotheses.jsonl](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/hypotheses.jsonl)
- DeepSeek 逐题判定：[deepseek_judgments.jsonl](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/deepseek_judgments.jsonl)
- DeepSeek 汇总：[deepseek_judgments_summary.json](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/deepseek_judgments_summary.json)
- SQLite 轨迹：[trajectory.sqlite3](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v3-oracle500-qwen25-c2048-20260903/trajectory.sqlite3)
- 样本清单：[oracle500_manifest.csv](/Users/qshf/my-project/Agentic_RL_Memory/results/oracle/oracle500_manifest.csv)

SQLite 中的主要追溯表包括：`samples`、`calls`、`sidecar_batches`、`sidecar_memory`、
`sidecar_states`、`sidecar_context`。其中 `samples` 记录每次 attempt 的状态、token、延迟、
答案和错误；`sidecar_batches` 记录每个 chunk 的输入、memory 前状态、模型原始响应和解析
状态；`sidecar_memory`/`sidecar_states` 记录 memory 更新前后状态，能够定位事实是在抽取、
更新还是 Answer 上下文阶段丢失。

## 8. 结果解读与下一步

1. V3 在 500 条 Oracle 样本上达到 60.20%，证明当前链路可以在本地 7B 模型上稳定完成
   大规模构建、落库、重试和 judge。
2. 单 session 题的高准确率说明基础事实保留和直接 Answer 上下文基本可用。
3. multi-session/temporal 题的低准确率说明后续应优先分析跨 session 记忆选择、时间字段
   保留和 Answer 的证据组合，不宜先把问题归因于 SQLite 写入。
4. 本次仅验证 V3，不能据此证明 V4 图谱或 V5 混合路由的收益；后续版本应复用同一份冻结
   manifest、同一 judge rubric，并保留 per-sample paired comparison。
5. 由于本地服务曾发生 connection refused，复现实验时应先记录服务健康检查，并把服务失败
   与模型/算法失败分开统计。
