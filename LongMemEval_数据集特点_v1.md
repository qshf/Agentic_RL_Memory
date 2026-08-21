# LongMemEval 数据集特点分析（S 版镜像）

> 数据来源：HuggingFace `LIXINYI33/longmemeval-s`（官方 S 版的公开镜像）
> 本地路径：`data/longmemeval_s`（Arrow 格式，用 `load_from_disk` 读取）
> 分析日期：2026-08-20

## 0. 数据来源和本地镜像边界

官方 LongMemEval 仓库说明：`longmemeval_s.json`、`longmemeval_oracle.json` 和 `longmemeval_m.json` 是三个不同的数据文件；每个文件各包含 500 个 evaluation instances。官方 S 文件约 40 个历史 session、约 115K tokens；Oracle 文件只保留证据相关历史 session。

本地 `data/longmemeval_s` 并不是官方单独的 500 条 S 文件，而是一个 1,000 行的镜像/拼接文件。根据本地 CSV 和 session 来源核对：

| 本地 dataset index | 对应内容 | 可观察特征 |
|---|---|---|
| `0–499` | `longmemeval_oracle` 风格子集 | 1–6 个 session，session ID 基本为 `answer_*` |
| `500–999` | `longmemeval_s` 风格子集 | 38–62 个 session，包含 `sharegpt_*`、`ultrachat_*` 等 filler |

两部分的 `question_id` 各出现一次，因此同一个最终问题在本地文件中出现两行；这不是“短历史版本和长历史版本”的官方配对，而是 Oracle 文件与 S 文件各自包含同一批问题。`dataset_index` 才是本地 Arrow 行的唯一键。

**实验结论：** LongMemEval-S 主结果只应在 `dataset_index=500–999` 的 500 条 S 子集上报告；`dataset_index=0–499` 应作为 Oracle Evidence 对照，不应混入 S 主结果或训练/验证切分。

## 1. 数据规模

| 指标 | 数值 |
|---|---|
| 样本总数 | 1000 |
| 字段数 | 9 |
| 单样本历史规模（sessions） | min=1, **median=38**, max=62 |
| 单样本消息数 | min=2, **median=396**, max=616 |
| 单样本历史字符数 | min=496, **median≈455K**, max≈514K |
| 单样本历史估算 token | median ≈ 114K tokens |

一句话：**每个 question sample 都配了一段由多个 session 组成的对话历史（haystack），模型需要从这些 session 中找证据回答最终问题。**

### 术语约定

- **session trajectory**：一个完整的单 session 对话轨迹，包含该 session 内按原始顺序排列的多条 `user / assistant` 消息。
- **question sample**：一个最终问题及其对应的 `haystack_sessions`、gold answer 和时间元数据。
- **haystack**：一个 question sample 下的多个 session trajectory 的集合。
- **memory trajectory**：Memory Sidecar 逐个读取 session/chunk、更新 memory store 所产生的状态日志；它不是数据集中的单个 session。

因此，不能把“一个 question sample”或“一个 haystack”直接称为“一条 trajectory”。

### 关于重复 `question_id` 的边界

本地镜像有 1,000 个 question sample、500 个 `question_id`；每个 ID 恰出现两次。两条 sample 的最终问题、gold answer 和 `question_type` 相同，但 `question_date`、session ID 和完整 haystack 不同。结合官方文件说明与本地 session 来源，前一条属于 Oracle 风格子集，后一条属于 LongMemEval-S 风格子集；它们不是同一历史简单增添干扰后的短/长配对。

因此：

- 用 `dataset_index` 标识并评测单个 sample；
- 用 `question_id` 做分组切分，避免同一最终问题泄漏；
- 将 `0–499` 作为 Oracle 对照，将 `500–999` 作为 S 主评测；
- 在 S 子集内部按 session 数量报告规模分组结果（如 38–46、47–62），而不是把 Oracle 与 S 称为短/长配对；
- 不将 Oracle 与 S 的准确率差直接解释为“添加干扰后的性能下降”；Oracle 是不同发布文件和不同评测设置。

