# Memory Sidecar Agentic RL 方案

**API 可行性验证 → 小模型蒸馏 → Slime 强化学习**

## 1. 项目定位

研究对象：面向长 session archive 的轻量级 **Memory Controller / Memory Sidecar**，而不是让小模型自己承担完整长上下文推理。

LongMemEval 是受控的、混合来源的多 session haystack，不是自然用户连续日志，也不是在线 Agent 自己生成的工具轨迹。第一阶段验证的是“长历史中的证据压缩与抗干扰”，不能直接外推为真实用户记忆效果。

一个 question sample 的 haystack 中位数约 114K token，但 Memory Agent 的单次输入保持短上下文。

Memory Agent 只负责“什么值得记、如何更新、何时删除/忽略”，复杂推理和工具执行仍交给强模型。

最终目标不是“记得更多”，而是在有限记忆和计算预算下，让历史信息更有效地支持未来回答或工具行动。

第一阶段核心研究问题：

> 在不训练模型的情况下，使用强 API 作为 Memory Agent，逐个 session trajectory 维护 compact memory，是否能以更低的最终上下文成本接近 Full Context，并优于通用的递归摘要压缩？

## 2. 系统形态

```text
Question Sample / Session Archive
        ↓
Session Trajectories → Memory Agent API → Memory Store
                              ↓
Final Question → Strong Model → Final Result
```

Memory Agent 每一步建议输入：

- 当前 session trajectory / 当前 session chunk
- 当前 compact memory
- 固定 memory policy instruction

Memory Agent 每一步输出：

- `ADD`：加入新的长期有用信息
- `UPDATE`：已有事实/要求发生变化
- `DELETE`：旧信息明确失效或被撤销
- `NOOP`：当前 session/chunk 不值得写入长期记忆

关键约束：Memory Agent 在历史处理阶段不读取最终问题；它只读取“当前 session/chunk + 当前 memory”，避免把问题相关检索误当成一般化记忆。最终问题只在 Answer Model 阶段加入。

## 3. 第一阶段数据集选择

主数据集：**LongMemEval**。

官方 LongMemEval 发布三个独立文件：`longmemeval_s_cleaned.json`、`longmemeval_oracle.json` 和 `longmemeval_m_cleaned.json`，每个文件包含 500 个 evaluation instances。本地官方下载目录为 `data/official_longmemeval/`：S 含 38–62 个 session（主评测），Oracle 只含 1–6 个证据 session（上界），M 含 460–490 个 session（后续扩展）。三者不可合并为 1,000 条或 1,500 条主数据集。

数据单位固定为：`question sample → haystack → 多个 session trajectory → messages`。一个最终问题对应多个 session，不能将 session 当成 question sample。同一官方文件内使用 `question_id` 标识样本；跨文件比较时使用 `(split_name, question_id)`。主结果只在 S 的 500 条样本上报告；Oracle 只作 Evidence 上界，不参与 S 的训练或验证切分。

第一轮规模建议：从官方 S 的 500 个问题中选定 50–100 个进行 API prototype；验证机制有效后再扩到完整 S。M 留到机制、预算和成本控制稳定后再运行。

## 4. 第一阶段：在线 API 可行性验证（当前优先）

目标：**不做 SFT、不做 RL，只验证 Memory Sidecar 机制本身是否有效。**

基本流程：

1. 读取 LongMemEval 的完整 haystack；一个 session 作为一条完整 session trajectory，过长 session 再按消息边界拆成较小 chunk。
2. 使用强模型 API 作为 Memory Agent。每次仅输入“当前 chunk + 当前 compact memory”，输出 `ADD / UPDATE / DELETE / NOOP`，并更新 Memory Store。
3. 历史全部处理完后，把“最终问题 + compact memory”输入强 Answer Model，生成最终答案。
4. 使用 benchmark 的 gold answer / 官方 evaluator 计算准确率，并保存每个 question sample 的完整 memory trajectory（逐个 session trajectory/chunk 的记忆更新日志）。
5. 在官方 LongMemEval-S 上与 Full Context、Rolling Summary、Memory Sidecar 统一比较；另在官方 Oracle 文件上运行 Oracle Evidence 上界。结果以 `(split_name, question_id)` 为样本单位，并按问题类型、S 的 haystack 规模分组报告。

建议的第一版 Memory Store：先保持简单、可读、可审计，不做向量数据库。条目应保留事实/事件、事件时间、当前或已过期状态、来源 session/message。数量题应累计可去重的原子事件集合；知识更新应把旧值标记为 `superseded`，而不是静默删除。

## 5. 必做对照实验

建议至少保留以下四组：

| 方法 | 输入方式 | 作用 |
|---|---|---|
| Full Context | 完整历史 + 最终问题 | 性能上界/高成本基线 |
| Rolling Summary | 超过上下文窗口时递归压缩历史；最终摘要/尾部 + 最终问题 | 通用持续压缩基线 |
| Memory Sidecar | 逐 session/chunk 更新 structured compact memory + 最终问题 | 核心方法 |
| Oracle Evidence | gold evidence + 最终问题 | 理想记忆上界 |

