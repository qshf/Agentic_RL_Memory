# V4 轨迹评审与针对性修复

## 评审范围

本次检查使用 `sidecar-v4-pilot24-lock2-20260829` 的 SQLite 轨迹、V4 实现和
`tests/test_sidecar_v4.py`。Answer 提示词、raw tail 和主实验配置未修改。

## 轨迹发现

1. `d24813b1` 有 3 条真实的 `uses` 关系被记录为 `unknown_relation`，因此没有进入
   可查询 graph。原因是模型输出了 `uses`，但关系别名和 Manager 提示词没有覆盖它。
2. 数量解析只覆盖 `courses/coins/items/days`。植物、专辑、书籍、毕业典礼等离散实体
   无法进入 typed `count`；同时 `7-10 days` 会错误读取上界 `10`，存在错误聚合风险。
3. 时间投影在 wake/bed 问题中只按 object 文本筛选。`OBSERVED_WAKE_TIME -> 7:30 am`
   这类正确边的 object 不包含 `wake`，会被排除。
4. graph-all 原本只显示时间区间起点，且不显示 `contradicted` 状态，Answer 难以区分
   计划/观察的区间和冲突事实。

## 已实施修复

- 增加 `uses/use -> USES` 归一化，并将 `USES` 纳入 occurrence predicates 和 Manager
  允许关系列表。
- 扩充离散数量词：`plants/albums/books/graduations/events/trips/nights`；用负向边界
  防止把 `7-10` 的上界当成单值 count。
- wake/bed 查询保留 `OBSERVED_WAKE_TIME`，不再要求 object 必须包含 wake/bed 关键词。
- graph renderer 展示 `time=start..interval_end`，并显式展示 `status=contradicted`。

## 影响与边界

这些修复只影响 V4 的关系归一化、typed attribute 和上下文投影，不改变 Answer prompt，
也不改变 occurrence 去重的核心规则。修复后应先在原 24 条 manifest 重跑 V4，不能把旧
轨迹和新轨迹混合统计；若准确率仍为 12/24，再进行 query-aware projection replay，
最后才决定是否进入 V5 或 120 条确认实验。