## 2. 问题类型分布

| question_type | 数量 | 占比 | 说明 |
|---|---|---|---|
| `temporal-reasoning` | 266 | 26.6% | 时间推理：比较事件先后、算间隔天数 |
| `multi-session` | 266 | 26.6% | 多窗口推理：信息分散在多个 session |
| `knowledge-update` | 156 | 15.6% | 知识更新：后出现的说法覆盖旧说法 |
| `single-session-user` | 140 | 14.0% | 单窗口，证据在用户消息 |
| `single-session-assistant` | 112 | 11.2% | 单窗口，证据在助手消息 |
| `single-session-preference` | 60 | 6.0% | 单窗口，用户偏好类 |

三类「单 session」合计约 31%；三类「多 session / 时序」合计约 69%，是记忆任务的主战场。

## 3. 数据 Schema

每条样本含 9 个字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `question_id` | str | 最终问题的唯一标识；在本镜像中对应一个短 sample 和一个长 sample，不能单独替代 sample 行主键 |
| `question_type` | str | 上表 6 类之一 |
| `question` | str | 最终要回答的问题 |
| `answer` | str | 标准答案（gold answer） |
| `question_date` | str | 提问时间戳 |
| `haystack_dates` | list[str] | 每个 session 的时间戳（与 session 一一对应） |
| `haystack_session_ids` | list[str] | 每个 session 的 ID |
| `haystack_sessions` | 三层嵌套 | 一个 question sample 下的多个 session trajectory |
| `answer_session_ids` | list[str] | 官方标注的答案出处 session |

**关键结构 `haystack_sessions`**（question sample → session trajectory → 消息）：

```
question sample → haystack_sessions（list，多个 session trajectory）
                  └── session[i]（一条完整 session trajectory）
               └── 每条消息 = dict{role, content, has_answer}
```

- `role`：`user` / `assistant`
- `content`：消息正文
- `has_answer`：`bool`，**消息级**标记，该消息是否包含回答所需的关键证据

## 4. 答案证据分布（核心发现）

### 4.1 证据在 session trajectory 级与消息级

- 含答案的 session 数：min=0, **median=2**, max=6
- 含答案的消息条数：min=0, **median=2**, max=6
- 含答案消息的角色：**user 1684 条** vs assistant 108 条（约 94% 证据在用户侧）

> ⚠️ 注意：约 42 条样本含答案数为 0（`has_answer` 全 False），这类样本可能证据标注缺失或答案需跨窗口综合推断，做评估时要留意。

### 4.2 证据在历史中的位置

- **session 内部**：证据高度集中在每条 session 的**第一条 user 消息**（55.5% 位于 session 首条，55.1% 为首条 user，前 2 条内 57.5%）。这是 LongMemEval 的构造手法——每个 session 首条 user 常以「By the way, I just …」陈述最近事件并埋入证据。
- **整体 turn 流（按时间排序）**：由于「首条证据」效应叠加，整体流**开头偏置明显**——20.7% 样本证据恰是 turn#1，38.2% 样本前 5 个 turn 内出现证据，0-9% 位置桶是全局最高峰（16%）。平均相对位置 0.47（略偏中间靠前）。
- ⚠️ 此前的"证据偏后（相对位置 0.66，session 级）"观测已修正：那是按 session 数组顺序统计的误导结果，按时间排序后证据实际整体居中略偏前，且开头有明显高峰。

> 含义：首条 user 消息的模板化证据位置会让任何压缩方法都可能学到位置捷径。Rolling Summary 和 Memory Sidecar 都应额外检查其对首条模板的依赖，同时仍需覆盖首条之外的 45% 证据（跨 session 聚合、中间埋点）。

### 4.3 答案跨 session trajectory 分布（本次重点验证）

