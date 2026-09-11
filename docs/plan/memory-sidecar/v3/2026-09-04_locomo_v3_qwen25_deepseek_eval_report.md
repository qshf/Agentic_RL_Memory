# LOCOMO V3 Qwen 7B 全量评测报告

## 0. LOCOMO 数据集说明与测试必要性

### 0.1 数据集是什么

LOCOMO（Long-Context Conversation）是面向长期、多会话记忆问答的基准数据集。本地文件
`data/official_locomo/locomo10.json` 包含 10 个长对话样本。每个样本由多个带日期的
session 组成，包含两位固定说话人、按时间排列的对话消息，以及针对同一对话的多条 QA。
QA 记录通常包含 `question`、`answer`、`evidence` 和 `category`；其中 `evidence` 指向
支持答案的对话片段（例如 `D1:3`）。类别 5 使用 `adversarial_answer`，不是普通的事实问答
答案。

本地数据的实际分布如下：

| 类别 | 任务 | 数量 | 主要考察能力 |
| --- | --- | ---: | --- |
| 1 | multi-hop | 282 | 从多个事实或多个 session 组合答案 |
| 2 | temporal reasoning | 321 | 日期、先后顺序、相对时间和时间更新 |
| 3 | open-domain | 96 | 基于对话事实进行开放式概括或推断 |
| 4 | single-hop | 841 | 从单条或局部证据直接检索事实 |
| 5 | adversarial | 446 | 识别问题与对话证据不匹配并正确拒答 |
| **合计** |  | **1,986** |  |

### 0.2 为什么需要测试 LOCOMO

LongMemEval 更偏向统一的长历史问答；LOCOMO 则把同一段长期对话拆成多个有明确日期的
session，并提供 evidence 标注。因此它能更直接检验当前 Memory Manager 的核心目标：

- 是否能跨 chunk、跨 session 保留事实；
- 是否能把人物、事件、偏好和时间关系整理成可复用的上下文；
- 是否会因摘要、更新或去重而丢失旧事实；
- Answer 是否能利用构造后的 memory 解决多跳和时序问题；
- evidence 不足时是否避免编造答案。

LOCOMO 适合作为 LongMemEval 之外的第二个验证集，但它不能单独证明 Manager 的抽取质量。
当前实验是端到端链路：Qwen 负责 Memory build 和 Answer，DeepSeek 负责答案判定。因此
结果同时受抽取、记忆维护、上下文组织、回答和 judge 影响。

### 0.3 与 LongMemEval 的关系和解读边界

两套数据集应使用相同的 runner 约定、模型参数和 judge 口径进行横向参考，但不应直接把
准确率当成论文排名：任务构成、答案长度、题型比例、evidence 标注方式和评测指标可能不同。
LOCOMO 的类别 1--4 是本报告的主指标；类别 5 需要“是否应拒答”的专用 rubric，不能用普通
正确性 prompt 得出的数字代表官方 adversarial accuracy。

本次采用“每个对话一次 Memory build、该对话全部 QA 复用同一份 SQLite memory”的方式。
这样测试的是稳定的长期记忆状态，而不是每道题临时重建记忆。每条 QA 的 evidence、生成
上下文、Answer、DeepSeek 判定和轨迹均保留在结果目录，便于区分是 memory 丢失、上下文
选择错误，还是 Answer/Judge 阶段出错。

## 1. 实验结论

使用 9934 服务器本地部署的 `Qwen2.5-7B-Instruct`，对 LOCOMO 全部 10 个对话、1,986 条 QA 完成了 V3 记忆构建和 Answer 生成。所有样本均完成，没有 `failed` 或 `running` 记录。

DeepSeek `deepseek-v4-pro` 对常规类别 1--4 的 1,540 条答案进行判定，结果为：

> **726/1,540，准确率 47.14%**

仓库中同时存在一份包含类别 5 的全量判定，结果为 943/1,986（47.48%）。类别 5 是 adversarial 题，官方数据没有普通 `answer`，该结果使用通用 rubric，只作为补充，不作为与论文结果对比的主指标。

## 2. 数据集与样本分布

| LOCOMO 类别 | 含义 | 样本数 | 主评测 |
| --- | --- | ---: | --- |
| 1 | multi-hop | 282 | 是 |
| 2 | temporal | 321 | 是 |
| 3 | open-domain | 96 | 是 |
| 4 | single-hop | 841 | 是 |
| 5 | adversarial | 446 | 否，单独口径 |
| **合计** |  | **1,986** |  |

本次按照对话拆分，每个对话先执行一次 Memory build，再从落盘的 Memory SQLite 重放所有 QA 的 Answer 阶段。这样同一对话的全部问题复用同一份记忆状态，避免每道题重复构建 Manager memory。

## 3. 运行配置

| 项目 | 配置 |
| --- | --- |
| Memory/Answer 协议 | V3 `memory-sidecar-v3-multi-event-atomic-batch` |
| Manager/Answer 模型 | `Qwen2.5-7B-Instruct`（sglang） |
| 服务访问 | SSH 隧道本地 `http://127.0.0.1:30001/v1` |
| temperature | 0.0 |
| thinking | disabled |
| chunk | 2,048 tokens |
| Manager context | 12,288 tokens |
| active memory | 8,192 tokens |
| update ledger | 2,048 tokens |
| raw tail | 16,384 tokens |
| shared context | 16,384 tokens |
| Manager max output | 4,096 tokens |
| Answer max output | 1,024 tokens |
| compactor | off |
| Answer 并发 | 2 |
| Memory 并发 | 1 |

