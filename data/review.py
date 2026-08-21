"""预览官方 LongMemEval-S 数据：schema、问题类型、历史规模与样本。

用法（在项目根目录）：
    uv run python data/review.py
    uv run python data/review.py --num 3 --max-chars 500
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


DATA_PATH = Path(__file__).resolve().parent / "official_longmemeval" / "longmemeval_s_cleaned.json"


def estimate_tokens(characters: int) -> int:
    """粗略估算英文 token 数，不替代实际 tokenizer 统计。"""
    return max(1, characters // 4)


def median(values: list[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2]


def preview(num: int, max_chars: int) -> None:
    with DATA_PATH.open(encoding="utf-8") as file:
        rows = json.load(file)

    n = len(rows)
    print("=== 数据集基本信息 ===")
    print(f"文件: {DATA_PATH}")
    print(f"样本数: {n}")
    print(f"字段: {list(rows[0])}")
    print()

    types = Counter(row["question_type"] for row in rows)
    print(f"=== 问题类型分布 ({len(types)} 类) ===")
    for question_type, count in types.most_common():
        print(f"  {question_type:<28} {count:>4}  ({count / n:6.1%})")
    print()

    session_counts = []
    message_counts = []
    character_counts = []
    for row in rows:
        sessions = row["haystack_sessions"]
        messages = [message for session in sessions for message in session]
        session_counts.append(len(sessions))
        message_counts.append(len(messages))
        character_counts.append(sum(len(message.get("content") or "") for message in messages))

    print("=== 历史规模（每个 question sample 的 haystack）===")
    print(f"  sessions 数    : min={min(session_counts)}  median={median(session_counts)}  max={max(session_counts)}")
    print(f"  消息条数       : min={min(message_counts)}  median={median(message_counts)}  max={max(message_counts)}")
    print(f"  历史字符数     : min={min(character_counts)}  median={median(character_counts)}  max={max(character_counts)}")
    print(f"  历史估算 token : min={estimate_tokens(min(character_counts))}  median~{estimate_tokens(median(character_counts))}  max={estimate_tokens(max(character_counts))}")
    print()

    print(f"=== 样本展示 (前 {num} 条, 每字段截断 {max_chars} 字符) ===")
    for index, row in enumerate(rows[:num], start=1):
        print(f"\n----- 样本 #{index} -----")
        for field in ("question_id", "question_type", "question", "answer"):
            value = str(row[field])
            if len(value) > max_chars:
                value = value[:max_chars] + f"...<+{len(str(row[field])) - max_chars} chars>"
            print(f"  {field:<20}: {value}")
        print(f"  {'session_ids':<20}: {row['haystack_session_ids']}")
        print(f"  {'answer_session_ids':<20}: {row['answer_session_ids']}")
        for session_index, session in enumerate(row["haystack_sessions"][:3]):
            print(f"    -- session[{session_index}] ({len(session)} msgs) --")
            for message in session[:2]:
                content = message["content"]
                if len(content) > max_chars:
                    content = content[:max_chars] + f"...<+{len(message['content']) - max_chars} chars>"
                evidence = " [HAS_ANSWER]" if message.get("has_answer") else ""
                print(f"      role={message['role']}{evidence}: {content}")


def main() -> None:
    parser = argparse.ArgumentParser(description="预览官方 LongMemEval-S 数据")
    parser.add_argument("--num", type=int, default=2, help="展示的样本数量")
    parser.add_argument("--max-chars", type=int, default=500, help="字段展示的最大字符数")
    args = parser.parse_args()
    preview(args.num, args.max_chars)


if __name__ == "__main__":
    main()
