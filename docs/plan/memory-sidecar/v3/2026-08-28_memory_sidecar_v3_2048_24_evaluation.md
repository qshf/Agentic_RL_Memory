# Memory Sidecar V3 2048 Chunk 24 条实验报告

## 1. 实验范围

本文记录 V3 在同一份 24 条冻结 pilot 样本上的 `chunk=2048` 实验，并比较最终 Answer
看到完整 canonical memory 与看到最终 summary 的两种上下文。

| 版本 | run | Manager chunk | Answer memory | raw tail | Answer 模型 |
| --- | --- | ---: | --- | --- | --- |
| V3 summary-on | `sidecar-v3-memory-summary-on-24-20260827-c2048` | 2,048 | 一次最终摘要 | 相同完整 baseline tail | Qwen `qwen3.8-27b` |
| V3 summary-off | `sidecar-v3-memory-summary-off-24-20260828-c2048` | 2,048 | 完整 canonical memory | 相同完整 baseline tail | Qwen `qwen3.8-27b` |

样本清单为：

```text
data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv
```

两组共享同一份 V3 canonical memory。summary-off 是从 summary-on 的已落盘 memory
进行 Answer-only replay，不重新调用 Manager；两组 Answer prompt 保持 V1 模板不变。
raw tail 先完整重建，再由 shared 80K Answer budget 统一裁剪；本次 24 条样本均未发生
tail 截断，因此两组看到的 tail 完全一致。

## 2. 结果摘要

| 指标 | summary-on | summary-off | 变化 |
| --- | ---: | ---: | ---: |
| 样本完成 | 24/24 | 24/24 | 不变 |
| Answer 调用 | 24 | 24 | 不变 |
| Manager 调用 | 1,478 | 0（复用） | - |
| Compactor 调用 | 24 | 0 | - |
| 总输入 tokens | 12,895,194 | 858,598 | Answer-only 不可直接比较 |
| 总输出 tokens | 544,971 | 3,123 | Answer-only 不可直接比较 |
| Answer 平均输入 | 27,342.6 | 35,774.9 | **-23.6%** |
| Answer 总输入 | 656,223 | 858,598 | **-23.6%** |
| 运行 wall time | 约 4.51 小时 | 约 12.0 分钟 | summary-on 包含 memory 构建 |

summary-on 的输入 token 构成为：

| 调用类型 | 次数 | 输入 tokens | 输出 tokens |
| --- | ---: | ---: | ---: |
| V3 Manager | 1,478 | 11,850,740 | 521,708 |
| V3 Compactor | 24 | 388,231 | 19,751 |
| Answer | 24 | 656,223 | 3,512 |
| 合计 | **1,526** | **12,895,194** | **544,971** |

summary-on 的最终摘要合计约 19,489 tokens；summary-off 的 canonical memory 合计约
221,864 tokens。两组 raw tail 合计均为 632,414 tokens，因此 Answer 输入差异主要来自
memory projection，而不是 tail 不一致。这里的 tail 是 baseline 的完整近期尾部，平均约
26.4K tokens，不是固定 16K tail。

## 3. 准确率与 DeepSeek Judge

原始 V3 运行没有在 Answer 阶段调用 judge；本次补充实验使用项目现有的
`evaluate_longmemeval_deepseek.py`，对两组已落盘的 24 条 Answer 独立评分。Judge 使用
`deepseek-v4-pro`、`longmemeval-official-rubric-deepseek-v2`，两组使用完全相同的
question、reference 和 rubric，且 48 条判断均无请求错误。

| 方案 | 正确数 | 准确率 | judged | errors |
| --- | ---: | ---: | ---: | ---: |
| summary-on | 10/24 | **41.67%** | 24 | 0 |
| summary-off | 12/24 | **50.00%** | 24 | 0 |
| off - on | +2 | **+8.33 个百分点** | - | - |

按题型：

| 题型 | summary-on | summary-off | 变化 |
| --- | ---: | ---: | ---: |
| knowledge-update | 3/4 (75%) | **4/4 (100%)** | +1 |
| multi-session | **2/10 (20%)** | 1/10 (10%) | -1 |
| single-session-assistant | 2/2 (100%) | 2/2 (100%) | 0 |
| single-session-preference | 0/4 (0%) | **2/4 (50%)** | +2 |
| single-session-user | 2/2 (100%) | 2/2 (100%) | 0 |
| temporal-reasoning | 1/2 (50%) | 1/2 (50%) | 0 |

