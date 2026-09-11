# 实验与方案文档导航

本目录按记忆系统的版本演进归档。文件名保留原始日期和标题，便于将设计决策与代码、实验产物和轨迹对应。

## 当前入口

| 状态 | 文档 | 说明 |
| --- | --- | --- |
| Current | [V5 事件束事实抽取](memory-sidecar/v5/2026-09-06_memory_sidecar_v5_event_bundle_extraction_plan.md) | 当前 V5 执行方案；以 shadow extraction 验证事件、数值、时间和证据绑定。 |
| Deferred | [V6 数字事实账本](memory-sidecar/v6/2026-09-05_memory_sidecar_v5_numeric_ledger_plan.md) | typed ledger、QuerySpec 与程序计算的延期预研。 |
| Reference | [LongMemEval 原始数据与架构分析](research/2026-09-06_longmemeval_raw_data_memory_architecture_analysis.md) | V5/V6 后续设计的原始数据和题型依据。 |

## 推荐阅读顺序

1. [Rolling Summary Baseline V1](baseline/rolling-summary/v1/2026-08-21_rolling_summary_baseline_v1.md) 与[评估记录](baseline/rolling-summary/v1/2026-08-21_rolling_summary_baseline_v1_evaluation.md)
2. [Memory Sidecar V3 方案](memory-sidecar/v3/2026-08-25_memory_sidecar_v3_solution.md) 与 [V3/V4 120 条对照](memory-sidecar/comparisons/2026-09-01_memory_sidecar_v3_v4_eval120_report.md)
3. [V4 图谱化事实记忆](memory-sidecar/v4/2026-08-28_memory_sidecar_v4_graph_plan.md) 与 V4 评测/轨迹复盘
4. [当前 V5 事件束事实抽取](memory-sidecar/v5/2026-09-06_memory_sidecar_v5_event_bundle_extraction_plan.md)
5. [延期 V6 数字事实账本](memory-sidecar/v6/2026-09-05_memory_sidecar_v5_numeric_ledger_plan.md)

## 目录说明

| 目录 | 内容与状态 |
| --- | --- |
| [`baseline/rolling-summary/v1/`](baseline/rolling-summary/v1/) | Rolling Summary V1 方案、评估记录和人工评审页面。 |
| [`memory-sidecar/v1/`](memory-sidecar/v1/) | V1 可行性验证、实现记录与 Pilot 对比。 |
| [`memory-sidecar/v2/`](memory-sidecar/v2/) | V2 方案和单样本处理流程。 |
| [`memory-sidecar/v3/`](memory-sidecar/v3/) | V3 方案、smoke 审计、24 条评测和 LongMemEval/LOCOMO 报告。 |
| [`memory-sidecar/v4/`](memory-sidecar/v4/) | 图谱方案、claim/smoke/pilot 评测和轨迹修复记录。 |
| [`memory-sidecar/v5/`](memory-sidecar/v5/) | 当前 V5 事件束事实抽取方案。 |
| [`memory-sidecar/v6/`](memory-sidecar/v6/) | 延期的数字事实账本预研。 |
| [`memory-sidecar/comparisons/`](memory-sidecar/comparisons/) | V1/V2 和 V3/V4 的跨版本对照报告。 |
| [`research/`](research/) | 数据与架构研究材料。 |
| [`operations/`](operations/) | 本地模型与评测部署说明。 |

## 文档清单

