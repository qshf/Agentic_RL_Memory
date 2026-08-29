# Memory Sidecar V4：24 条 Pilot 评测报告

## 1. 结论摘要

V4 在冻结的 24 条 LongMemEval pilot 上完成了端到端测试。使用 `graph-all + raw tail` 的默认上下文，DeepSeek `deepseek-v4-pro` 按 LongMemEval rubric 判定 **12/24，准确率 50.00%**。

与同一数据集上的 V3 基线相比：

| 方案 | 正确数 | 准确率 |
|---|---:|---:|
| V3 summary-on | 10/24 | 41.67% |
| V3 summary-off | 12/24 | 50.00% |
| V4 graph-all | 12/24 | 50.00% |

因此，V4 当前的主要收益是工程和可审计性改进，而不是准确率提升。V4 没有低于 V3 summary-off，但也没有达到进入 120 条确认实验的效果门槛。

## 2. 实验配置

| 项目 | 配置 |
|---|---|
| 数据集 | `longmemeval_s_cleaned.json` |
| 样本 | `longmemeval_s_pilot_24_from_baseline_eval_20260822.csv` 中冻结的 24 条 |
| chunk | 2048 tokens |
| Manager | Qwen `qwen3.8-27b`，stateless minimal-claim prompt |
| Manager max output | 1024 tokens |
| Answer | V1 Answer prompt，未修改 |
| 上下文 | V4 graph-all + 16K raw tail |
| Judge | DeepSeek `deepseek-v4-pro`，`longmemeval-official-rubric-deepseek-v2` |
| 并发方式 | 两个各 12 条的独立 SQLite shard 并发；避免多个 worker 写同一 sample |

V4 Manager 只输出事实原子和本地 evidence ID，程序负责关系别名、时间规范化、节点/边 ID、occurrence 路由、冲突和 SQLite 审计。

## 3. 逐样本结果

`yes/no` 为 DeepSeek judge 结果；“回答摘要”只用于快速定位，完整回答保存在对应 shard 的 `hypotheses.jsonl`。

| question_id | 题型 | Gold | Judge | 回答摘要 |
|---|---|---|---|---|
| `gpt4_d84a3211` | multi-session | `$185` | yes | 自行车链条、车灯、头盔合计 `$185` |
| `gpt4_2ba83207` | multi-session | Thrive Market | no | 错选 Walmart，并把多条金额混入 Walmart |
| `bf659f65` | multi-session | `3` | no | 列出音乐专辑，但数量结论不符合 gold |
| `0edc2aef` | single-session-preference | Miami 酒店偏好 | no | 声称没有 Miami 酒店信息 |
| `09d032c9` | single-session-preference | 便携充电宝相关建议 | no | 只给通用手机省电建议 |
| `d24813b1` | single-session-preference | 基于烘焙经历给建议 | yes | 给出结合既有蛋糕经验的烘焙建议 |
| `67e0d0f2` | multi-session | `20` | yes | `8 edX + 12 Coursera = 20` |
| `gpt4_d6585ce9` | temporal-reasoning | my parents | yes | 回答 my parents |
| `852ce960` | knowledge-update | `$400,000` | no | 回答 `$350,000` |
| `69fee5aa` | knowledge-update | `38` | no | 回答 `37` |
| `dad224aa` | knowledge-update | `7:30 am` | yes | 列出多种观察并包含正确的 `7:30 am` |
| `778164c6` | single-session-assistant | Grilled Snapper with Mango Salsa | yes | 复述此前 assistant 推荐结果 |
| `ccb36322` | single-session-user | Spotify | yes | 回答 Spotify |
| `0a995998` | multi-session | `3` | no | 回答 `1 item` |
| `3a704032` | multi-session | `3` | no | 植物数量统计错误 |
| `dd2973ad` | multi-session | `2 AM` | no | 认为没有相关睡觉时间记录 |
| `81507db6` | multi-session | `3` | no | 毕业典礼数量/是否参加判断错误 |
| `4f54b7c9` | multi-session | `5` | yes | 正确列出家人传下来的古董数量 |
| `830ce83f` | knowledge-update | the suburbs | yes | 回答 Rachel moved back to the suburbs |
| `6cb6f249` | multi-session | `17 days` | yes | 回答 17 days |
| `c4f10528` | single-session-assistant | Miss Bee Providore | yes | 回答 Miss Bee Providore |
| `75832dbd` | single-session-preference | AI healthcare research 偏好 | no | 给出泛 AI/职业背景建议，偏离偏好 |
| `51a45a95` | single-session-user | Target | yes | 识别出 Target |
| `gpt4_59149c77` | temporal-reasoning | 7 days | no | 声称没有足够日期信息 |

