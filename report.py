"""Excel report (xlsxwriter) + console summary.

The workbook holds the summary, per-column results and capped samples. At 1M rows the full detail
lives in the exported CSV / Parquet files (Excel itself cannot hold more than 1,048,576 rows).
"""
from __future__ import annotations

import json
from pathlib import Path

import config
from db import lit
from planner import plan_text


def console_summary(s: dict, top: int = 10) -> str:
    c = s["counts"]
    out = [
        f"Matched on : {', '.join(k['left'] + ' = ' + k['right'] for k in s['keys']) or 'row position'}",
        f"Rows       : left {c['left_rows']:,} | right {c['right_rows']:,}",
        f"Matched    : {c['matched']:,}  ({c['match_rate_left_pct']:.2f}% of left, {c['match_rate_right_pct']:.2f}% of right)",
        f"Only left  : {c['only_in_left']:,}   Only right: {c['only_in_right']:,}",
        f"Duplicates : left {c['left_duplicate_rows']:,} rows / right {c['right_duplicate_rows']:,} rows"
        f"   Blank keys: left {c['left_blank_key_rows']:,} / right {c['right_blank_key_rows']:,}",
        f"Identical  : {c['identical_rows']:,} matched rows   Differ: {c['rows_with_differences']:,} matched rows",
        f"Columns    : {c['compared_columns']} compared, "
        f"{sum(1 for e in s['columns'] if e['mismatches'])} with differences",
    ]
    worst = sorted([e for e in s["columns"] if e["mismatches"]], key=lambda e: -e["mismatches"])[:top]
    if worst:
        out.append("Top columns by mismatches:")
        out += [f"  {e['label'][:40]:<42}{e['mismatches']:>12,}  ({e['mismatch_pct']}% of matched)" for e in worst]
    out.append(f"Engine time: {s['timings_seconds']['total']}s")
    return "\n".join(out)


def _rel(path: str) -> str:
    p = Path(path)
    return f"read_parquet({lit(p.as_posix())})" if p.suffix == ".parquet" else \
        f"read_csv({lit(p.as_posix())}, header=true, all_varchar=true)"


def _sample(con, path: str, n: int):
    if not path or not Path(path).exists():
        return [], []
    cur = con.execute(f"SELECT * FROM {_rel(path)} LIMIT {int(n)}")
    return [d[0] for d in cur.description], cur.fetchall()