两组判断不同的 4 条为：`09d032c9`（on no / off yes）、`d24813b1`（no / yes）、
`dad224aa`（no / yes）和 `dd2973ad`（yes / no）。这说明 summary 会改变 Answer 的
证据选择，影响不只体现在 token 数量。

## 4. Answer 行为对比

### 4.1 summary-on 的代表性结果

summary-on 结果中，以下样本与 reference 明显不一致：

| question_id | reference | summary-on 输出 | 初步判断 |
| --- | --- | --- | --- |
| `gpt4_d84a3211` | `$185` | `$160` | 金额记录未完整聚合 |
| `gpt4_2ba83207` | `Thrive Market` | `Trader Joe's` | 选择了有明确金额但不是 reference 的商店 |
| `bf659f65` | `3` | `1` | 摘要/记忆未保留全部购买记录 |
| `67e0d0f2` | `20` | `12` | 课程数量聚合不足 |
| `dad224aa` | `7:30 am` | `7:15 am` | 把 weekday target/observation 当成周六答案 |
| `6cb6f249` | `17 days` | `1 day` | 时间差计算或事件选择错误 |
| `gpt4_59149c77` | `7 days / 8 days` rubric | 无法确定 | 日期事实没有被有效保留 |

也有若干简单实体问答保持正确，例如 `852ce960=$400,000`、`69fee5aa=38`、
`778164c6=Grilled Snapper with Mango Salsa`、`ccb36322=Spotify`、
`830ce83f=the suburbs`、`c4f10528=Miss Bee Providore` 和 `51a45a95=Target`。

### 4.2 summary-on 与 summary-off 的差异

两组使用同一 canonical memory 和同一 tail，但最终 Answer 选择并不一致：

| question_id | summary-on | summary-off | 说明 |
| --- | --- | --- | --- |
| `gpt4_d84a3211` | `$160` | 分项后仍未得到 `$185` | 完整 memory 没有自动解决聚合问题 |
| `gpt4_2ba83207` | `Trader Joe's` | `Walmart` | 两组选择了不同的金额记录，均未选 reference |
| `dad224aa` | `7:15 am` | 详细列出周六记录，选择 `7:30 am` | 摘要改变了时间事实的竞争关系 |
| `0a995998` | `2` | `1 item` | summary 与 canonical 对记录数量的理解不同 |
| `3a704032` | `1` | `2` | “last month”的日期边界解释不同 |
| `dd2973ad` | `2 AM` | 无法确定 | summary 保留了时间线，canonical Answer 未有效选取 |
| `gpt4_59149c77` | 无法确定 | `1 day` | 两组都没有复原 reference 所需的日期差 |

这说明 summary 并不是单纯减少 token：它会改变 Answer 可见事实的排序和竞争关系。摘要
如果漏掉 observation、金额明细或日期，就可能比完整 memory 更差；但完整 memory 也不能
保证 Answer 模型完成聚合、排序和时间推理。

## 5. Memory 与 Compactor 观察

### 5.1 运行完整性

24 条样本均生成了 Manager 轨迹、canonical memory、Compactor 结果和 Answer。2048 chunk
导致 Manager 调用较多，但本次没有因 shared 80K Answer budget 发生 tail 截断。

summary-on 的数据库中还保留了两个早期中断 attempt（`09d032c9`、`d24813b1`）的
`running` 状态；最终 `run_summary.json` 按 24 个 completed sample 统计，不应把这两个
残留 attempt 当作额外样本或最终答案。

### 5.2 摘要保真度问题

`dad224aa` 是最清楚的案例。canonical memory 同时包含：

```text
7:30：前一个周六的一次具体观察
9:30：更早一个周六的一次具体观察
8:30：后来描述的周六当前习惯
7:15 / 8:00：工作日和周末目标
```

summary 只保留了类似下面的目标信息：

```text
Her sleep goal is 7:15 am weekdays/8:00 am weekends; she is experimenting with a 10:45 pm bedtime.
```

因此 summary-on 输出 `7:15 am`，不是因为摘要正确解决了 `7:30/8:30` 冲突，而是因为
摘要删除了相关 observation。该样本的 reference 本身与后续“当前约 8:30”陈述存在语义
冲突，必须在 judge/人工审计中单独标记，不能作为 summary-on 的准确率提升证据。

