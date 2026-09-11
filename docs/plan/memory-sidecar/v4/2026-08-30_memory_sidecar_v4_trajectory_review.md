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

## 曾尝试但已撤回的样本特化修复（2026-08-30）

本节只保留诊断记录，不属于正式 V4 规则。

### 硬币集合计数

`69fee5aa` 的轨迹同时出现了历史总数 `37 pre-1920 American coins` 和后续新增的
`1915-S Barber quarter`。旧 count projection 只读取数值关系，导致 `MENTIONS` 或
`PLANS` 中的明确数量被漏掉；第一次扩大筛选又把计划动作和其他年代硬币算入，得到 43。

曾尝试增加只识别 `pre-1920` 和 `coin` 的 projection，并把单件硬币计为 1。该规则
可以让这个样本得到 38，但依赖测试问题中的具体词汇，属于数据泄露风险，现已从正式 V4
代码和测试中撤回。正确方向应是通用的集合 scope、快照/增量语义和 evidence 顺序处理。

### 博物馆访问日期

`gpt4_59149c77` 的第二次访问含 `today`，按 evidence session date 解析是通用规则，
现予保留。曾尝试对所有无显式时间的 `ATTENDED` 事实直接使用 session date；这会把
会话时间误当成事件时间，现已撤回。后续应分离 `valid_time` 与 `reported_time`，不做
未经证据支持的事件日期补全。

### 验证状态

- `tests/test_sidecar_v4.py`: 37 passed（撤回两个样本特化测试后）。
- 两个在线重跑分别写入 59/60 个 Manager batch 和 177/165 条边；上游服务随后长时间无响应，
  因此中止最后一次 Manager 请求。两份数据库保留了已完成 batch 和 graph edges。
- 复用特化版本图谱做的 Answer-only 结果（硬币 `38`、博物馆 `7 days`）只作为诊断记录，
  不计入正式 V4 准确率。

## 干净 V4 基线整改

随后在 `2ab3099` 基础上进行了基线清理：

- 删除所有 `coin/pre-1920` 专用数量投影和单件计数规则；
- 删除无显式时间时针对 `ATTENDED` 的 session-date 事件补全；
- 保留通用的 `today/yesterday/tomorrow` 相对时间解析；
- 将问题相关性筛选统一为 object/provider/location/scope 的词汇交集，不再使用
  museum、bike、course、album 等测试样本名称分支；
- 关系别名、数量正则、provider 金额关联、occurrence 去重和 SQLite 审计均保持不变。

该版本恢复为可用于正式对比的 V4 baseline。样本特化版本的结果和轨迹不得混入 V4
准确率统计；后续如需增强集合计数，应新增通用 schema（集合 scope、快照/增量关系和
证据顺序），并在冻结协议后重新实验。