**用户观察确认：同一个 question sample 对应多个 session trajectory，答案可能分散在不同 session 中。**

全数据集答案跨 session 数：

| 答案所在 session 数 | 样本数 | 占比 |
|---|---|---|
| 0 | 42 | 4.2% |
| 1 | 392 | 39.2% |
| **2** | **456** | **45.6%** |
| 3 | 60 | 6.0% |
| 4 | 32 | 3.2% |
| 5 | 12 | 1.2% |
| 6 | 6 | 0.6% |

**56.6% 的样本答案分布在 ≥2 个 session。** 其中 multi-session / temporal-reasoning / knowledge-update 三类（688 条）尤为明显：

| 答案跨 session 数 | 样本数 |
|---|---|
| 2 | 456 |
| 1 | 92 |
| 3 | 60 |
| 4 | 32 |
| 0 | 30 |
| 5 | 12 |
| 6 | 6 |

单 session 三类（user / assistant / preference）答案固定在同一 session（median=1）。

### 4.4 跨窗口答案样例

**样例 1** — `0a995998`（multi-session）
- 问题：How many items of clothing do I need to pick up or return from a store?
- 答案：3
- 答案分散在 **3 个** session：`answer_afa9873b_2/3/1`（共 3 个 session）

**样例 2** — `6d550036`（multi-session）
- 问题：How many projects have I led or am I currently leading?
- 答案：2
- 答案分散在 **4 个** session：`answer_ec904b3c_4/2/1/3`（共 4 个 session）

**样例 3** — `gpt4_59c863d7`（multi-session）
- 问题：How many model kits have I worked on or bought?
- 答案分散在 **4 个** session（共 4 个 session）

这类题目要求**跨窗口聚合信息**（数一数分布在 4 个窗口里的项目/衣服/模型数量），是 Memory Sidecar 的核心挑战场景。

## 5. `answer_session_ids` 的可靠性（重要坑）

对比官方 `answer_session_ids` 与消息级 `has_answer`：

- 两者**完全一致**：876/1000（87.6%）
- 但 `answer_session_ids` **恒等于全部 session 列表**：500/1000（50%）

即：**这个镜像数据集里约一半样本的 `answer_session_ids` 没有区分度（被填成全部 session）**，不能用它判断「答案在哪个 session」。

✅ **正确做法**：以消息级 `has_answer` 为唯一证据源（浏览器工具已按此修复）。跨窗口分析也应基于 `has_answer` 推导，而非 `answer_session_ids`。

## 6. 对 Memory Sidecar 实验的含义

1. **长历史 + 逐 session trajectory 记忆匹配**：median 114K token 的 haystack 由多个 session trajectory 组成，正适合「逐 session / 逐 chunk 维护 compact memory」的验证场景；但 Full Context 需要显式处理上下文窗口溢出。
2. **跨 session 聚合是难点**：56.6% 样本需要综合多个 session 的证据，Memory 必须能跨 session 累积并去重（尤其数量类问题，如"有多少个项目"）。
3. **Oracle Evidence 基线更可靠**：用消息级 `has_answer` 而非 `answer_session_ids` 构造 Oracle 上界，避免被镜像数据污染。
4. **模板位置偏差需要单独控制**：答案证据按时间排序后整体略偏前且可能跨多个 session；对 Rolling Summary 和 Memory Sidecar 都应做模板词或 session 顺序敏感性分析，避免把位置捷径误判为记忆能力。

## 7. 复现统计的脚本要点

```python
from datasets import load_from_disk
ds = load_from_disk('data/longmemeval_s')

# 正确获取某样本答案所在 session
def ans_sessions(row):
    sid = row['haystack_session_ids']
    return [sid[i] for i, s in enumerate(row['haystack_sessions'])
            if any(m.get('has_answer') for m in s)]
```

## 8. Excel 字段说明

文件：`data/longmemeval_session_layout.csv`。