Answer 阶段通过 `replay_source_db` 和 `replay_source_run_id` 读取该对话的 Memory build 结果；没有重新调用 Manager。每个阶段均记录 `config.json`、`run_summary.json`、`hypotheses.jsonl` 和 `trajectory.sqlite3`。

## 4. DeepSeek 评测结果

主指标只统计类别 1--4。评测使用 `longmemeval-official-rubric-deepseek-v2` 判定，1,540 条请求全部成功。

| 类别 | 正确 | 总数 | 准确率 |
| --- | ---: | ---: | ---: |
| multi-hop | 82 | 282 | 29.08% |
| temporal | 69 | 321 | 21.50% |
| open-domain | 32 | 96 | 33.33% |
| single-hop | 543 | 841 | 64.57% |
| **类别 1--4 合计** | **726** | **1,540** | **47.14%** |

补充的全量通用 rubric 结果：

| 范围 | 正确 | 总数 | 准确率 |
| --- | ---: | ---: | ---: |
| 类别 1--4 | 726 | 1,540 | 47.14% |
| 类别 5 adversarial | 215 | 446 | 48.21% |
| **类别 1--5** | **943** | **1,986** | **47.48%** |

类别 5 的 215/446 不能直接解释为官方 adversarial accuracy，因为本次全量文件沿用了普通正确性 prompt；后续如需论文对比，应使用专门的“是否正确拒答/识别不可回答”判定协议。

## 5. Qwen 运行成本

10 个 Memory build 和 10 个 Answer replay 均已完成：

| 阶段 | 样本数 | LLM calls | 输入 tokens | 输出 tokens | 墙钟时间 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Memory build | 10 个对话 | 111 | 395,523 | 59,011 | 约 16.1 分钟 |
| Answer replay | 1,986 QA | 1,986 | 29,941,052 | 97,821 | 约 57.7 分钟 |

Answer 阶段运行时间约 0.961 小时；Memory 阶段约 0.268 小时。输入 token 较高的原因是每道 Answer 都带有完整的历史上下文、V3 memory 和 raw tail，且 Qwen 服务的上下文窗口配置为 131,072。

## 6. 对话级完成情况

| 对话 | QA 数 | Memory | Answer |
| --- | ---: | --- | --- |
| conv-26 | 199 | completed | completed |
| conv-30 | 105 | completed | completed |
| conv-41 | 193 | completed | completed |
| conv-42 | 260 | completed | completed |
| conv-43 | 242 | completed | completed |
| conv-44 | 158 | completed | completed |
| conv-47 | 190 | completed | completed |
| conv-48 | 239 | completed | completed |
| conv-49 | 196 | completed | completed |
| conv-50 | 204 | completed | completed |
| **合计** | **1,986** |  |  |

## 7. 9934 服务状态

Qwen 服务为容器 `sglang-qwen25-7b`，实际模型为 `Qwen2.5-7B-Instruct`，通过 `127.0.0.1:30001 -> container:30000` 暴露。另一个已有容器 `slime-agentic-rl-4090-sglang-1` 运行 `Qwen3-0.6B`，本次未使用，也未停止其他容器。

评测结束后检查到 RTX 4090：GPU 利用率 0%、温度约 30°C、功耗约 16.6W、风扇约 30%。本地 runner 和 SSH 隧道均已退出。

## 8. 可复核文件

- 全量结果目录：[results/locomo_v3_qwen25_all_20260904](/Users/qshf/my-project/Agentic_RL_Memory/results/locomo_v3_qwen25_all_20260904)
- 类别 1--4 source：[locomo_source_cat1_4.json](/Users/qshf/my-project/Agentic_RL_Memory/results/locomo_v3_qwen25_all_20260904/locomo_source_cat1_4.json)
- 类别 1--4 hypotheses：[locomo_hypotheses_cat1_4.jsonl](/Users/qshf/my-project/Agentic_RL_Memory/results/locomo_v3_qwen25_all_20260904/locomo_hypotheses_cat1_4.jsonl)
- 类别 1--4 判定：[deepseek_judgments_cat1_4.jsonl](/Users/qshf/my-project/Agentic_RL_Memory/results/locomo_v3_qwen25_all_20260904/deepseek_judgments_cat1_4.jsonl)
- 类别 1--4 汇总：[deepseek_judgments_cat1_4_summary.json](/Users/qshf/my-project/Agentic_RL_Memory/results/locomo_v3_qwen25_all_20260904/deepseek_judgments_cat1_4_summary.json)
- 全量通用 rubric 汇总：[deepseek_judgments_all_summary.json](/Users/qshf/my-project/Agentic_RL_Memory/results/locomo_v3_qwen25_all_20260904/deepseek_judgments_all_summary.json)
- 每个对话的 `config.json`、`run_summary.json`、`hypotheses.jsonl`、`trajectory.sqlite3` 均位于结果目录下对应的 `conv_*/memory_build/` 和 `conv_*/answers/` 子目录。

## 9. 结果解释边界

本实验测量的是“Qwen 7B 生成答案 + DeepSeek 判定”在 V3 上的端到端效果，不是纯 Manager 抽取准确率。LOCOMO 的单跳题明显高于多跳和时序题，说明当前主要瓶颈仍在长程关系组合、时间推理和 Answer 对上下文的利用，而不只是数据库写入或 SQLite 可靠性。

此外，类别 1--4 的 47.14% 是本项目当前评测口径下的结果；不同论文可能使用 F1、BLEU、GPT judge 或不同的类别 5 处理方式，不能直接把数字当作严格同口径的论文排名。
