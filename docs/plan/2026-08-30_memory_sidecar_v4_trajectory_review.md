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

## 本轮针对性修复（2026-08-30）

### 硬币集合计数

`69fee5aa` 的轨迹同时出现了历史总数 `37 pre-1920 American coins` 和后续新增的
`1915-S Barber quarter`。旧 count projection 只读取数值关系，导致 `MENTIONS` 或
`PLANS` 中的明确数量被漏掉；第一次扩大筛选又把计划动作和其他年代硬币算入，得到 43。

现改为只投影两类事实：

- object 明确包含 `pre-1920 ... coins` 的集合总数；
- scope 明确为 `pre-1920 American coins` 的单件硬币，程序计数为 1。

已落库 Manager 图谱的离线验证结果为 `37 + 1 = 38`，并记录
`count_projection_mode=collection_aggregate_plus_items`，不再计入无关事实。

### 博物馆访问日期

`gpt4_59149c77` 的第二次访问含 `today`，已按 evidence session date 解析为
`2023-01-15`。第一次访问原文是“刚从导览回来”，没有显式日期；对唯一 evidence 且
关系为 `ATTENDED` 的事实，程序使用 session date 作为代理日期，并写入
`event_date_from_session` 审计动作，使 projection 能保留两次访问及其日期。

### 验证状态

- `tests/test_sidecar_v4.py`: 39 passed。
- 两个在线重跑分别写入 59/60 个 Manager batch 和 177/165 条边；上游服务随后长时间无响应，
  因此中止最后一次 Manager 请求。两份数据库保留了已完成 batch 和 graph edges。
- 复用已落库图谱做 Answer-only 验证：硬币样本输出 `38`；博物馆样本在内存应用本轮
  `event_date_from_session` 后输出 `7 days`。后者说明修复有效，但要形成正式端到端轨迹，
  仍需服务恢复后重新完成该样本并把修复后的时间字段写入 SQLite。
