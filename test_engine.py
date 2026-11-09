"""End-to-end check of ingest -> plan -> reconcile against data with KNOWN differences (no LLM needed).

    python test_engine.py                    # 20,000 rows x 100 cols, CSV + Excel
    python test_engine.py --rows 1000000     # full-scale run (CSV only unless --xlsx)
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import config
import db
import engine
import planner
from gen_test_data import generate
from ingest import ingest


QUICK = False


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {name}: {got:,}" + ("" if ok else f"  (expected {want:,})"))
    return ok


def verify(summary, exp, tag, text_rule=False):
    c, ok = summary["counts"], True
    for k in ("left_rows", "right_rows", "matched", "only_in_left", "only_in_right", "left_duplicate_rows",
              "rows_with_differences", "identical_rows"):
        ok &= check(f"{tag} {k}", c[k], exp[k])
    by = {e["left"]: e for e in summary["columns"]}
    for col, want in exp["mismatches"].items():
        got = by[col]["mismatches"]
        if text_rule and col == "status_3":
            want = 0
        ok &= check(f"{tag} mismatches[{col}]", got, want)
    total = sum(e["mismatches"] for e in summary["columns"])
    ok &= check(f"{tag} no unexpected mismatches in other columns", total,
                sum(exp["mismatches"].values()) - (exp["mismatches"]["status_3"] if text_rule else 0))
    return ok


def run_case(con, plan_src, exp, out_dir, tag, tolerance_check=True):
    linfo = ingest(con, plan_src["left"], plan_src.get("left_sheet"), "src_left")
    rinfo = ingest(con, plan_src["right"], plan_src.get("right_sheet"), "src_right")
    t0 = time.time()
    plan, pl, pr = planner.propose_plan(con, linfo, rinfo, llm=None)
    print(planner.plan_text(plan, max_rows=12))
    assert not plan["validation"]["errors"], plan["validation"]["errors"]
    k = plan["keys"]
    assert [x["left"] for x in k] == ["invoice_no"] and k[0]["right"] == "Invoice Number", f"unexpected key {k}"
    assert len(plan["columns"]) == exp["cols"] - 1, f"expected all {exp['cols'] - 1} non-key columns mapped"
    print(f"  planning done in {time.time() - t0:.1f}s")

    s = engine.reconcile(con, plan, out_dir / tag)
    ok = verify(s, exp, tag)

    # same data, looser rules: tolerance 0.01 on amount_2 and text rule on status_3 must remove those differences
    if not QUICK:
        planner.apply_changes(plan, {"set_rule": [{"column": "amount_2", "rule": "numeric", "tolerance": 0.01},
                                                  {"column": "status_3", "rule": "text"}]},
                              {c["name"]: c for c in pl["columns"]}, {c["name"]: c for c in pr["columns"]})
        s2 = engine.reconcile(con, plan, out_dir / (tag + "_loose"))
        by = {e["left"]: e["mismatches"] for e in s2["columns"]}
        ok &= check(f"{tag} amount_2 with tolerance 0.01", by["amount_2"], 0)
        ok &= check(f"{tag} status_3 with text rule", by["status_3"], 0)
        ok &= check(f"{tag} amount_5 unaffected", by["amount_5"], exp["mismatches"]["amount_5"])

    hints = {e["left"]: e.get("hints") for e in s["columns"]}
    print("  hint amount_2:", hints["amount_2"], "| hint status_3:", hints["status_3"])
    assert hints["amount_2"]["within_0.01"] == exp["mismatches"]["amount_2"]
    assert hints["status_3"]["equal_ignoring_case_and_spaces"] == exp["mismatches"]["status_3"]
    print(f"  timings: {s['timings_seconds']}")
    return ok, s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--xlsx", action="store_true", help="force Excel test (default: on for <= 200k rows)")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--quick", action="store_true", help="skip the second (looser-rules) run; for big datasets")
    a = ap.parse_args()
    global QUICK
    QUICK = a.quick

    base = Path("test_run")
    if base.exists():
        shutil.rmtree(base)
    config.WORK_DIR = base / "work"
    data = base / "data"
    print(f"Generating {a.rows:,} rows x {a.cols} columns ...")
    do_xlsx = a.xlsx or a.rows <= 200_000
    exp = generate(a.rows, a.cols, data, xlsx=do_xlsx)
    con = db.connect(base / "test.duckdb")

    all_ok = True
    print("\n=== CSV ===")
    ok, _ = run_case(con, {"left": data / "left.csv", "right": data / "right.csv"}, exp, base / "out", "csv")
    all_ok &= ok
    if do_xlsx:
        print("\n=== Excel (two sheets of one workbook) ===")
        ok, _ = run_case(con, {"left": data / "pair.xlsx", "left_sheet": "Left", "right": data / "pair.xlsx",
                               "right_sheet": "Right"}, exp, base / "out", "xlsx")
        all_ok &= ok
    print("\nRESULT:", "ALL CHECKS PASSED" if all_ok else "FAILURES")
    if not a.keep:
        con.close()
        shutil.rmtree(base, ignore_errors=True)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
