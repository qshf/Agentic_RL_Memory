# V4 错误样本修复后重跑记录

## 实验配置

- 样本：原 V4 24 条 pilot 中 DeepSeek judge 判错的 12 条
- Manager：Qwen `qwen3.8-27b`
- chunk：2048
- Answer：原 V1 Answer prompt
- 上下文：V4 `graph-all + raw tail`
- Judge：DeepSeek `deepseek-v4-pro`
- 两路独立 SQLite 并发，每路 6 条

轨迹目录：

- `results/memory_sidecar/sidecar-v4-retry-errors-shard1-20260830/`
- `results/memory_sidecar/sidecar-v4-retry-errors-shard2-20260830/`

## 结果

| question_id | 修复前 | 修复后 | 结论 |
|---|---|---|---|
| `gpt4_2ba83207` | no | no | graph-all 仍混入非目标金额，需 query-aware projection |
| `bf659f65` | no | **yes** | 抽取/关系修复后恢复 Tame Impala vinyl，计数为 3 |
| `0edc2aef` | no | no | Miami 偏好事实仍未被 Answer 有效使用 |
| `09d032c9` | no | no | Answer 仍给通用电池建议，未利用 portable power bank 偏好 |
| `gpt4_d6585ce9` | no | no | 时间相关 ATTENDED 事实过多，Answer 选错同行人 |
| `852ce960` | no | no | 在线答案仍为旧结果；新规则离线确认 Wells Fargo 取 `$350,000` |
| `69fee5aa` | no | no | 38 枚事实未完整恢复，仍需抽取/计数轨迹审计 |
| `0a995998` | no | no | 服装待取/退事实只回答 2，可能有事实遗漏或状态混淆 |
| `81507db6` | no | no | Answer 把 4 个毕业事件都计入，需时间范围/事件投影 |
| `75832dbd` | no | no | 偏好上下文被通用 AI/职业事实竞争 |
| `51a45a95` | no | **yes** | Target coupon 事实恢复，答案为 Target |
| `gpt4_59149c77` | no | no | “today”相对日期未形成可计算日期链 |

新结果为 `2/12`。如果假设原来 12 条正确样本未受影响，24 条 pilot 的候选总分为
`14/24`；这不是完整 24 条重跑后的正式准确率，不能与原始 `12/24` 直接当作严格 A/B
结论。

## 轨迹结论

1. `USES` 关系修复有效，能够把此前 unknown relation 的事实送入 graph，但偏好题仍需要
   Answer 使用正确的关系和对象，单纯进入 graph 不足够。
2. claim 局部 evidence 属性解析修复有效，专辑计数题由 2 恢复为 3。
3. provider-nearest amount 规则已通过离线重放：同一句包含房价 `$325,000` 和预批准额
   `$350,000` 时，`Wells Fargo` claim 现在得到 `$350,000`。该样本需单独在线重跑才能
   评估 Answer 变化。
4. grocery、毕业典礼、同行人和 MoMA 时间差等失败，核心仍是 graph-all 没有问题级筛选、
   聚合和相对时间链；继续扩大 graph-all 不会稳定解决这些问题。

## 下一步

先用同一批新数据库做 Answer-only replay，比较 `graph-all` 与 `query-auto`，尤其是
`gpt4_2ba83207`、`81507db6`、`gpt4_d6585ce9` 和 `gpt4_59149c77`。随后单独在线重跑
`852ce960`，确认金额选择修复能否改变最终答案；不要立即扩大到 120 条。

## 后续回放结果

在同一批持久化 graph 上运行 `query-auto` 后：

- `gpt4_2ba83207` 从 Walmart 修复为 **Thrive Market**，证明 provider projection 有效；
- `81507db6` 的 occurrence fallback 能看到 3 个 `ATTENDED` 毕业事件，但 Answer 仍可能
  受时间文本影响；
- `bf659f65` 的 occurrence fallback 保留 3 个专辑/EP 相关边，但当前 Answer 仍只答 1，
  说明投影筛选还需继续收紧；
- `69fee5aa` 仍只有 1 条相关 count occurrence，图谱中没有 `38`，更像 Manager 抽取遗漏；
- `gpt4_59149c77` 已从 generic count 改为 temporal projection，但 Manager 仍未把两个
  博物馆访问规范化成可计算日期；
- 最新代码在线重跑 `852ce960` 后回答 **`$350,000`**，修复生效。

因此下一步优先级是：先修 Manager 对 coin count 和 museum date 的抽取，再对 count/temporal
projection 做小范围回放；provider 金额问题已通过 query-aware projection 解决，不再继续
扩大 graph-all。