| 版本/类别 | 文档 | 用途 | 状态 |
| --- | --- | --- | --- |
| Baseline V1 | [实施方案](baseline/rolling-summary/v1/2026-08-21_rolling_summary_baseline_v1.md) | Rolling Summary 对照基线 | Reference |
| Baseline V1 | [评估记录](baseline/rolling-summary/v1/2026-08-21_rolling_summary_baseline_v1_evaluation.md) | 120 条结果与错误分析 | Reference |
| Baseline V1 | [人工评审](baseline/rolling-summary/v1/rolling_summary_v1_manual_review.html) | 评测结果浏览页面 | Reference |
| Sidecar V1 | [快速可行性验证计划](memory-sidecar/v1/2026-08-22_memory_sidecar_pilot_v1.md) | 初始研究假设和门槛 | Historical |
| Sidecar V1 | [Strong V1 实施记录](memory-sidecar/v1/2026-08-22_memory_sidecar_strong_v1_implementation.md) | 实际处理链路说明 | Historical |
| Sidecar V1 | [Pilot 对比报告](memory-sidecar/v1/2026-08-23_memory_sidecar_pilot_v1_evaluation.md) | Sidecar 与 Rolling 对照 | Historical |
| Sidecar V2 | [V2 解决方案](memory-sidecar/v2/2026-08-24_memory_sidecar_v2_solution.md) | 版本化记忆设计 | Historical |
| Sidecar V2 | [单样本处理流程](memory-sidecar/v2/2026-08-24_memory_sidecar_v2_process_flow.md) | `process_one_v2()` 执行流程 | Historical |
| Sidecar V3 | [V3 方案](memory-sidecar/v3/2026-08-25_memory_sidecar_v3_solution.md) | V3 记忆协议与验收 | Reference |
| Sidecar V3 | [Smoke 轨迹审计](memory-sidecar/v3/2026-08-27_memory_sidecar_v3_smoke_trace_review.md) | 问题定位与整改 | Historical |
| Sidecar V3 | [2048/24 条评测](memory-sidecar/v3/2026-08-28_memory_sidecar_v3_2048_24_evaluation.md) | chunk 配置对照 | Reference |
| Sidecar V3 | [LongMemEval Oracle 500](memory-sidecar/v3/2026-09-04_memory_sidecar_v3_longmemeval_oracle500_report.md) | Oracle 500 实验记录 | Reference |
| Sidecar V3 | [LOCOMO Qwen 7B](memory-sidecar/v3/2026-09-04_locomo_v3_qwen25_deepseek_eval_report.md) | LOCOMO 全量评测 | Reference |
| Sidecar V4 | [图谱化事实记忆](memory-sidecar/v4/2026-08-28_memory_sidecar_v4_graph_plan.md) | 图谱实验方案 | Historical |
| Sidecar V4 | [最小 Claim Smoke](memory-sidecar/v4/2026-08-28_memory_sidecar_v4_claim_smoke.md) | Claim 协议验证 | Historical |
| Sidecar V4 | [Manager 上下文与去重](memory-sidecar/v4/2026-08-29_memory_sidecar_v4_manager_context_dedupe_improvement.md) | V4 修复方案 | Historical |
| Sidecar V4 | [24 条 Pilot](memory-sidecar/v4/2026-08-29_memory_sidecar_v4_pilot24_report.md) | Pilot 评测 | Historical |
| Sidecar V4 | [四样本 Smoke](memory-sidecar/v4/2026-08-29_memory_sidecar_v4_smoke4_evaluation.md) | 修复验证 | Historical |
| Sidecar V4 | [错误样本重跑](memory-sidecar/v4/2026-08-30_memory_sidecar_v4_error_rerun.md) | 修复后结果 | Historical |
| Sidecar V4 | [轨迹评审](memory-sidecar/v4/2026-08-30_memory_sidecar_v4_trajectory_review.md) | 轨迹分析与修复 | Historical |
| Sidecar V5 | [事件束事实抽取](memory-sidecar/v5/2026-09-06_memory_sidecar_v5_event_bundle_extraction_plan.md) | Shadow extraction 当前方案 | Current |
| Sidecar V6 | [数字事实账本](memory-sidecar/v6/2026-09-05_memory_sidecar_v5_numeric_ledger_plan.md) | typed ledger 预研 | Deferred |
| 跨版本 | [V1/V2 对比](memory-sidecar/comparisons/2026-08-24_memory_sidecar_v1_v2_comparison.md) | V1 与 V2 实验对照 | Reference |
| 跨版本 | [V3/V4 全量 120 条](memory-sidecar/comparisons/2026-09-01_memory_sidecar_v3_v4_eval120_report.md) | 全量结果与结论 | Reference |
| 跨版本 | [V3/V4 强模型 24 条复测](memory-sidecar/comparisons/2026-09-06_memory_sidecar_v3_v4_pilot24_qwen38_report.md) | 强模型配对复测 | Reference |
| 研究 | [LongMemEval 数据与架构分析](research/2026-09-06_longmemeval_raw_data_memory_architecture_analysis.md) | 后续架构依据 | Reference |
| 运维 | [Qwen2.5 SGLang 部署](operations/2026-09-03_qwen25_sglang_deployment.md) | 本地模型评测部署 | Reference |

## V5 历史候选

以下方案保留完整内容，供回溯设计决策；它们不代表当前实现主线。

| 文档 | 归档原因 |
| --- | --- |
| [混合路由实验计划](archive/memory-sidecar/v5/2026-08-29_memory_sidecar_v5_hybrid_router_plan.md) | 图谱与 V3 的混合路由早期探索，未成为现行方案。 |
| [数字与时间专用记忆](archive/memory-sidecar/v5/2026-09-04_memory_sidecar_v5_numeric_temporal_plan.md) | 后续收敛为抽取层验证和延期的 V6 账本。 |
| [原始事实抽取](archive/memory-sidecar/v5/2026-09-05_memory_sidecar_v5_raw_fact_extraction_plan.md) | raw-span 草案，已由当前事件束抽取方案替代。 |
| [问题驱动证据选择](archive/memory-sidecar/v5/2026-09-06_memory_sidecar_v5_question_selective_memory_plan.md) | 未选定的原文证据选择路线。 |
| [Selective Sentence Memory](archive/memory-sidecar/v5/2026-09-06_memory_sidecar_v5_selective_sentence_memory_plan.md) | 未选定的原文证据选择路线。 |

`memory_sidecar/v5_demo.py` 是 flat-fact 旧基线，当前文档中没有与其一一对应的活动设计说明；它不应被视为当前 V5 方案。
