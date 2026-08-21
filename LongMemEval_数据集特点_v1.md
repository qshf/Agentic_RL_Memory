# LongMemEval 数据集特点（官方清洗版）

> 官方数据集：[xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
>
> 本地目录：`data/official_longmemeval/`
>
> 核对日期：2026-08-21

## 1. 官方文件与本地核对结果

LongMemEval 官方发布的是三个**独立**的评测文件，每份各有 500 个 question sample。它们不是一个 1,000 条数据集中的两个互为短/长历史的分区，不能混合成同一主结果。

| 本地文件 | 样本数 | session 数（min / median / max） | 用途 |
|---|---:|---:|---|
| `longmemeval_s_cleaned.json` | 500 | 38 / 48 / 62 | 主实验：约 40 个 session、约 115K token 的长历史 |
| `longmemeval_oracle.json` | 500 | 1 / 2 / 6 | 只含答案证据 session 的 Oracle Evidence 上界 |
| `longmemeval_m_cleaned.json` | 500 | 460 / 476 / 490 | 更大规模版本，约 500 个 session |

文件已从官方 Hugging Face 仓库下载并以 JSON 成功解析。本地文件约为 S 265MB、Oracle 15MB、M 2.5GB。

## 2. 数据单位与术语

数据的固定层级是：

```text
question sample
  -> haystack（该问题的全部历史）
    -> 多个 session trajectory（一个完整单 session 对话）
      -> messages（session 内按原始顺序排列的消息）
```

- **question sample**：一个最终问题、其 haystack、标准答案和时间元数据；这是评测的基本单位。
- **session trajectory**：一个完整单 session 的消息序列，不是多个 session 拼出的轨迹。
- **haystack**：同一个问题对应的多个 session trajectory 的集合。
- **memory trajectory**：实验系统逐 session/chunk 更新 Memory Store 产生的状态日志；它不是原始数据中的 session trajectory。

因此，一个最终问题对应多个 session；不能把 `for sample in row["haystack_sessions"]` 中的 `sample` 当成 question sample。

## 3. Schema 与标识

每条样本有 9 个字段：

| 字段 | 含义 |
|---|---|
| `question_id` | 最终问题 ID；在**同一官方文件内**可作为样本标识。跨文件比较时使用 `(split_name, question_id)`。 |
| `question_type` | 六类记忆问题之一。 |
| `question` / `answer` | 最终问题与 gold answer。 |
| `question_date` | 最终问题的时间元数据。 |
| `haystack_dates` | 与 session 一一对应的日期。 |
| `haystack_session_ids` | 与 session 一一对应的 ID。 |
| `haystack_sessions` | 多个完整 session trajectory。 |
| `answer_session_ids` | 官方标注的答案来源 session。 |

每条消息通常为 `dict{role, content, has_answer}`。`has_answer` 是消息级证据标记，可用于离线的 Oracle / 诊断实验；主方法在处理历史时不得读取它。

## 4. 问题能力与数据构造

LongMemEval 覆盖单 session 信息抽取、多 session 聚合、知识更新、时间推理与拒答等长期记忆能力。S 的长 haystack 中含有与问题无关的 filler session，因此评测的是在干扰历史中保存和找回有效信息的能力。

这不是自然产生的真实用户日志。根据论文与官方仓库说明，背景属性和问题由人工控制的流程构造，Llama 3 70B 被用于生成交互内容；干扰会话来自 ShareGPT、UltraChat 等公开对话来源。因此它是一个**受控、部分合成、混合来源**的 benchmark：适合比较记忆机制，但不能直接等同于真实线上用户的长期行为分布。

## 5. 实验边界

1. **主结果只在 S 上报告。** `longmemeval_s_cleaned.json` 的 500 条样本是首轮主评测集合。
2. **Oracle 只作上界或诊断。** 它不等于 S 的短窗口版本，不能与 S 合并后报告平均分，也不能将两者差值解释为单纯的抗干扰退化。
3. **M 留作扩展。** M 的每题约 500 个 session，适合在 S 机制有效后检验扩展性与成本；不应在 API 原型阶段与 S 混跑。
4. **顺序按发布文件处理。** S 与 M 的会话按时间顺序发布；Oracle 的会话顺序不保证按日期排序。任何重排必须记录为实验处理，而不是假定三个文件的数组行为相同。
5. **不要让离线标注泄漏。** `answer`、`answer_session_ids` 和 `has_answer` 只能用于最终评分、Oracle 上界和错误分析，不能输入 query-independent Memory Sidecar 的历史写入阶段。

## 6. 对基线设计的含义

主实验在同一份 S 历史、相同处理顺序、相同最终 Answer Model 和相同最终 token budget 下比较：

| 方法 | 历史处理阶段是否读取最终问题 | 最终输入 |
|---|---|---|
| Full Context | 否 | 完整可容纳历史 + 最终问题 |
| Rolling Summary | 否 | 递归摘要 + 未压缩尾部 + 最终问题 |
| Memory Sidecar | 否 | structured memory + 最终问题 |
| Oracle Evidence | 可使用 gold evidence，仅作上界 | gold evidence + 最终问题 |

Rolling Summary 是合理的通用压缩基线：当累计上下文超过设定窗口，就将旧上下文压回预算后继续处理后续 session。它必须单独标注为 query-independent 压缩，且和 Sidecar 使用相同模型、窗口、预算与解码设置。

## 7. 官方参考

1. [LongMemEval 官方仓库](https://github.com/xiaowu0162/LongMemEval)
2. [LongMemEval 论文](https://arxiv.org/abs/2410.10813)
3. [官方 cleaned dataset](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