Rolling Summary 的压缩阶段也不读取最终问题：`summary + 未压缩尾部 + 当前 chunk` 超过窗口时，压缩器将已有上下文压回预算内，再继续处理后续 chunk。它与 Memory Sidecar 使用相同顺序、相同窗口和相同最终 Answer Model；差异仅在于前者输出自然语言摘要，后者输出结构化记忆。

公平性原则：固定最终 Answer Model、压缩器模型、窗口、最终 memory/summary token budget 与 decoding 设置；记录每种方法实际输入 token、总 API token、压缩调用次数、成本与延迟。Memory Sidecar 的优势必须来自“更好的历史信息选择”，而不是更强的 Answer Model。

## 6. 第一阶段评价指标与 Go / No-Go 门槛

重点指标：

- **Final QA Accuracy / Task Success**：最重要指标
- **Answer Context Tokens**：最终强模型实际需要读取多少历史信息
- **Total API Tokens / Cost**：Memory Agent 逐步处理历史造成的总成本
- **Memory Size**：最终 compact memory token 数及随 session/chunk 的增长曲线
- **Update Correctness**：发生知识更新时，旧值是否被正确替换而非并存
- **Memory Error Analysis**：漏记、误记、过期记忆、重复记忆四类错误
- **Haystack-Size Robustness**：在 S 子集内部按 session 数量分组报告性能与成本；不把 Oracle 子集作为 S 的短历史组

建议的工程 Go 条件（非 benchmark 官方标准）：

> Memory Sidecar 在 LongMemEval-S 子集上优于 Rolling Summary；同时在远低于 Full Context 的最终上下文预算下，性能能够接近 Full Context。

若 Sidecar 在强 API 上都没有显示出稳定收益，或只依赖数据中的模板信号才有效，则先修改 memory policy / schema / chunk 策略，而不是进入 RL。

## 7. 小模型与硬件约束下的设计原则

目标训练模型：4B 以下；硬件为 4×RTX 5090。

原实施方案建议：

- 0.5B 用于冒烟
- 1.5B–1.7B 用于第一次真实全参数闭环
- 约 4B 需要经过显存与吞吐门控

关键设计原则：

- 把“100K”定义为 question sample 的完整 haystack 长度，而不是 Memory Agent 单次 context
- 第一版将 Memory Agent 单步 context 控制在约 2K–4K token
- memory 超预算时，后续再引入合并、压缩和遗忘
- RL 阶段优先较小 group size 与较短 rollout 上下文，避免 `4B × 长 context × 多样本 rollout` 带来的吞吐问题

## 8. 后续路线（略）

### 阶段 2：SFT 蒸馏

将 API Memory Agent 产生的高质量 trajectory 经过 verifier / 结果一致性过滤，转为 SFT 数据，使 1.5B–4B 小模型学会基础 memory policy 与动作协议。

### 阶段 3：Slime + GRPO

把独立 Agent Loop 接入 Slime：

- `custom_generate` 负责多轮 Memory-Agent-Environment rollout
- `custom_rm` / verifier 根据最终任务正确性、memory 状态与成本产生 reward
- 训练只对 Agent 自己生成的 action/token 计算策略损失

### 阶段 4：有限预算与 Agentic 任务

加入固定 memory slot/token budget、`MERGE / COMPRESS / EVICT`，并迁移到随机化干扰历史、LongMemEval-V2 或真实 Code Agent trajectory，验证小型 Memory Sidecar 是否能持续辅助强工具型 Agent。

## 9. 第一阶段交付物

- LongMemEval 数据加载与 turn/session stream 转换脚本
- Memory Agent API runner 与可替换 prompt
- 可持久化、可回放的 Memory Store / trajectory 日志
- Full Context / Rolling Summary / Memory Sidecar / Oracle Evidence 四组实验结果
- 逐题错误分析与 token / 成本统计
- 是否进入 SFT/RL 的 Go / No-Go 结论

## 10. 依据与参考

1. 《Memory Agentic RL 实施方案》：项目总体目标、阶段划分、环境/轨迹/Verifier/Trainer 解耦、0.5B→1.7B→4B 训练顺序，以及 Slime 接入建议。
2. [LongMemEval 官方仓库](https://github.com/xiaowu0162/LongMemEval)：三个独立文件各含 500 个 evaluation instances；S 版本约 40 sessions / 约 115K tokens，Oracle 版本只保留证据相关历史。
3. [LongMemEval 官方论文](https://arxiv.org/abs/2410.10813)：数据构造、五类长期记忆能力和评测方法。
4. [官方清洗数据](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)：`longmemeval_s_cleaned.json`、`longmemeval_oracle.json` 与 `longmemeval_m_cleaned.json`。
5. LongMemEval-V2 官方仓库（2026）：基于长 web-agent trajectories 的 agentic memory benchmark，可用于后续从对话记忆迁移到工具/Code Agent 场景。