## 4. 按题型统计

| question_type | 正确 | 总数 | 准确率 |
|---|---:|---:|---:|
| `knowledge-update` | 2 | 4 | 50.00% |
| `multi-session` | 4 | 10 | 40.00% |
| `single-session-assistant` | 2 | 2 | 100.00% |
| `single-session-preference` | 1 | 4 | 25.00% |
| `single-session-user` | 2 | 2 | 100.00% |
| `temporal-reasoning` | 1 | 2 | 50.00% |
| **合计** | **12** | **24** | **50.00%** |

V4 在 assistant/user 单 session 题上表现稳定；主要损失集中在 multi-session、preference 和需要精确筛选的 temporal/knowledge-update 题。

## 5. 成本与轨迹指标

两路 shard 的合计如下：

| 指标 | 合计 |
|---|---:|
| samples | 24 |
| evidence chunks | 1,478 |
| Manager + Answer calls | 1,502 |
| Manager input tokens | 3,059,631 |
| Manager output tokens | 354,472 |
| Answer input tokens | 704,972 |
| Answer output tokens | 5,384 |
| 总 input tokens | 3,764,603 |
| 总 output tokens | 359,856 |
| normalized edges | 4,659 |
| raw unknown-relation claims | 58 |
| quarantined claims | 30 |

每条样本的完整 Manager 请求、原始响应、normalized claims、route result、edge 快照和 Answer 都保存在 SQLite 中；没有 sample 级失败或 `not_runnable`。

## 6. 轨迹分析

### 6.1 已验证的改进

1. Manager 不再把不断增长的 graph state 放回每个 chunk，单次输入保持稳定，避免了 V3 式状态膨胀。
2. `has_completed` 等关系别名可以规范化为 `COMPLETED`，课程计数题得到 `8 + 12 = 20`。
3. 后续 claim 的金额和 provenance 可以回填到已有 occurrence，而不是由第一次不完整 claim 永久决定。
4. `last month`、`last week`、`the week before last`、`last Saturday` 可以按 evidence session 日期解析为可比较时间区间。
5. 所有 V4 graph、claim、route 和 projection 都进入 SQLite，可追溯到 source unit。

### 6.2 仍然存在的主要问题

1. **全图上下文过杂。** `gpt4_2ba83207` 中 Thrive Market 的 `$150` 已被抽取，但 Answer 在全图中把 Walmart 的多条金额拼接成更大的总额，说明“事实已进入 graph”不等于“Answer 能正确筛选”。
2. **关系语义仍被金额污染。** `PLANS/PREFERS/COMPLETED` 的金额或数量会与 `PURCHASED` 混在同一个 graph-all 文本中，模型可能错误聚合。
3. **跨 chunk 同事件仍难判定。** provider 后缀合并已扩展到同 session 相邻 evidence，但两个 claim 如果来自更远的 unit，仍可能形成重复 occurrence。
4. **时间关系没有问题级投影。** 程序已解析时间，但 Answer 仍需从 `TARGET/PREFERS/OBSERVED` 中自行判断“计划时间”还是“实际观察时间”。
5. **模型仍会把辅助内容当事实。** 虽然 prompt 已排除问题、建议和 assistant 语句，但长历史中仍出现 quarantine/raw claim，表明需要更严格的 query-aware projection，而不是继续扩大 graph-all。

## 7. 后续决策

当前不直接进入 120 条确认实验。V4 的下一步应保持 graph-all 对照，新增 V5 query-aware 程序投影：

- 数值比较题只投影 `PURCHASED` occurrence，并按 provider/currency 聚合；
- 时间题同时投影 `TARGET/PREFERS/OBSERVED`，显式标注 plan/preference/observation；
- provider 未明确的金额保留 `unknown`，禁止按邻近文本归因；
- 非目标关系保留在 SQLite 审计中，但不直接进入 Answer 上下文；
- 用同一 graph snapshot 对比 V4 graph-all 与 V5 projection，避免重复 Manager 构建。

只有 query-aware projection 在这 24 条上至少超过 V3 summary-off 的 `12/24`，或在保持准确率的同时显著减少上述结构错误，才进入 120 条确认实验。

## 8. 产物

- [V4 shard 1 轨迹](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-pilot24-shard1-20260829)
- [V4 shard 2 轨迹](/Users/qshf/my-project/Agentic_RL_Memory/results/memory_sidecar/sidecar-v4-pilot24-shard2-20260829)
- [V4 smoke 与修复记录](/Users/qshf/my-project/Agentic_RL_Memory/docs/plan/2026-08-29_memory_sidecar_v4_smoke4_evaluation.md)
- Git 实现提交：`247e316`
- Git 结果记录提交：`7e6218e`
