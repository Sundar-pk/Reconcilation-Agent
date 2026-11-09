"""Tests the LLM-assisted paths with scripted fake models (no network), plus the CLI and the approval gate.

    python test_flow.py
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import config
import db
import engine
import explain
import planner
from gen_test_data import generate
from ingest import ingest

FAILS = []


def ok(cond, msg):
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def main():
    base = Path("test_flow_run")
    shutil.rmtree(base, ignore_errors=True)
    config.WORK_DIR = base / "work"
    data = base / "data"
    exp = generate(3000, 20, data, xlsx=False, log=lambda *_: None)
    con = db.connect(base / "t.duckdb")
    linfo = ingest(con, data / "left.csv", None, "src_left", progress=lambda *_: None)
    rinfo = ingest(con, data / "right.csv", None, "src_right", progress=lambda *_: None)

    from langchain_core.language_models.fake_chat_models import FakeListChatModel, GenericFakeChatModel
    from langchain_core.messages import AIMessage

    print("1) LLM-assisted planning (scripted model)")
    plan0, pl, pr = planner.propose_plan(con, linfo, rinfo, llm=None, progress=lambda *_: None)
    reply = json.dumps({
        "mapping": [{"left": "amount_2", "right": "Amt_2", "rule": "numeric", "tolerance": 0.01},
                    {"left": "status_3", "right": "Status 3", "rule": "text"},
                    {"left": "does_not_exist", "right": "Amt_2", "rule": "numeric"},     # hallucinated column
                    {"left": "status_6", "right": "date_7", "rule": "exact"}],            # tries to remap a confident pair
        "keys": [{"left": "customer_code", "right": "Customer Code", "normalize": []}],   # worse key: must be rejected
        "notes": ["amount_2 differs by rounding"]})
    llm = FakeListChatModel(responses=["I think... <think>x</think> ```json\n" + reply + "\n```"])
    plan, pl, pr = planner.propose_plan(con, linfo, rinfo, llm=llm, progress=lambda *_: None)
    by = {c["left"]: c for c in plan["columns"]}
    ok(by["amount_2"]["tolerance"] == 0.01 and by["amount_2"]["rule"] == "numeric", "LLM tolerance applied to amount_2")
    ok(by["status_3"]["rule"] == "text", "LLM rule applied to status_3")
    ok(by["status_6"]["right"] == "status_6", "confident mapping not overridden by the LLM")
    ok([k["left"] for k in plan["keys"]] == ["invoice_no"], "worse LLM key rejected, data-verified key kept")
    ok(any("unknown columns" in n for n in plan["notes"]), "hallucinated column reported, not used")
    ok(not plan["validation"]["errors"], "plan validates")

    bad = FakeListChatModel(responses=["not json at all"] * 5)
    plan_b, *_ = planner.propose_plan(con, linfo, rinfo, llm=bad, progress=lambda *_: None)
    ok(any("LLM review skipped" in n for n in plan_b["notes"]) and plan_b["keys"] == plan0["keys"],
       "unusable LLM output falls back to the heuristic plan")

    print("2) explain: privacy modes")
    s = engine.reconcile(con, plan, base / "out", progress=lambda *_: None)
    p_stats = explain.build_payload(s, "stats_only")
    p_mask = explain.build_payload(s, "masked_samples")
    ok(all("top_value_changes" not in c for c in p_stats["columns_with_mismatches"]), "stats_only payload contains no cell values")
    ok(any("top_value_changes" in c for c in p_mask["columns_with_mismatches"]), "masked_samples payload includes (masked) value changes")
    text = explain.explain(FakeListChatModel(responses=["## Summary\nok"]), s)
    ok(text.startswith("## Summary"), "explain returns the model text")
    ok("matched" in explain.fallback_text(s), "plain fallback summary works without an LLM")

    print("3) agent tools + approval gate")
    from agent import Session, build_agent

    sess = Session(con, linfo, rinfo, FakeListChatModel(responses=[reply, "## Summary\nfine"] * 3), base / "agent_out")

    class ToolFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    def call(name, i, **args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": str(i)}])

    a1 = build_agent(sess, ToolFake(messages=iter([call("propose_plan", 1), call("run_reconciliation", 2), AIMessage(content="done")])))
    out = a1.invoke({"messages": [("user", "reconcile")]})
    tool_msgs = [m.content for m in out["messages"] if m.type == "tool"]
    ok("NOT APPROVED" in tool_msgs[-1] and sess.summary is None, "run_reconciliation refused before /approve")
    sess.approved = True
    a2 = build_agent(sess, ToolFake(messages=iter([call("run_reconciliation", 3), call("mismatch_samples", 4, column="amount_5", n=3),
                                                   call("explain_results", 5), call("check_key", 6, left_columns=["invoice_no"],
                                                   right_columns=["Invoice Number"], normalize=[]), AIMessage(content="done")])))
    out = a2.invoke({"messages": [("user", "go")]})
    tm = [m.content for m in out["messages"] if m.type == "tool"]
    ok(sess.summary is not None and "Matched" in tm[0], "run_reconciliation works after approval")
    ok("withheld" in tm[1], "mismatch_samples withholds cell values in stats_only mode")
    ok(sess.narrative is not None and (sess.out_dir / "reconciliation_report.xlsx").exists(), "narrative + Excel report written")
    ok('"score"' in tm[3], "check_key returns key quality")

    print("4) CLI end to end (no LLM), unattended")
    r = subprocess.run([sys.executable, "main.py", "reconcile", "--left", str(data / "left.csv"), "--right", str(data / "right.csv"),
                        "--no-llm", "--yes", "--output", str(base / "cli_out"), "--rule", "amount_2=numeric:0.01"],
                       capture_output=True, text=True, env={**__import__("os").environ, "WORK_DIR": str(base / "cli_work")})
    ok(r.returncode == 0, f"CLI exit code 0 (got {r.returncode}) {r.stderr[-300:] if r.returncode else ''}")
    rep = base / "cli_out" / "reconciliation_report.xlsx"
    ok(rep.exists(), "Excel report created")
    if rep.exists():
        from openpyxl import load_workbook
        wb = load_workbook(rep, read_only=True)
        ok({"Summary", "Narrative", "Columns", "Value mismatches", "Only in left", "Only in right", "Plan"} <= set(wb.sheetnames),
           f"report sheets: {wb.sheetnames}")
        cols = {row[0]: row[3] for row in wb["Columns"].iter_rows(min_row=2, values_only=True)}
        ok(cols.get("amount_2 / Amt_2") == 0 and cols.get("status_3 / Status 3") == exp["mismatches"]["status_3"],
           "CLI --rule override took effect (amount_2 within tolerance, status_3 still exact)")
    ok((base / "cli_out" / "plan.json").exists() and (base / "cli_out" / "value_mismatches.csv").exists(), "plan.json and detail files written")
    r2 = subprocess.run([sys.executable, "main.py", "run", "--plan", str(base / "cli_out" / "plan.json"), "--no-llm",
                         "--output", str(base / "cli_out2")], capture_output=True, text=True,
                        env={**__import__("os").environ, "WORK_DIR": str(base / "cli_work")})
    ok(r2.returncode == 0 and (base / "cli_out2" / "summary.json").exists(), f"'run --plan' re-executes a saved plan {r2.stderr[-200:]}")

    con.close()
    shutil.rmtree(base, ignore_errors=True)
    print("\nRESULT:", "ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILURE(S)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
