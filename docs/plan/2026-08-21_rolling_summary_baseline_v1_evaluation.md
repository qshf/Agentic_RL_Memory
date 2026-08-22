# Rolling Summary Baseline V1 评估记录

## 1. 评估对象

本记录对应 run `rolling-summary-eval120-v1-atomic-c2`，即 V1 atomic-facts prompt、Qwen `qwen3.8-27b`、rolling trigger/prefix `80 * 1024` tokens、并发 2、冻结 120 条样本。

生成结果：

- 120/120 个样本完成，240 次模型调用，120 次 rolling compression。
- 总输入 13,713,371 tokens，总输出 559,642 tokens。
- 墙钟时间约 4 小时 23 分钟，吞吐约 0.00760 samples/s。
- SQLite 完整性检查通过，最终答案同时写入 `samples.hypothesis` 和 `calls.response_text`。

评判使用 `scripts/evaluate_longmemeval_deepseek.py`，模型为 DeepSeek `deepseek-v4-pro`，按 LongMemEval 题型 rubric 返回 yes/no。该结果是 LLM judge 近似评估，不等同于官方 GPT-4o judge 的复现；需要结合样本人工复核。

## 2. 评估结论

DeepSeek judge 判定正确 73/120，准确率 **60.83%**，无 judge 请求错误。

| 题型 | 正确/总数 | 准确率 |
| --- | ---: | ---: |
| single-session-user | 15/17 | 88.24% |
| temporal-reasoning | 21/32 | 65.63% |
| knowledge-update | 13/19 | 68.42% |
| single-session-preference | 4/7 | 57.14% |
| multi-session | 16/32 | 50.00% |
| single-session-assistant | 4/13 | 30.77% |

V1 的滚动状态、SQLite 四表存储、摘要重复写入和最终答案落库均按设计工作；当前主要瓶颈是信息保留和答案推理，不是数据库写入或 runner 失败。因此 V1 可以作为可回放 baseline，但 **60.83% 不足以作为可用准确率版本**。

### 2.1 按失败原因拆分

对 47 条 `judge=no` 逐条检查最终 Answer 实际收到的 `summary + raw tail`，并且暂不把“模型看到了证据但算错/选错”计为上下文问题，得到以下保守统计：

| 归类 | 数量 | 占 judge=no |
| --- | ---: | ---: |
| 明确上下文缺失或关键事实未保留 | **18** | **38.30%** |
| 上下文已有，最终答案推理/选择/枚举错误 | 26 | 55.32% |
| gold 或 judge 存在疑点 | 3 | 6.38% |

因此，如果用户问“有多少是上下文问题”，当前答案是：**18/120 条总体样本（15.00%），或 18/47 条错误样本（38.30%）**。

明确归入上下文问题的样本为：

```text
ccb36322, 0a995998, 3a704032, dd2973ad,
81507db6, 4f54b7c9, a08a253f,
37f165cf, gpt4_1916e0ea, gpt4_fa19884d,
1568498a, ceb54acb, f523d9fe,
8aef76bc, 8752c811, 352ab8bd, fca762bc, 7a8d0b71
```

上面列表实际包含 21 个 ID；其中 `92a0aa75`、`dcfa8644`、`gpt4_93159ced` 的边界证据不足，属于待复核项。按严格保守口径剔除这 3 个边界项后，**明确上下文问题是 18 条**；若把这 3 条也按“最终上下文没有保留可直接回答的完整时间/数值关系”计入，则是 **21 条（44.68% of judge=no）**。后续报告默认使用 18 条的保守口径。

这项统计的判定标准是：最终 summary/raw tail 中没有保留回答所需的关键事实、事实之间的关系，或完整的数值/时间条件。若事实已经存在，只是模型没有做求和、排序、日期计算、去重或区分“当前”与“计划”，归入答案生成错误，不归入上下文问题。

### 2.2 上下文充分样本的 DeepSeek 复核

