# Memory Sidecar V1 / V2 对比报告

## 1. 实验范围

本文比较同一份 24 条冻结样本上的两个 Memory Sidecar 运行：

| 版本 | run | Manager chunk | 主要协议 | 最终回答模型 | Judge |
| --- | --- | ---: | --- | --- | --- |
| V1 | `sidecar-strong-pilot-24-v1-c2-selected` | 2,048 tokens | 单个 Manager 调用返回单个事件 | Qwen `qwen3.8-27b` | DeepSeek `deepseek-v4-pro` |
| V2 | `sidecar-v2-pilot-24-c1` | **8,192 tokens** | batch 事件、原子路由、版本状态、Reconciler、金额补漏 | Qwen `qwen3.8-27b` | DeepSeek `deepseek-v4-pro` |

两次运行使用相同的 LongMemEval-S 24 条 pilot manifest：

`data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv`

V2 没有重新生成历史或最终答案，而是使用已经落盘的 V2 `hypotheses.jsonl` 做 judge，因此评分失败时不需要重跑推理。

## 2. 结果摘要

| 指标 | V1 | V2 | 变化 |
| --- | ---: | ---: | ---: |
| Judge 正确数 | 10/24 | **12/24** | **+2** |
| Judge 准确率 | 41.67% | **50.00%** | **+8.33 个百分点** |
| 总模型调用 | 1,502 | **451** | **-70.0%** |
| 总输入 tokens | 6,004,238 | **5,202,130** | **-13.4%** |
| 总输出 tokens | 227,865 | 325,318 | +42.8% |
| 运行耗时 | 5,449 秒 | 6,362 秒 | +16.8% |
| 样本完成 | 24/24 | 24/24 | 不变 |

V2 在这组样本上同时取得了更高准确率和更少调用次数，但总耗时没有下降。原因是 V2 每个 Manager batch 输出更完整的结构化结果，并额外执行金额补漏和 Reconciler；单次调用输出更长、延迟更高。

## 3. 准确率比较

### 3.1 按题型

| 题型 | V1 | V2 | 变化 |
| --- | ---: | ---: | ---: |
| knowledge-update | 4/4 (100%) | 0/4 (0%) | -4 |
| multi-session | 2/10 (20%) | **6/10 (60%)** | +4 |
| single-session-assistant | 1/2 (50%) | **2/2 (100%)** | +1 |
| single-session-preference | 2/4 (50%) | 2/4 (50%) | 0 |
| single-session-user | 1/2 (50%) | 1/2 (50%) | 0 |
| temporal-reasoning | 0/2 (0%) | **1/2 (50%)** | +1 |

V2 的主要收益集中在 `multi-session` 和部分时间推理题，说明更大的 chunk、跨 batch 状态和 Reconciler 对跨轮次聚合有帮助。`knowledge-update` 从 V1 的 4/4 降到 0/4，是明显回归，不能被总体平均数掩盖；应优先逐题审计这些样本的 active/superseded 状态、时间链和最终 Answer 上下文。

### 3.2 逐题迁移

相对 V1，V2 新增正确 7 条：

```text
3a704032, 6cb6f249, 778164c6, 81507db6,
dd2973ad, gpt4_59149c77, gpt4_d84a3211
```

V1 正确而 V2 错误 5 条：

```text
67e0d0f2, 69fee5aa, 830ce83f,
852ce960, dad224aa
```

两者均正确 5 条。净变化为 +2 条，和 12/24 对 10/24 的总体结果一致。

## 4. 路由与完整性

### 4.1 V1

V1 共执行 1,478 个 Manager 事件，平均每题约 61.6 个事件：

| route_status | 数量 |
| --- | ---: |
| applied | 1,100 |
| noop | 368 |
| rejected_add_conflict | 9 |
| deduplicated | 1 |

V1 的一个 Manager 调用对应一个事件，单个 chunk 中如果包含多个事实，容易出现只保存其中一个事实的情况。

### 4.2 V2

V2 共生成 2,150 个 event item，平均每题 14.08 个 Manager batch，每个 batch 可以包含多个 item：

| route_status | 数量 |
| --- | ---: |
| applied | **2,121** |
| rejected_patch_conflict | 19 |
| rejected_parse | 5 |
| rejected_invalid_time_evidence | 2 |
| rejected_target_mismatch | 2 |
| deduplicated_source_merged | 1 |