该 CSV 一行对应一个完整的 **session trajectory**，不是一个 question sample。一个 question sample 可能包含多个 session，因此同一个 `dataset_index`、`question_id` 和 `question` 会在多行重复出现。

### 8.1 Question sample 字段

| 字段 | 含义 |
|---|---|
| `dataset_index` | question sample 在 Arrow 数据集中的行号，从 0 开始；用于精确定位样本。 |
| `question_id` | 最终问题的唯一标识；在本镜像中同一问题对应一个短 sample 和一个长 sample，不能单独作为 CSV 行的唯一键。 |
| `question_type` | 问题类型，例如 `temporal-reasoning`、`multi-session`、`knowledge-update`。 |
| `question` | 最终要回答的问题；因每一行对应一个 session，会随该 sample 的多个 session 重复。 |
| `question_date` | 最终问题的时间元数据；不能简单当作历史截断时间。 |
| `sample_session_count` | 当前 question sample 包含的 session trajectory 数量。 |

### 8.2 Session 排列字段

| 字段 | 含义 |
|---|---|
| `session_array_order` | 当前 session 在原始 `haystack_sessions` 数组中的顺序，从 1 开始。 |
| `chronological_rank` | 按 `session_date` 从早到晚排序后的顺序，从 1 开始。 |
| `array_order_is_chronological` | 原始数组是否已经按 session 日期从早到晚排列。`False` 表示原始顺序与时间顺序不同。 |
| `session_id` | 当前 session trajectory 的来源 ID。 |
| `session_date` | 当前 session 的日期和时间。它与 `question_date` 含义不同。 |

`session_array_order` 和 `chronological_rank` 必须区分：前者是数据原始排列，后者是按日期重新排序后的排列。实验中若要按时间处理历史，应使用 `chronological_rank`。

### 8.3 Session 规模字段

| 字段 | 含义 |
|---|---|
| `message_count` | 当前 session 中的消息条数。 |
| `message_chars` | 当前 session 所有消息正文的字符数，用于粗略估计上下文规模，不等于精确 token 数。 |
| `first_message_preview` | 当前 session 第一条消息的前 160 个字符，用于人工快速识别 session 内容；不是完整 session 内容。 |

### 8.4 证据字段

| 字段 | 含义 |
|---|---|
| `has_answer` | 当前 session 中是否至少有一条消息被标记为答案证据。 |
| `answer_message_indices` | 当前 session 中被标记为证据的消息下标，从 0 开始；例如 `0` 表示第 1 条消息，`0;5` 表示第 1 条和第 6 条消息。 |

如果 `answer_message_indices` 为空，含义是“当前这一行对应的 session 没有被标记为答案证据”，不代表整个 question sample 没有答案。应按 `dataset_index` 汇总同一问题的所有 session 后再判断：只有当该 sample 的所有 session 都为空时，才是整个 sample 没有标记证据的情况。

## 9. 排列核对文件

使用 `data/export_session_layout.py` 生成 `data/longmemeval_session_layout.csv`。
该文件一行对应一个 session trajectory，保留人工核对所需的核心字段：

- `dataset_index`：question sample 在 Arrow 数据集中的行号
- `session_array_order`：该 session 在 `haystack_sessions` 中的原始顺序（从 1 开始）
- `chronological_rank`：按 `haystack_dates` 排序后的位置
- `session_id`、`session_date`、消息数、字符数、证据消息下标和首条消息预览

原始数组顺序和时间排序顺序同时保留，避免把 session 的排列误当成时间顺序。

## 10. 官方参考

- 官方仓库：[xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval)
- 官方论文：[LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory](https://arxiv.org/abs/2410.10813)
- 官方清洗数据：[xiaowu0162/longmemeval-cleaned](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
已删除重复或不可靠的字段：`session_array_index`、`message_roles`、`first_message_role` 和 `raw_answer_session_ids`。