为验证“上下文已经足够但原 Answer Model 回复错误”的归因，选取上述 47 条错误中除 18 条上下文缺失和 3 条 gold/judge 疑点之外的 26 条，复用每题最终实际使用的 `summary + raw tail + question`，改用 DeepSeek `deepseek-v4-pro` 重新生成答案。生成阶段没有提供 gold；随后用同一 LongMemEval rubric 的 DeepSeek judge 判定新答案。

结果：

- 26/26 生成成功，无 API 错误；
- 14/26 被判正确，准确率 **53.85%**；
- 12/26 仍被判错误；
- 这 26 条原来全部是 Qwen V1 的错误，因此替换为强模型后有 **14 条得到纠正**。

被纠正的代表性样本包括：`eeda8a6d`（17 条鱼）、`gpt4_ab202e7f`（5 个厨房项目）、`gpt4_e05b82a6`（10 次过山车）、`92a0aa75`（1 年 5 个月）、`gpt4_468eb063`（9 天）、`gpt4_45189cb4`（运动赛事顺序）、`2a1811e2`（21 天）、`dcfa8644`（14 天）、`c4ea545c`（频率更新）和 `031748ae_abs`（正确识别题目角色不匹配）。

仍然错误的 12 条为：`gpt4_d84a3211`、`gpt4_2ba83207`、`bf659f65`、`0edc2aef`、`09d032c9`、`d24813b1`、`67e0d0f2`、`gpt4_d6585ce9`、`852ce960`、`69fee5aa`、`dad224aa`、`778164c6`。其中包括 `$185` 求和、Thrive Market 选择、音乐专辑计数、偏好迁移和若干数值更新错误，说明“上下文充分”不等于答案模型一定能完成证据筛选和推理。

注意：生成模型和 judge 都是 `deepseek-v4-pro`，因此这是强模型替换实验，不是独立模型盲评；14/26 应作为方向性验证结果，不应解释为无偏估计。

### 2.3 二次错误样本的带理由再生成

对 2.2 中仍错误的 12 条样本再次使用相同的最终上下文（`summary + raw tail + question`）调用 `deepseek-v4-pro`。这次要求模型返回两个 JSON 字段：`answer` 和 `evidence_basis`。`evidence_basis` 只记录可核验的事实、日期、计算或取值规则，不要求模型输出隐藏思维链。随后仍使用同一 DeepSeek judge 复核答案字段。

结果为：12/12 生成成功；2/12 判定正确，准确率 **16.67%**；10/12 仍错误。被纠正的是：

- `gpt4_d84a3211`：列出 `$25 + $40 + $120 = $185`，修正了漏加头盔费用的问题；
- `d24813b1`：结合用户曾成功制作 lemon poppyseed cake 的经验，给出 lemon lavender pound cake 等可迁移建议。

仍错误的样本及理由暴露出的错误如下：

| question_id | 结果 | 模型实际选择 | `evidence_basis` 暴露的问题 |
| --- | --- | --- | --- |
| `gpt4_2ba83207` | no | Walmart | 将 Thrive Market `$150` 与 Walmart `$120` 的比较结论选反；理由中同时列出了正确金额，说明是排序/选择错误。 |
| `bf659f65` | no | 2 | 只枚举 Billie Eilish 和 Whiskey Wanderers，漏掉第三张专辑/EP；属于计数和枚举不完整。 |
| `0edc2aef` | no | 无 Miami 建议 | 错误声称没有偏好信息，未把已有酒店偏好迁移到 Miami。 |
| `09d032c9` | no | 通用电池建议 | 明确称上下文没有手机/电池细节，忽略了 portable power bank 这一用户事实。 |
| `67e0d0f2` | no | 12 | 取用了 Coursera 的局部计数，没有汇总全部在线课程，属于跨 session 汇总错误。 |
| `gpt4_d6585ce9` | no | sister | 选中了另一场音乐活动的同行人，未按“last Saturday”选择目标事件。 |
| `852ce960` | no | `$350,000` | 取用摘要旧值，没有采用 raw tail 中较新的 `$400,000`。 |
| `69fee5aa` | no | 37 | 同样选择旧的摘要值，未采用最新的 38。 |
| `dad224aa` | no | 8:30 am | 选择了另一条周六作息记录，未解决 7:30/8:30 的时间状态冲突。 |
| `778164c6` | no | Escovitch Fish | 选择了同主题的另一道 Jamaican 鱼料理，未定位题目要求的“snapper + fruit”答案 Grilled Snapper with Mango Salsa。 |

