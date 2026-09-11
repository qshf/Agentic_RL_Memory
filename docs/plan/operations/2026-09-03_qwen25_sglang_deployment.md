# Qwen2.5-7B 本地 SGLang 评测部署

## 目标

在 9934 服务器的 48G 显存 GPU 上新增一个独立的 Qwen2.5-7B-Instruct 服务，供 V3/V4 记忆管理实验使用。部署不执行 `docker compose down`，不停止或重建其他容器。

模型文件固定放在 `/home/ubuntu/DiskData/models/Qwen2.5-7B-Instruct`，SGLang 缓存放在 `/home/ubuntu/DiskData/sglang-cache`。

## 部署步骤

```bash
cd /path/to/Agentic_RL_Memory
chmod +x scripts/deploy_sglang_qwen25.sh
scripts/deploy_sglang_qwen25.sh download
scripts/deploy_sglang_qwen25.sh start
scripts/deploy_sglang_qwen25.sh status
```

默认服务地址为 `http://127.0.0.1:30001/v1`，容器名为 `sglang-qwen25-7b`，GPU 为 `0`。如服务器上的 GPU 编号或端口不同，只覆盖环境变量：

```bash
GPU_ID=1 SGLANG_PORT=30002 scripts/deploy_sglang_qwen25.sh start
```

ModelScope CLI 支持使用 `--model` 和 `--local_dir` 将完整模型快照下载到指定目录。[ModelScope CLI](https://github.com/modelscope/modelscope/blob/master/docs/source/command.md)

## SGLang 参数

容器使用官方 `lmsysorg/sglang:latest-runtime` 镜像，单卡 `--tp 1`，服务端模型名固定为 `Qwen2.5-7B-Instruct`。由于 9934 上已有服务和训练进程占用显存，默认使用 `--mem-fraction-static 0.55`；只有确认其他任务空闲且 32K 上下文稳定后才逐步提高。

SGLang 官方 Docker 示例使用 GPU 映射、共享 IPC、模型卷挂载和 `launch_server`；ModelScope 模型也可以通过本地模型路径启动。[SGLang Docker 安装](https://github.com/sgl-project/sglang/blob/main/docs/docs/get-started/install.mdx) [SGLang ModelScope](https://github.com/sgl-project/sglang/blob/main/docs_new/docs/supported-models/modelscope.mdx)

## 评测端配置

在 9934 服务器上运行仓库实验时：

```bash
export QWEN38_BASE_URL=http://127.0.0.1:30001/v1
export QWEN38_MODEL=Qwen2.5-7B-Instruct
export QWEN38_API_KEY=local
```

`QWEN38_*` 是项目历史环境变量名，实际模型由 `QWEN38_MODEL` 决定。当前 runner 会向 `/v1/models`、`/tokenize` 和 `/v1/chat/completions` 做预检；SGLang 提供 OpenAI 兼容接口。

正式跑 V3/V4 前，需要将本地 tokenizer 切换为 Qwen2.5-7B-Instruct tokenizer，并把 tokenizer 路径写入 run config，否则 token 预算与旧 Qwen3.8 tokenizer 不完全可比。

## 服务管理

```bash
scripts/deploy_sglang_qwen25.sh status
scripts/deploy_sglang_qwen25.sh logs
scripts/deploy_sglang_qwen25.sh restart
scripts/deploy_sglang_qwen25.sh stop
```

`stop` 和 `restart` 只操作 `sglang-qwen25-7b`，不会影响其他容器。

## 显存与上下文策略

48G 显存足以运行 7B BF16 模型，但长上下文的 KV cache 仍可能成为瓶颈。先以 32K 上下文跑 4 条兼容性样本，再测试 64K；128K 作为单独实验，不与基础结果混合。Qwen2.5 模型卡说明更长上下文需要额外 YaRN 配置，不能仅凭显存容量直接推断可稳定运行。[Qwen2.5-7B-Instruct 模型卡](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct)