## 6. 与 V1/V2 报告的关系

V1/V2 报告中的 24 条准确率也来自同一 judge 版本，因此可以做方向性对照：

| 方案 | DeepSeek judge 正确数 | 准确率 |
| --- | ---: | ---: |
| V1 | 10/24 | 41.67% |
| V2 | 12/24 | 50.00% |
| V3 summary-on | 10/24 | 41.67% |
| V3 summary-off | 12/24 | 50.00% |

不过 V3 修改了 Manager prompt、memory schema、chunk 策略和 Compactor，不能将上述数字
解释为单变量因果实验。本次可与 V1/V2 直接比较的成本和行为方向为：

1. V3 `chunk=2048` 仍保持每条样本按 evidence 时间顺序串行处理；
2. 最终 summary 将 Answer 输入减少约 23.6%；
3. 2048 chunk 的 Manager 调用量明显高于 8192 chunk，memory 构建成本较高；
4. summary-on 的质量取决于 Compactor 是否保留可回答问题的事实，而不是只保留高层主题。

## 7. 结论

1. **工程链路完成**：24 条样本全部完成，canonical memory、summary、Answer 和轨迹均已
   落盘。
2. **摘要有效压缩上下文**：在相同 raw tail 下，Answer 输入从约 35.8K 降至约 27.3K，
   减少约 23.6%。
3. **本次 DeepSeek judge 显示 summary-on 为 10/24，summary-off 为 12/24**；摘要压缩
   带来了 2 条准确率回退，不能宣布 summary-on 提升准确率。
4. **summary-on 存在事实遗漏风险**：尤其是多条 observation、金额明细和日期链；
   `dad224aa` 中 on 被判 no、off 被判 yes，说明摘要删除事实会直接影响答案。
5. **完整 memory 也不是充分条件**：`gpt4_d84a3211`、`gpt4_2ba83207` 等样本表明，
   Answer 模型仍可能错误聚合或选择时间上不匹配的记录。

## 8. 下一步

1. 对 summary-on 与 summary-off 的 4 条 judge 分歧样本做逐层审计，重点检查摘要输入和
   Answer 最终可见上下文。
2. 对 summary 漏掉的三类事实增加保真度检查：数值/金额明细、带日期 observation、
   plan 与 completed event 的区分。
3. 对 `dad224aa`、`gpt4_d84a3211`、`gpt4_2ba83207`、`67e0d0f2`、`6cb6f249` 做
   Manager → canonical memory → summary → Answer 的逐层审计。
4. 在同一 24 条样本上再比较 2048 与 8192 的正式 judge 结果，再决定是否扩大到 120 条。

## 9. 可复核文件

- summary-on 运行统计：
  `results/memory_sidecar/sidecar-v3-memory-summary-on-24-20260827-c2048/run_summary.json`
- summary-on 轨迹：
  `results/memory_sidecar/sidecar-v3-memory-summary-on-24-20260827-c2048/trajectory.sqlite3`
- summary-on 假设导出：
  `results/memory_sidecar/sidecar-v3-memory-summary-on-24-20260827-c2048/hypotheses.jsonl`
- summary-on DeepSeek judge：
  `results/memory_sidecar/sidecar-v3-memory-summary-on-24-20260827-c2048/deepseek_judgments.jsonl`
- summary-on DeepSeek 汇总：
  `results/memory_sidecar/sidecar-v3-memory-summary-on-24-20260827-c2048/deepseek_judgments_summary.json`
- summary-off 运行统计：
  `results/memory_sidecar/sidecar-v3-memory-summary-off-24-20260828-c2048/run_summary.json`
- summary-off 轨迹：
  `results/memory_sidecar/sidecar-v3-memory-summary-off-24-20260828-c2048/trajectory.sqlite3`
- summary-off DeepSeek judge：
  `results/memory_sidecar/sidecar-v3-memory-summary-off-24-20260828-c2048/deepseek_judgments.jsonl`
- summary-off DeepSeek 汇总：
  `results/memory_sidecar/sidecar-v3-memory-summary-off-24-20260828-c2048/deepseek_judgments_summary.json`
- 冻结样本清单：
  `data/samples/longmemeval_s_pilot_24_from_baseline_eval_20260822.csv`
- 源数据：
  `data/official_longmemeval/longmemeval_s_cleaned.json`
