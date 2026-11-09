"""LLM narrative over the engine's results.

The model never sees raw data. It gets counts, per-column mismatch statistics and pattern hints,
and writes the explanation. All numbers come from the engine, and the prompt forbids new ones.
LLM_DATA_MODE=stats_only (default) also withholds the "most common value changes" (cell values).
"""
from __future__ import annotations

import json

import config
from planner import _mask

SYSTEM = """You are a reconciliation analyst writing for finance / operations reviewers.
You receive JSON results computed by a deterministic engine comparing a LEFT and a RIGHT dataset.
Rules:
- Use ONLY numbers present in the JSON. Never calculate new totals or percentages that are not given;
  you may quote or round given figures. If something is not in the data, say it is unknown.
- Be concise and concrete. Name columns exactly as given.
Write Markdown with these sections:
## Summary
Two to four sentences: how well the datasets reconcile, and the main issue.
## Record-level findings
Bullets on matched / only-in-left / only-in-right / duplicate keys / blank keys, with counts.
## Value differences
For the columns with the most mismatches: what differs and the likely cause, using the hints
(for example "all differences are within 0.01, probably rounding" or "differs only by letter case").
## Suggested rule changes
Where hints show a looser rule would remove false differences (tolerance, text instead of exact,
date instead of datetime), say which column and why. Say "None" if nothing applies.
## Next steps
Two or three actions for a human reviewer."""


def build_payload(summary: dict, mode: str | None = None, max_columns: int = 30) -> dict:
    mode = mode or config.LLM_DATA_MODE
    cols = []
    for e in sorted(summary["columns"], key=lambda e: -e["mismatches"])[:max_columns]:
        if e["mismatches"] == 0:
            continue
        item = {k: e[k] for k in ("label", "rule", "tolerance", "mismatches", "mismatch_pct") if k in e}
        for k in ("sum_left_matched", "sum_right_matched", "sum_diff_matched", "max_abs_diff",
                  "total_left", "total_right", "total_diff", "hints", "top_numeric_diffs"):
            if k in e:
                item[k] = e[k]
        if mode == "masked_samples" and e.get("top_value_changes"):
            item["top_value_changes"] = [{"left": _mask(c["left"]), "right": _mask(c["right"]), "rows": c["rows"]}
                                         for c in e["top_value_changes"]]
        cols.append(item)
    return {
        "left": {"file": summary["left"]["path"], "sheet": summary["left"]["sheet"]},
        "right": {"file": summary["right"]["path"], "sheet": summary["right"]["sheet"]},
        "matched_on": [f"{k['left']} = {k['right']}" for k in summary["keys"]] or "row position",
        "counts": summary["counts"],
        "differing_columns_per_row": summary["differing_columns_per_row"],
        "columns_with_mismatches": cols,
        "columns_without_mismatches": sum(1 for e in summary["columns"] if e["mismatches"] == 0),
        "not_compared": summary["not_compared"],
    }


def explain(llm, summary: dict, mode: str | None = None) -> str:
    from llm import llm_text

    payload = build_payload(summary, mode)
    return llm_text(llm, SYSTEM, json.dumps(payload, default=str))


def fallback_text(summary: dict) -> str:
    """Plain, LLM-free summary used when no model is configured."""
    c = summary["counts"]
    lines = [
        "## Summary",
        f"{c['matched']:,} of {c['left_rows']:,} left rows ({c['match_rate_left_pct']:.2f}%) matched a right row; "
        f"{c['rows_with_differences']:,} matched rows differ in at least one column.",
        "",
        "## Record-level findings",
        f"- Only in left: {c['only_in_left']:,}",
        f"- Only in right: {c['only_in_right']:,}",
        f"- Blank-key rows: {c['left_blank_key_rows']:,} left / {c['right_blank_key_rows']:,} right",
        f"- Duplicate-key rows: {c['left_duplicate_rows']:,} left / {c['right_duplicate_rows']:,} right",
        "",
        "## Value differences",
    ]
    worst = sorted([e for e in summary["columns"] if e["mismatches"]], key=lambda e: -e["mismatches"])[:15]
    lines += [f"- {e['label']}: {e['mismatches']:,} mismatches ({e['mismatch_pct']}% of matched)" for e in worst] or ["- None"]
    return "\n".join(lines)