这次再生成的结论是：错误案例中存在可见证据时，模型仍可能因旧值优先、局部计数、事件定位、偏好迁移和实体消歧而答错；仅要求模型补充理由并未自动纠正这些错误。理由字段对诊断有价值，因为它能直接显示模型采用了哪条证据，但不能作为答案正确性的替代判据。

错误集中在两种不同阶段：

1. 摘要阶段丢失早期、答案所需的事实。query-independent 摘要不知道最终问题，无法优先保护某个答案事实；事实一旦被压缩掉，末尾 raw tail 也无法补回。
2. 事实仍在摘要中，但 Answer Model 没有完成求和、排序、日期计算或证据选择。这类问题继续增加摘要长度不能直接解决。

## 3. 错误样例分析

### 3.1 `ccb36322`：摘要遗漏早期事实

- 问题：`What is the name of the music streaming service have I been using lately?`
- gold：`Spotify`
- hypothesis：`I don't have information about a music streaming service...`
- 原始证据位于 `answer_f1fbb330` session：用户明确说自己最近在 Spotify 听 Arctic Monkeys 和 The Neighbourhood。
- 轨迹证据：该事实出现在 `states.step_ordinal=45/46` 的 raw message；第一次压缩在 step 355。最终 `states.summary_text` 长度约 16,457 字符，既不含 `Spotify`，也不含 `music streaming`；step 356 之后的 raw tail 已经是后续 session。

判断：这是典型的 **summary recall failure**。答案模型在最终上下文中确实没有可用的 Spotify 证据，不应归因于答案模型“不会回答”。

### 3.2 `fca762bc`：早期 assistant 事实被丢弃，并出现无依据替代答案

- 问题：询问使用 mnemonic 记忆单词的语言学习 app。
- gold：`Memrise`
- hypothesis：`Anki`
- 原始答案 session 的 assistant 明确写出：`Memrise uses mnemonics...`，对应 trajectory raw step 185。
- 第一次压缩在 step 346；最终摘要中没有 `Memrise` 或 `mnemonic`。最终 raw tail 只包含更晚的 session，不能恢复 step 185 的答案。

判断：第一层是 **summary omission**；第二层是答案模型在缺证据时生成了 `Anki`，属于 unsupported substitution/hallucination。对于“历史中没有证据”的问题，Answer prompt 还需要更严格的证据门槛。

### 3.3 `gpt4_d84a3211`：摘要完整，但答案算术错误

- 问题：从年初到现在总共花了多少 bike-related expenses？
- gold：`$185`
- hypothesis：`$85`
- 最终摘要包含完整金额：链条 `$25`、车灯 `$40`、头盔 `$120`，并保留了相应日期和 bike context。
- 正确计算为 `$25 + $40 + $120 = $185`；`$85` 等于只把前两项相加。

判断：这是 **answer reasoning failure**，不是摘要缺失。改进方向是最终答案阶段显式枚举候选事实并做算术校验，不能只继续扩大 summary budget。

### 3.4 `gpt4_7abb270c`：排序题有数据/标注歧义

- 问题：六个博物馆从早到晚的顺序。
- gold：`Science Museum, Museum of Contemporary Art, ...`
- hypothesis：`Museum of Contemporary Art, Science Museum, ...`
- 原始对话中 Science Museum 明确是 `2023-01-15 today`；同一 session 中 Museum of Contemporary Art 被描述为 `recently`，摘要记录为 `prior to 2023-01-15`。按字面时间，“MCA 在 Science 之前”是合理解释。

判断：judge 判为 no，但该样本不能简单归为模型错误，存在 gold 与相对日期表达不一致的风险。此类题应在人工复核统计中单独标记，避免把标注问题误计为压缩失败。

