"""Build a self-contained browser page for manual LongMemEval review."""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_JUDGMENTS = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/deepseek_judgments.jsonl")
DEFAULT_SOURCE = Path("data/official_longmemeval/longmemeval_s_cleaned.json")
DEFAULT_DB = Path("results/rolling_summary/rolling-summary-eval120-v1-atomic-c2/trajectory.sqlite3")
DEFAULT_OUTPUT = Path("docs/plan/rolling_summary_v1_manual_review.html")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judgments", type=Path, default=DEFAULT_JUDGMENTS)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def session_index(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for date, session_id, messages in zip(
        row.get("haystack_dates", []),
        row.get("haystack_session_ids", []),
        row.get("haystack_sessions", []),
    ):
        result[session_id] = {
            "session_id": session_id,
            "date": date,
            "messages": [{"role": message.get("role", ""), "content": message.get("content", "")} for message in messages],
        }
    return result


def trajectory_fields(connection: sqlite3.Connection, sample_id: int) -> dict[str, Any]:
    sample = connection.execute(
        "SELECT dataset_index, compression_count, full_history_tokens, summary_tokens, raw_tail_tokens "
        ", answer_input_tokens, answer_output_tokens FROM samples WHERE id = ?",
        (sample_id,),
    ).fetchone()
    compression_rows = connection.execute(
        "SELECT step_ordinal, detail FROM states WHERE sample_id = ? AND event = 'rolling_compression' "
        "ORDER BY step_ordinal",
        (sample_id,),
    ).fetchall()
    final_summary = connection.execute(
        "SELECT summary_text FROM states WHERE sample_id = ? AND event = 'final' LIMIT 1",
        (sample_id,),
    ).fetchone()
    last_compression = connection.execute(
        "SELECT COALESCE(MAX(step_ordinal), 0) FROM states WHERE sample_id = ? AND event = 'rolling_compression'",
        (sample_id,),
    ).fetchone()[0]
    tail_rows = connection.execute(
        "SELECT raw_text FROM states WHERE sample_id = ? AND event = 'ingest' AND step_ordinal > ? "
        "ORDER BY step_ordinal",
        (sample_id, last_compression),
    ).fetchall()
    compressions = []
    for step, detail in compression_rows:
        try:
            parsed = json.loads(detail or "{}")
        except json.JSONDecodeError:
            parsed = {"raw_detail": detail}
        compressions.append({"step": step, **parsed})
    return {
        "dataset_index": sample[0] if sample else None,
        "compression_count": sample[1] if sample else 0,
        "full_history_tokens": sample[2] if sample else 0,
        "summary_tokens": sample[3] if sample else 0,
        "raw_tail_tokens": sample[4] if sample else 0,
        "answer_input_tokens": sample[5] if sample else 0,
        "answer_output_tokens": sample[6] if sample else 0,
        "compressions": compressions,
        "final_summary": (final_summary[0] if final_summary else None),
        "final_raw_tail": "\n".join(row[0] for row in tail_rows if row[0]),
    }


