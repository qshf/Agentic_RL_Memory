"""预览 LongMemEval 数据集：schema、问题类型分布、历史规模与样本展示。

用法（在项目根目录，uv 环境）：
    uv run python data/review.py
    uv run python data/review.py --num 3 --max-chars 500
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from datasets import load_from_disk

DATA_PATH = Path(__file__).resolve().parent / "longmemeval_s"


def estimate_tokens(text: str | int) -> int:
    """粗略 token 估算：约 4 字符 ≈ 1 token（英文场景）。"""
    return max(1, len(text) // 4) if isinstance(text, str) else max(1, text // 4)


def preview(num: int, max_chars: int) -> None:
    ds = load_from_disk(str(DATA_PATH))
    n = ds.num_rows
    print(f"=== 数据集基本信息 ===")
    print(f"样本数: {n}")
    print(f"字段: {list(ds.features.keys())}")
    print()

    # 问题类型分布
    types = Counter(ds["question_type"])
    print(f"=== 问题类型分布 ({len(types)} 类) ===")
    for t, c in types.most_common():
        print(f"  {t:<28} {c:>4}  ({c / n:6.1%})")
    print()

    # 历史规模统计：每个 question sample 包含多个 session；每个 session 是一条完整对话轨迹。
    # 结构：sample -> sessions -> messages dict{role, content, has_answer}
    n_sessions = []
    n_msgs = []
    total_chars = []
    for dataset_index, row in enumerate(ds):
        sessions = row["haystack_sessions"]
        n_sessions.append(len(sessions))
        msgs = [message for session in sessions for message in session]
        n_msgs.append(len(msgs))
        total_chars.append(sum(len(message.get("content") or "") for message in msgs))
        
    s_sorted = sorted(total_chars)
    print("=== 历史规模（每个 question sample 的 haystack）===")
    print(f"  sessions 数    : min={min(n_sessions)}  median={sorted(n_sessions)[n // 2]:.0f}  max={max(n_sessions)}")
    print(f"  消息条数       : min={min(n_msgs)}  median={sorted(n_msgs)[n // 2]:.0f}  max={max(n_msgs)}")
    print(f"  历史字符数     : min={min(s_sorted)}  median={s_sorted[n // 2]:.0f}  max={max(s_sorted)}")
    print(f"  历史估算 token : min={estimate_tokens(min(s_sorted))}  median≈{estimate_tokens(s_sorted[n // 2])}  max={estimate_tokens(max(s_sorted))}")
    print()

    # 样本展示
    print(f"=== 样本展示 (前 {num} 条, 每字段截断 {max_chars} 字符) ===")
    for i in range(min(num, n)):
        row = ds[i]
        print(f"\n----- 样本 #{i + 1} -----")
        for k in ["question_id", "question_type", "question", "answer"]:
            v = row[k]
            s = str(v)
            if len(s) > max_chars:
                s = s[:max_chars] + f"...<+{len(str(v)) - max_chars} chars>"
            print(f"  {k:<20}: {s}")
        print(f"  {'session_ids':<20}: {row['haystack_session_ids']}")
        print(f"  {'answer_session_ids':<20}: {row['answer_session_ids']}")
        print(f"  sessions 数: {len(row['haystack_sessions'])}")
        for j, session in enumerate(row["haystack_sessions"][:3]):
            print(f"    -- session[{j}] ({len(session)} msgs) --")
            for m in session[:2]:
                content = m["content"]
                if len(content) > max_chars:
                    content = content[:max_chars] + f"...<+{len(m['content']) - max_chars} chars>"
                flag = " [HAS_ANSWER]" if m.get("has_answer") else ""
                print(f"      role={m['role']}{flag}: {content}")


def main() -> None:
    parser = argparse.ArgumentParser(description="预览 LongMemEval 数据集")
    parser.add_argument("--num", type=int, default=2, help="展示的样本数量")
    parser.add_argument("--max-chars", type=int, default=500, help="字段展示的最大字符数")
    args = parser.parse_args()
    preview(args.num, args.max_chars)


if __name__ == "__main__":
    main()
