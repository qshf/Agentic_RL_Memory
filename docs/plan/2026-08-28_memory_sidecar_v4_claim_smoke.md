# V4 最小 Claim Smoke 测试记录

## 1. 测试范围

使用 V4 最小 claim parser/router 对 4 条关键 LongMemEval 样本做真实数据链路检查，固定 `chunk=2048`：

```text
gpt4_d84a3211
67e0d0f2
dad224aa
gpt4_2ba83207
```

代码入口：`scripts/run_v4_claim_smoke.py`。

## 2. 离线数据结果

离线模式不调用模型，只验证源数据加载、chronological session 排序、2048 chunk 切分和 evidence 编号构建。

| question_id | chunk 数 | evidence message 数 | evidence 字符数 |
| --- | ---: | ---: | ---: |
| `gpt4_d84a3211` | 61 | 495 | 500,971 |
| `67e0d0f2` | 64 | 510 | 506,601 |
| `dad224aa` | 62 | 521 | 503,137 |
| `gpt4_2ba83207` | 63 | 500 | 501,772 |
| 合计 | **250** | **2,026** | **2,011,481** |

每个 chunk 都生成局部 evidence map；首尾 chunk 均保持完整 message/unit，没有发生程序级错误。离线结果文件：

`results/memory_sidecar/sidecar-v4-claim-smoke-20260828-offline.json`

本次离线 smoke 同时写入 V4 专用 SQLite：

`results/memory_sidecar/sidecar-v4-claim-smoke-20260828/trajectory.sqlite3`

数据库统计为：4 samples、250 batches、0 claims、0 edges。离线模式不伪造 Manager 输出，因此 claims/edges 为 0 是预期结果；在线运行会在同一批次表中写入原始 response、规范化 claim 和 edge 状态。

注意：4 条样本合计是 2,026 条 message，不是 2,028；manifest 中的 message_count 与脚本逐 session 重建结果一致，以输出文件为准。

## 3. 在线模型调用

使用 `.env` 中配置的 Qwen 服务和 V4 最小 claim prompt 启动在线 smoke。脚本完成本地 tokenizer 初始化后，在第一条样本第一个 Manager chat completion 请求处持续无响应，等待后手动中止；没有生成可用于准确率或 parser 覆盖率的模型结果，也没有写入在线结果文件。

这次失败属于服务连接/响应问题，不属于 V4 parser/router 失败。脚本已增加：

- `--timeout-seconds`，默认 60 秒；
- `--offline`，用于不调用模型的数据链路测试；
- 输出每个 chunk 的 evidence 数、字符数和 Manager token 统计字段。

在线重试命令：

```bash
set -a; source .env; set +a
.venv/bin/python scripts/run_v4_claim_smoke.py \
  --chunk-budget 2048 \
  --timeout-seconds 60 \
  --question-id gpt4_d84a3211 67e0d0f2 dad224aa gpt4_2ba83207
```

## 4. 当前判断

1. `chunk=2048` 下真实数据切分和 evidence provenance 链路可用。
2. V4 最小 claim parser/router 的离线单元测试通过，V3 回归测试也通过。
3. 尚未获得真实 Manager 输出，因此不能判断模型在 4 条样本上的抽取覆盖、unknown relation 比例或 graph edge 正确率。
4. 服务恢复后应从同一命令重跑在线 smoke；只有 4 条样本的 normalized/raw/quarantine 分布和关键结构断言可回放，才进入 24 条 pilot。