def build_payload(judgments_path: Path, source_path: Path, db_path: Path) -> dict[str, Any]:
    judgments = load_jsonl(judgments_path)
    source = {row["question_id"]: row for row in json.loads(source_path.read_text(encoding="utf-8"))}
    connection = sqlite3.connect(db_path)
    rows = []
    try:
        for judgment in judgments:
            original = source[judgment["question_id"]]
            sample_row = connection.execute(
                "SELECT id FROM samples WHERE question_id = ? ORDER BY attempt DESC LIMIT 1",
                (judgment["question_id"],),
            ).fetchone()
            sessions = session_index(original)
            answer_sessions = [sessions[sid] for sid in original.get("answer_session_ids", []) if sid in sessions]
            trajectory = trajectory_fields(connection, sample_row[0]) if sample_row else {}
            rows.append(
                {
                    "question_id": judgment["question_id"],
                    "question_type": judgment.get("question_type", original.get("question_type", "")),
                    "question_date": original.get("question_date"),
                    "question": judgment.get("question", original.get("question", "")),
                    "reference_answer": judgment.get("reference_answer", original.get("answer")),
                    "hypothesis": judgment.get("hypothesis", ""),
                    "judge": judgment.get("judgment"),
                    "judge_response": judgment.get("judge_response"),
                    "judge_version": judgment.get("judge_version"),
                    "answer_session_ids": original.get("answer_session_ids", []),
                    "answer_sessions": answer_sessions,
                    "trajectory": trajectory,
                }
            )
    finally:
        connection.close()
    return {
        "run_id": judgments_path.parent.name,
        "generated_from": str(judgments_path),
        "rows": rows,
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rolling Summary V1 人工审核</title>
<style>
:root { color-scheme: light; --ink:#17212b; --muted:#66727e; --line:#d9e0e6; --panel:#fff; --bg:#f4f6f8; --blue:#1769aa; --red:#b42318; --green:#087443; --amber:#9a6700; }
* { box-sizing: border-box; }
body { margin:0; color:var(--ink); background:var(--bg); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
header { background:#16232e; color:#fff; padding:22px clamp(16px,4vw,48px); }
h1 { margin:0 0 5px; font-size:22px; font-weight:650; }
header p { margin:0; color:#cbd5dd; }
main { max-width:1440px; margin:0 auto; padding:18px clamp(12px,3vw,36px) 48px; }
.toolbar { display:grid; grid-template-columns:minmax(220px,1fr) 180px 180px auto auto; gap:10px; align-items:center; margin-bottom:14px; }
input, select, textarea, button { font:inherit; }
input[type="search"], select, textarea { border:1px solid var(--line); border-radius:5px; background:#fff; color:var(--ink); padding:8px 10px; }
button { border:1px solid #aeb9c3; border-radius:5px; background:#fff; color:var(--ink); padding:8px 11px; cursor:pointer; }
button:hover { border-color:var(--blue); color:var(--blue); }
.check { display:inline-flex; gap:6px; align-items:center; white-space:nowrap; }
.stats { display:flex; flex-wrap:wrap; gap:8px 18px; color:var(--muted); border-bottom:1px solid var(--line); padding:0 0 14px; margin-bottom:14px; }
.stats strong { color:var(--ink); }
.case { background:var(--panel); border:1px solid var(--line); border-left:4px solid #9aa7b2; margin:10px 0; padding:14px 16px; }
.case.judge-no { border-left-color:var(--red); }
.case.judge-yes { border-left-color:var(--green); }
.case.reviewed { border-left-color:var(--blue); }
.case-head { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin-bottom:10px; }
.qid { font:600 14px ui-monospace,SFMono-Regular,Menlo,monospace; }
.badge { border-radius:3px; padding:2px 7px; font-size:12px; background:#edf1f4; color:#44515d; }
.badge.no { background:#fde8e7; color:var(--red); }.badge.yes { background:#e6f4ed; color:var(--green); }
.meta { color:var(--muted); font-size:12px; }
.grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; }
.field { min-width:0; }.field h3 { margin:0 0 4px; color:var(--muted); font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.03em; }
.field p, .field pre { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; }
.answer { border:1px solid #dfe5ea; border-radius:4px; padding:9px; min-height:48px; background:#fafbfc; }
.answer.hyp { background:#fffdf5; }.answer.gold { background:#f5fbf8; }
details { margin-top:11px; border-top:1px solid var(--line); padding-top:9px; }
summary { cursor:pointer; color:var(--blue); font-weight:600; }
.context { margin-top:9px; border:1px solid var(--line); background:#fbfcfd; padding:10px; max-height:470px; overflow:auto; }
.message { padding:7px 0; border-bottom:1px solid #e7ebee; white-space:pre-wrap; overflow-wrap:anywhere; }.message:last-child{border-bottom:0}.role{font-weight:650;color:#4d5d69;margin-right:6px}
.review { display:grid; grid-template-columns:180px minmax(220px,1fr); gap:10px; align-items:start; margin-top:12px; padding-top:12px; border-top:1px solid var(--line); }
.review label { color:var(--muted); font-size:12px; font-weight:600; }.review-controls { display:grid; gap:8px; }.tags { display:flex; flex-wrap:wrap; gap:8px 12px; }.tags label { color:var(--ink); font-weight:400; }
textarea { min-height:66px; resize:vertical; width:100%; }
.empty { padding:30px 10px; color:var(--muted); text-align:center; }
@media (max-width:900px) { .toolbar{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.review{grid-template-columns:1fr} }
</style>
</head>
<body>
<header><h1>Rolling Summary V1 人工审核</h1><p id="subtitle"></p></header>
<main>
  <section class="toolbar" aria-label="筛选">
    <input id="search" type="search" placeholder="搜索 question_id、问题、答案、备注…">
    <select id="type"><option value="">全部题型</option></select>
    <select id="judge"><option value="">全部 judge 结果</option><option value="no">judge: no</option><option value="yes">judge: yes</option></select>
    <label class="check"><input id="onlyNo" type="checkbox" checked> 只看 judge: no</label>
    <button id="export">导出审核结果</button>
    <button id="clear">清除本机草稿</button>
  </section>
  <div id="stats" class="stats"></div>
  <section id="cases"></section>
</main>
<script>
const DATA = __DATA__;
const STORAGE_KEY = `rolling-summary-v1-review:${DATA.run_id}`;
const TAGS = [
  ["summary_omission", "摘要遗漏"], ["answer_reasoning", "答案推理"],
  ["unsupported_answer", "无依据答案"], ["data_or_gold", "数据或 gold"],
  ["judge_issue", "judge 疑点"], ["other", "其他"]
];
let draft = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const text = (value) => value === null || value === undefined ? "" : String(value);
function reviewOf(id) { return draft[id] || { verdict:"", tags:[], notes:"" }; }
function saveDraft() { localStorage.setItem(STORAGE_KEY, JSON.stringify(draft)); }
function sessionHtml(sessions) {
  if (!sessions.length) return '<div class="meta">未找到 answer_session_ids 对应的原始 session。</div>';
  return sessions.map((s) => `<h4>${esc(s.session_id)} <span class="meta">${esc(s.date)}</span></h4>` + s.messages.map((m) => `<div class="message"><span class="role">${esc(m.role)}:</span>${esc(m.content)}</div>`).join("")).join("");
}
function tagsHtml(item) {
  return TAGS.map(([key,label]) => `<label><input class="review-field tag" data-key="${key}" type="checkbox" ${item.tags.includes(key)?"checked":""}> ${label}</label>`).join("");
}
function card(row) {
  const review = reviewOf(row.question_id);
  const t = row.trajectory || {};
  const cls = row.judge === "no" ? "judge-no" : "judge-yes";
  const judgeBadge = `<span class="badge ${row.judge === "no" ? "no" : "yes"}">judge: ${esc(row.judge || "error")}</span>`;
  const compression = (t.compressions || []).map((c) => `step ${c.step}`).join(", ") || "无";
  return `<article class="case ${cls} ${review.verdict ? "reviewed" : ""}" data-id="${esc(row.question_id)}">
    <div class="case-head"><span class="qid">${esc(row.question_id)}</span>${judgeBadge}<span class="badge">${esc(row.question_type)}</span><span class="meta">dataset index ${esc(t.dataset_index)} · compression ${esc(t.compression_count)} · ${esc(compression)} · full history ${esc(t.full_history_tokens)} tokens · answer input ${esc(t.answer_input_tokens)} tokens</span></div>
    <div class="grid">
      <div class="field"><h3>Question</h3><div class="answer">${esc(row.question)}</div></div>
      <div class="field"><h3>Reference / Gold</h3><div class="answer gold">${esc(row.reference_answer)}</div></div>
      <div class="field"><h3>Model hypothesis</h3><div class="answer hyp">${esc(row.hypothesis)}</div></div>
    </div>
    <details><summary>答案所在原始 session (${row.answer_sessions.length})</summary><div class="context">${sessionHtml(row.answer_sessions)}</div></details>
    <details><summary>最终 summary (${esc(t.summary_tokens)} tokens · ${text(t.final_summary).length} chars)</summary><pre class="context">${esc(t.final_summary || "")}</pre></details>
    <details><summary>最终 raw tail (${esc(t.raw_tail_tokens)} tokens · ${text(t.final_raw_tail).length} chars)</summary><pre class="context">${esc(t.final_raw_tail || "")}</pre></details>
    <details><summary>压缩 detail / judge response</summary><pre class="context">${esc(JSON.stringify({compressions:t.compressions || [], judge_response:row.judge_response}, null, 2))}</pre></details>
    <div class="review"><label>人工结论</label><div class="review-controls">
      <select class="review-field verdict"><option value="">未审核</option><option value="correct" ${review.verdict==="correct"?"selected":""}>正确</option><option value="incorrect" ${review.verdict==="incorrect"?"selected":""}>错误</option><option value="ambiguous" ${review.verdict==="ambiguous"?"selected":""}>有歧义</option><option value="data_issue" ${review.verdict==="data_issue"?"selected":""}>数据问题</option><option value="judge_issue" ${review.verdict==="judge_issue"?"selected":""}>judge 问题</option></select>
      <div class="tags">${tagsHtml(review)}</div>
      <textarea class="review-field notes" placeholder="人工审核备注…">${esc(review.notes)}</textarea>
    </div></div>
  </article>`;
}
function filteredRows() {
  const q = $("search").value.trim().toLowerCase(), type = $("type").value, judge = $("judge").value, onlyNo = $("onlyNo").checked;
  return DATA.rows.filter((row) => {
    const review = reviewOf(row.question_id);
    const haystack = [row.question_id,row.question_type,row.question,row.reference_answer,row.hypothesis,review.notes].map(text).join(" ").toLowerCase();
    return (!q || haystack.includes(q)) && (!type || row.question_type === type) && (!judge || row.judge === judge) && (!onlyNo || row.judge === "no");
  });
}
function render() {
  const rows = filteredRows();
  const judged = DATA.rows.filter((r) => r.judge === "yes").length, no = DATA.rows.filter((r) => r.judge === "no").length;
  const reviewed = DATA.rows.filter((r) => reviewOf(r.question_id).verdict).length;
  $("subtitle").textContent = `${DATA.run_id} · ${DATA.rows.length} samples · ${reviewed} 已人工审核`;
  $("stats").innerHTML = `<span>当前 <strong>${rows.length}</strong> 条</span><span>judge yes <strong>${judged}</strong></span><span>judge no <strong>${no}</strong></span><span>人工已审核 <strong>${reviewed}</strong></span>`;
  $("cases").innerHTML = rows.length ? rows.map(card).join("") : '<div class="empty">没有匹配的样本</div>';
}
function updateReview(target) {
  const article = target.closest(".case"), id = article.dataset.id, current = reviewOf(id);
  if (target.classList.contains("verdict")) current.verdict = target.value;
  if (target.classList.contains("notes")) current.notes = target.value;
  if (target.classList.contains("tag")) current.tags = TAGS.map(([key]) => article.querySelector(`[data-key="${key}"]`).checked ? key : null).filter(Boolean);
  draft[id] = current; saveDraft(); render();
}
$("type").innerHTML += [...new Set(DATA.rows.map((r) => r.question_type))].sort().map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
["search","type","judge","onlyNo"].forEach((id) => $(id).addEventListener(id === "search" ? "input" : "change", render));
$("cases").addEventListener("change", (e) => { if (e.target.classList.contains("review-field")) updateReview(e.target); });
$("export").addEventListener("click", () => { const payload = {run_id:DATA.run_id, exported_at:new Date().toISOString(), reviews:draft}; const blob = new Blob([JSON.stringify(payload,null,2)], {type:"application/json"}); const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download=`${DATA.run_id}-manual-review.json`; a.click(); URL.revokeObjectURL(a.href); });
$("clear").addEventListener("click", () => { if (confirm("清除本机保存的人工审核草稿？")) { draft={}; saveDraft(); render(); } });
render();
</script>
</body>
</html>
'''


def write_html(payload: dict[str, Any], output: Path) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(HTML_TEMPLATE.replace("__DATA__", encoded), encoding="utf-8")


def main() -> None:
    args = parse_args()
    write_html(build_payload(args.judgments, args.source, args.db), args.output)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