def write_report(con, summary: dict, plan: dict, out_path, narrative: str | None = None) -> Path:
    import xlsxwriter

    out_path = Path(out_path)
    wb = xlsxwriter.Workbook(str(out_path), {"strings_to_formulas": False, "strings_to_numbers": False})
    hdr = wb.add_format({"bold": True, "bg_color": "#1F3864", "font_color": "#FFFFFF", "border": 1})
    bold = wb.add_format({"bold": True})
    num = wb.add_format({"num_format": "#,##0"})
    dec = wb.add_format({"num_format": "#,##0.00"})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    title = wb.add_format({"bold": True, "font_size": 14})

    def put(ws, r, c, v, fmt=None):
        if v is None:
            return
        if isinstance(v, bool):
            ws.write_string(r, c, str(v))
        elif isinstance(v, int):
            ws.write_number(r, c, v, fmt or num)
        elif isinstance(v, float):
            ws.write_number(r, c, v, fmt or dec)
        else:
            ws.write_string(r, c, str(v))  # never interpreted as a formula

    def table(ws, headers, rows, start=0, widths=None):
        for j, h in enumerate(headers):
            ws.write_string(start, j, str(h), hdr)
        for i, row in enumerate(rows, start=start + 1):
            for j, v in enumerate(row):
                put(ws, i, j, v)
        for j, h in enumerate(headers):
            w = (widths or {}).get(j) or min(45, max(10, len(str(h)) + 2))
            ws.set_column(j, j, w)
        ws.freeze_panes(start + 1, 0)

    c = summary["counts"]

    # ---- Summary
    ws = wb.add_worksheet("Summary")
    ws.write_string(0, 0, "Reconciliation report", title)
    info = [("Generated", summary["generated_at"]),
            ("Left", f"{summary['left']['path']} [{summary['left']['sheet']}]"),
            ("Right", f"{summary['right']['path']} [{summary['right']['sheet']}]"),
            ("Matched on", ", ".join(f"{k['left']} = {k['right']}" for k in summary["keys"]) or "row position")]
    for i, (k, v) in enumerate(info, start=2):
        ws.write_string(i, 0, k, bold)
        ws.write_string(i, 1, str(v))
    metrics = [("Left rows", c["left_rows"]), ("Right rows", c["right_rows"]), ("Matched rows", c["matched"]),
               ("Match rate of left (%)", c["match_rate_left_pct"]), ("Match rate of right (%)", c["match_rate_right_pct"]),
               ("Only in left", c["only_in_left"]), ("Only in right", c["only_in_right"]),
               ("Blank-key rows, left", c["left_blank_key_rows"]), ("Blank-key rows, right", c["right_blank_key_rows"]),
               ("Duplicate-key rows, left", c["left_duplicate_rows"]), ("Duplicate-key rows, right", c["right_duplicate_rows"]),
               ("Matched rows identical", c["identical_rows"]), ("Matched rows with differences", c["rows_with_differences"]),
               ("Columns compared", c["compared_columns"]),
               ("Columns with differences", sum(1 for e in summary["columns"] if e["mismatches"]))]
    table(ws, ["Metric", "Value"], metrics, start=7, widths={0: 34, 1: 70})
    r = 7 + len(metrics) + 3
    ws.write_string(r, 0, "Full detail files", bold)
    for i, (k, v) in enumerate(summary["files"].items(), start=r + 1):
        ws.write_string(i, 0, k)
        ws.write_string(i, 1, str(v))
    nc = summary["not_compared"]
    r = r + len(summary["files"]) + 3
    for label, items in (("Only in left (not compared)", nc["unmapped_left"]), ("Only in right (not compared)", nc["unmapped_right"]),
                         ("Ignored by rule", nc["ignored"])):
        ws.write_string(r, 0, label, bold)
        ws.write_string(r, 1, ", ".join(items) if items else "-")
        r += 1

    # ---- Narrative
    if narrative:
        wn = wb.add_worksheet("Narrative")
        wn.set_column(0, 0, 130)
        for i, line in enumerate(narrative.splitlines()):
            wn.write_string(i, 0, line, wrap)

    # ---- Columns
    wc = wb.add_worksheet("Columns")
    rows = []
    for e in sorted(summary["columns"], key=lambda e: -e["mismatches"]):
        rows.append([e["label"], e["rule"], e["tolerance"] if e["rule"] == "numeric" else None, e["mismatches"],
                     e["mismatch_pct"], e.get("sum_left_matched"), e.get("sum_right_matched"), e.get("sum_diff_matched"),
                     e.get("max_abs_diff"), e.get("total_left"), e.get("total_right"), e.get("total_diff"),
                     json.dumps(e["hints"]) if e.get("hints") else None,
                     json.dumps(e["top_numeric_diffs"]) if e.get("top_numeric_diffs") else None])
    table(wc, ["Column", "Rule", "Tolerance", "Mismatches", "% of matched", "Sum left (matched)", "Sum right (matched)",
               "Diff (matched)", "Max abs diff", "Total left (all)", "Total right (all)", "Diff (all)", "Pattern hints",
               "Most common numeric differences"], rows, widths={0: 38, 12: 60, 13: 50})
    wc.autofilter(0, 0, len(rows), 13)

    # ---- Samples
    n = config.REPORT_SAMPLE_ROWS
    for sheet, key in (("Value mismatches", "value_mismatches"), ("Only in left", "only_in_left"),
                       ("Only in right", "only_in_right"), ("Duplicate keys left", "duplicate_keys_left"),
                       ("Duplicate keys right", "duplicate_keys_right")):
        if key not in summary["files"]:
            continue
        heads, data = _sample(con, summary["files"][key], n)
        if not heads:
            continue
        w = wb.add_worksheet(sheet)
        table(w, heads, data)
        total = {"value_mismatches": sum(e["mismatches"] for e in summary["columns"]),
                 "only_in_left": c["only_in_left"], "only_in_right": c["only_in_right"]}.get(key)
        if total is not None and total > len(data):
            w.write_string(len(data) + 2, 0, f"Showing first {len(data):,} of {total:,}. Full list: {summary['files'][key]}")

    # ---- Plan
    wp = wb.add_worksheet("Plan")
    wp.set_column(0, 0, 150)
    for i, line in enumerate(plan_text(plan, max_rows=100000).splitlines()):
        wp.write_string(i, 0, line)

    wb.close()
    return out_path