### 3.5 `bc8a6e93_abs`：疑似 judge false negative

- 问题：叔叔生日聚会做了什么？
- gold 的要求是指出“未提及叔叔；提到的是侄女”。
- hypothesis：`I baked a lemon blueberry cake for my niece's birthday, not my uncle's.`
- 最终摘要也保留了 lemon blueberry cake/niece。

判断：这句话实际上覆盖了 gold 的否定事实和正确对象，但 DeepSeek judge 判为 no。该样本说明 LLM judge 结果不能视为绝对真值，应保留人工复核标记；它也可能使 60.83% 低估约 1 个样本点。

### 3.6 `eeda8a6d`：现状与计划混淆，属于答案阶段错误

- 问题：`How many fish are there in total in both of my aquariums?`
- gold：`17`
- hypothesis：`26 fish`
- 数据集的两个 answer session 给出了清晰的现状事实：
  - 20-gallon tank：10 条 neon tetras + 5 条 golden honey gouramis + 1 条 pleco = 16 条；
  - 10-gallon tank：1 条 betta Bubbles；
  - 合计：`16 + 1 = 17`。
- V1 最终 summary 仍保留了这两条记录：`Aquarium Stock | 10 neon tetras, 5 golden honey gouramis, 1 small pleco catfish`，以及 `Aquarium | Has a 10-gallon tank with a betta fish named Bubbles`。
- 最终 raw tail 没有覆盖这两个存量事实，但包含后续讨论：用户“考虑添加” lemon tetras 或 zebra danios，助手建议以 `10-15` 条为一组引入。

最可能的错误路径是：模型把现有 20-gallon 存量 `10+5+1=16` 与计划添加的 schooling fish 最小组规模 `10` 相加，得到 `26`，同时漏掉了 10-gallon tank 中的 betta。也就是说，答案上下文中有足够证据，gold 也可由原始 session 直接推出；错误发生在 Answer Model 对“当前存量”和“计划添加”的状态区分及最终求和上。

该样例不应归为 summary omission。它说明摘要中的状态标签（`Current status`、`Current interest`、`Current plan`）虽然存在，但自由文本 Answer prompt 没有强制模型只统计当前存量。后续 Answer prompt 应要求先列出“已有/当前”对象，再明确排除“考虑添加/建议/计划”对象，最后执行求和。

## 4. 对 V1 的处理结论

本轮不修改 V1 代码和数据定义，先固定上述结果作为 baseline。后续版本若要提升准确率，应分别验证：

- 面向问题的 query-aware memory 或事实索引，解决 `Spotify`/`Memrise` 类召回遗漏；
- Answer 阶段的证据枚举、日期/算术/排序校验，解决 `$185` 类推理错误；
- 对“不足信息”和个人偏好题增加 abstain/证据门槛，减少无依据的 `Anki` 类替代答案；
- 对 gold 与相对日期冲突、否定答案等样本增加人工复核集，区分系统错误和数据/judge 错误。

## 5. 可复核文件

- 运行汇总：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/run_summary.json`
- 全部答案：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/hypotheses.jsonl`
- DeepSeek 判定：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_judgments.jsonl`
- 判定汇总：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_judgments_summary.json`
- 轨迹数据库：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3`
- 人工审核页：`docs/plan/rolling_summary_v1_manual_review.html`
- 审核页生成脚本：`scripts/build_manual_review_html.py`
- 上下文充分样本的 DeepSeek 新答案：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_regenerated_context_good.jsonl`
- 新答案 judge 结果：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_regenerated_context_good_judgments.jsonl`
- 新答案 judge 汇总：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_regenerated_context_good_judgments_summary.json`
- 二次错误带理由再生成：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_second_error_reasoned.jsonl`
- 二次错误 judge 结果：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_second_error_reasoned_judgments.jsonl`
- 二次错误 judge 汇总：`results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_second_error_reasoned_judgments_summary.json`
- 二次错误再生成脚本：`scripts/regenerate_second_error_reasoned_deepseek.py`