应用成功率为 **2,121 / 2,150 = 98.65%**。V2 的拒绝结果会被保存在事件项和状态快照中，便于回放；这比 V1 只记录单事件路由状态更适合定位冲突和字段丢失。

### 4.3 金额补漏

V2 对用户消息中的明确美元金额增加了程序检查：如果 Manager batch 没有覆盖金额，则额外调用 money repair。此次共触发 65 次：

| 状态 | 次数 |
| --- | ---: |
| money_repaired | 46 |
| money_repair_incomplete | 19 |

这说明金额 guardrail 能发现 V1 中容易漏掉的金额，但当前 repair prompt 仍有约 29.2%（19/65）未完整修复。它是 V2 当前最明确的剩余问题。

## 5. 成本和上下文

V2 将 chunk 从 2,048 提高到 8,192，Manager 调用数显著下降：

| V2 调用类型 | 次数 | 输入 tokens | 输出 tokens |
| --- | ---: | ---: | ---: |
| Manager | 338 | 3,698,660 | 296,109 |
| money repair | 65 | 777,785 | 25,541 |
| Reconciler | 24 | 174,978 | 177 |
| Answer | 24 | 550,707 | 3,491 |
| 合计 | **451** | **5,202,130** | **325,318** |

相比 V1，V2 的输入 token 减少 13.4%，但输出 token 增加 42.8%。因此 8,192 chunk 的直接效果是减少请求轮数和重复上下文输入，而不是保证总延迟或总 token 都下降。

## 6. 结论

1. **V2 的结构化协议有效改善了 pilot 总体准确率**：50.00% 对 41.67%，但提升幅度有限，且存在题型回归。
2. **V2 显著减少了调用次数**：1,502 降至 451，验证了 8,192 chunk 对减少调用轮数的作用。
3. **V2 的路由可审计性明显增强**：同一 batch 可保存多个 item，并记录 parse、冲突、时间证据和版本状态。
4. **V2 仍有数据完整性风险**：19 次金额修复未完成，另有 28 个事件项被拒绝或解析失败，需要检查是否影响对应样本的 memory sufficiency。
5. **不能据此宣布 V2 已优于 V1**：样本只有 24 条，V2 的 `knowledge-update` 为 0/4，且两次运行的 prompt、schema 和 chunk 策略不同，结果只能作为 pilot 信号。

## 7. 下一步

建议按以下顺序处理：

1. 先审计 V2 的 4 条 `knowledge-update` 失败样本，确认是 memory 状态错误、时间/版本选择错误，还是 Answer Model 使用错误。
2. 针对 19 条 `money_repair_incomplete`，保存初始事件、repair 事件和最终路由结果，逐条判断是金额识别、事件合并还是 target 定位失败。
3. 对 V1 正确而 V2 错误的 5 条做回归审计，避免只优化新增正确样本。
4. 修复后在相同 24 条样本上重跑 V2，再决定是否扩展到 120 条正式评估。

## 8. 可复核文件

- V1 运行统计：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/run_summary.json`
- V1 judge：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/deepseek_judgments_summary.json`
- V1 trajectory：`results/memory_sidecar/sidecar-strong-pilot-24-v1-c2-selected/trajectory.sqlite3`
- V2 配置：`results/memory_sidecar/sidecar-v2-pilot-24-c1/config.json`
- V2 运行统计：`results/memory_sidecar/sidecar-v2-pilot-24-c1/run_summary.json`
- V2 judge：`results/memory_sidecar/sidecar-v2-pilot-24-c1/deepseek_judgments_summary.json`
- V2 逐题 judge：`results/memory_sidecar/sidecar-v2-pilot-24-c1/deepseek_judgments.jsonl`
- V2 trajectory：`results/memory_sidecar/sidecar-v2-pilot-24-c1/trajectory.sqlite3`

## 9. 可比性限制

- 两次实验使用相同 24 条问题、Qwen `qwen3.8-27b` 生成答案和 DeepSeek `deepseek-v4-pro` judge，但 V2 修改了 Manager prompt、事件 schema、路由和补漏流程，因此不是只改变 chunk 的严格 A/B 实验。
- 24 条样本不足以估计稳定准确率，尤其是每个题型的样本数很小。
- V2 的总输出 token 包含 65 次额外 money repair，不能简单与 V1 的 Manager 输出做单项价格比较。
- Judge 是 LLM judge，不等同于官方 benchmark judge；应结合逐题人工复核。
