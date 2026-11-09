"""Reconciliation Agent CLI.

    python main.py sheets  --left book.xlsx
    python main.py reconcile --left a.xlsx --left-sheet Jan --right a.xlsx --right-sheet Feb
    python main.py reconcile --left a.csv --right b.xlsx --right-sheet Data --key "InvoiceNo=Invoice Number:upper"
    python main.py plan      --left a.xlsx --right b.xlsx          # build + save a plan only
    python main.py run       --plan output/run_x/plan.json         # execute an edited / approved plan
    python main.py chat      --left a.xlsx --right b.xlsx          # interactive LangChain agent
    python main.py clean [--all]
"""
import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

# 1) Apply org guardrails (monkey patches) BEFORE any langchain / openai import.
import guardrails_hook

guardrails_hook.apply()

import config  # noqa: E402
import db  # noqa: E402
import engine  # noqa: E402
import explain as explain_mod  # noqa: E402
import planner  # noqa: E402
from ingest import ingest, list_sheets  # noqa: E402
from report import console_summary, write_report  # noqa: E402


def say(msg=""):
    print(msg, flush=True)


# ----------------------------------------------------------------------------- inputs
def resolve_inputs(args):
    left = Path(args.left)
    right = Path(args.right) if args.right else left
    ls, rs = args.left_sheet, args.right_sheet
    if right.resolve() == left.resolve() and left.suffix.lower() not in (".xlsx", ".xlsm", ".xls", ".xlsb"):
        sys.exit(f"{left.name} is not a workbook, so there is nothing to compare it with. Give --right <other file>.")
    if right.resolve() == left.resolve() and left.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".xlsb"):
        sheets = list_sheets(left)
        if ls is None and rs is None:
            if len(sheets) < 2:
                sys.exit(f"{left.name} has one sheet. Give --right <other file>, or use a workbook with two sheets.")
            ls, rs = sheets[0], sheets[1]
            say(f"Same workbook for both sides: comparing sheets '{ls}' and '{rs}'. (Use --left-sheet / --right-sheet to choose.)")
        elif ls == rs:
            sys.exit("Left and right are the same sheet of the same file.")
        elif ls is None or rs is None:
            given = ls or rs
            other = next((s for s in sheets if s != given), None)
            if other is None:
                sys.exit("Need two different sheets.")
            ls, rs = (given, other) if ls else (other, given)
    return left, ls, right, rs


def load_sides(con, left, ls, right, rs, header_row=0):
    say("Loading data (first time only; later runs reuse the cached copy) ...")
    linfo = ingest(con, left, ls, "src_left", header_row, say)
    rinfo = ingest(con, right, rs, "src_right", header_row, say)
    return linfo, rinfo


def make_llm(args):
    from llm import configured

    if getattr(args, "no_llm", False):
        return None
    if not configured():
        say("LLM not configured (INTERNAL_LLM_URL / KEY / MODEL missing in .env): using heuristics and a plain-text summary.")
        return None
    from llm import get_llm

    return get_llm()


def parse_override(spec: str):
    """'Left=Right[:norm1,norm2]'  ->  key dict"""
    pair, _, norms = spec.partition(":")
    left, _, right = pair.partition("=")
    return {"left": left.strip(), "right": (right or left).strip(),
            "normalize": [n.strip() for n in norms.split(",") if n.strip()]}


def parse_rule(spec: str):
    """'Column=rule[:tolerance]'  ->  set_rule dict"""
    col, _, rest = spec.partition("=")
    rule, _, tol = rest.partition(":")
    d = {"column": col.strip(), "rule": rule.strip()}
    if tol.strip():
        d["tolerance"] = float(tol)
    return d


def out_dir_for(args) -> Path:
    if args.output:
        return Path(args.output)
    return config.OUTPUT_DIR / f"run_{dt.datetime.now():%Y%m%d_%H%M%S}"


def show_plan(plan):
    say("\n" + "=" * 78 + "\nPROPOSED PLAN\n" + "=" * 78)
    say(planner.plan_text(plan))
    say("=" * 78)


# ----------------------------------------------------------------------------- commands
def cmd_sheets(args):
    for f in [args.left] + ([args.right] if args.right else []):
        say(f"{f}:")
        for s in list_sheets(f):
            say(f"  - {s}")


def build_plan(args, con, llm):
    left, ls, right, rs = resolve_inputs(args)
    linfo, rinfo = load_sides(con, left, ls, right, rs, args.header_row)
    plan, pl, pr = planner.propose_plan(con, linfo, rinfo, llm=llm, data_mode=args.data_mode, progress=say)
    pl_by = {c["name"]: c for c in pl["columns"]}
    pr_by = {c["name"]: c for c in pr["columns"]}
    changes = {}
    if args.key:
        changes["keys"] = [parse_override(k) for k in args.key]
    if args.rule:
        changes["set_rule"] = [parse_rule(r) for r in args.rule]
    if args.ignore:
        changes["ignore"] = args.ignore
    if changes:
        try:
            for m in planner.apply_changes(plan, changes, pl_by, pr_by):
                say(f"  override: {m}")
        except (ValueError, KeyError) as exc:
            sys.exit(f"Invalid override: {exc}")
    return plan, linfo, rinfo


def execute(args, con, plan, llm, out_dir):
    if plan["validation"]["errors"]:
        sys.exit("Plan has errors, not running:\n  " + "\n  ".join(plan["validation"]["errors"]))
    say("\nRunning reconciliation ...")
    summary = engine.reconcile(con, plan, out_dir, progress=say, export_format=args.export_format)
    say("\n" + console_summary(summary))

    narrative = None
    try:
        if llm is not None:
            say("\nAsking the LLM to explain the results ...")
            narrative = explain_mod.explain(llm, summary, args.data_mode)
    except Exception as exc:
        say(f"  LLM explanation failed ({type(exc).__name__}: {str(exc)[:200]}); using a plain summary.")
    if not narrative:
        narrative = explain_mod.fallback_text(summary)
    (out_dir / "narrative.md").write_text(narrative, encoding="utf-8")
    report = write_report(con, summary, plan, out_dir / "reconciliation_report.xlsx", narrative=narrative)
    say("\n" + narrative)
    say(f"\nExcel report : {report}\nDetail files : {out_dir}")
    return summary


def cmd_plan(args):
    con = db.connect()
    plan, *_ = build_plan(args, con, make_llm(args))
    out = out_dir_for(args)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    show_plan(plan)
    say(f"\nSaved: {out / 'plan.json'}\nEdit it if needed, then run:  python main.py run --plan \"{out / 'plan.json'}\"")


def cmd_reconcile(args):
    con = db.connect()
    llm = make_llm(args)
    plan, *_ = build_plan(args, con, llm)
    out = out_dir_for(args)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    show_plan(plan)
    if not args.yes:
        ans = input("\nRun the reconciliation with this plan? [y/N] (n = stop; edit plan.json and use 'run --plan') ").strip().lower()
        if ans not in ("y", "yes"):
            say(f"Stopped. Plan saved to {out / 'plan.json'}")
            return
    execute(args, con, plan, llm, out)


def cmd_run(args):
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    con = db.connect()
    llm = make_llm(args)
    L, R = plan["left"], plan["right"]
    load_sides(con, L["path"], L["sheet"], R["path"], R["sheet"], L.get("header_row", 0))
    lcols, rcols = db.columns_of(con, "src_left"), db.columns_of(con, "src_right")
    errors, warnings = planner.validate_plan(plan, lcols, rcols)
    plan["validation"] = {"errors": errors, "warnings": warnings}
    show_plan(plan)
    execute(args, con, plan, llm, out_dir_for(args))


def cmd_chat(args):
    from agent import Session, build_agent
    from llm import configured, get_llm

    if not configured():
        sys.exit("chat needs a tool-calling LLM: set INTERNAL_LLM_URL / KEY / MODEL in .env")
    if config.LLM_MODE == "custom":
        sys.exit("chat needs an OpenAI-compatible endpoint (tool calling). Use 'reconcile' with INTERNAL_LLM_MODE=custom.")
    con = db.connect()
    left, ls, right, rs = resolve_inputs(args)
    linfo, rinfo = load_sides(con, left, ls, right, rs, args.header_row)
    llm = get_llm()
    session = Session(con, linfo, rinfo, llm, out_dir_for(args))
    agent = build_agent(session)
    say("\nReconciliation agent ready. Ask in plain English, e.g. 'propose a plan' or 'reconcile these two files'.")
    say("Commands: /plan show plan | /approve approve the current plan | /quit\n")
    history = []
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit", "exit", "quit"):
            break
        if text == "/plan":
            say(planner.plan_text(session.plan) if session.plan else "No plan yet.")
            continue
        if text == "/approve":
            if not session.plan:
                say("No plan yet. Ask the agent to propose one first.")
            elif session.plan["validation"]["errors"]:
                say("The plan has errors; fix them first.")
            else:
                session.approved = True
                say("Plan approved. Ask the agent to run the reconciliation.")
            continue
        result = agent.invoke({"messages": history + [("user", text)]})
        history = result["messages"]
        say("\nagent> " + str(history[-1].content) + "\n")


def cmd_clean(args):
    if args.all:
        shutil.rmtree(config.WORK_DIR, ignore_errors=True)
        say(f"Removed {config.WORK_DIR} (cached data and intermediate tables).")
        return
    con = db.connect()
    engine.cleanup(con)
    con.execute("CHECKPOINT")
    say("Dropped intermediate tables. (Use --all to also remove the cached source data.)")


# ----------------------------------------------------------------------------- argument parsing
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    ap = argparse.ArgumentParser(description="Reconcile two Excel/CSV files or two sheets, at 1M+ rows x 100+ columns")
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p, files=True):
        if files:
            p.add_argument("--left", required=True, help="left file (.xlsx .xls .csv .parquet)")
            p.add_argument("--right", help="right file (omit to use another sheet of the left workbook)")
            p.add_argument("--left-sheet")
            p.add_argument("--right-sheet")
            p.add_argument("--header-row", type=int, default=0, help="1-based header row (0 = first non-empty row)")
        p.add_argument("--output", help="output folder (default: output/run_<timestamp>)")
        p.add_argument("--no-llm", action="store_true", help="heuristics + plain summary only")
        p.add_argument("--data-mode", choices=["stats_only", "masked_samples"], default=config.LLM_DATA_MODE,
                       help="what the LLM may see (default stats_only: no cell values)")
        p.add_argument("--export-format", choices=["csv", "parquet"], default=config.EXPORT_FORMAT)

    def overrides(p):
        p.add_argument("--key", action="append", help="force a key: 'Left=Right[:upper,strip_zeros,numeric]' (repeat for composite)")
        p.add_argument("--rule", action="append", help="set a rule: 'Column=numeric:0.01' | 'Column=text' | 'Column=ignore'")
        p.add_argument("--ignore", action="append", help="column to leave out of the comparison")

    p = sub.add_parser("sheets", help="list sheets of Excel files")
    p.add_argument("--left", required=True)
    p.add_argument("--right")
    p.set_defaults(fn=cmd_sheets)

    p = sub.add_parser("plan", help="build and save a plan (no reconciliation)")
    common(p)
    overrides(p)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("reconcile", help="plan -> you approve -> run -> explain -> Excel report")
    common(p)
    overrides(p)
    p.add_argument("--yes", action="store_true", help="skip the approval prompt (unattended runs)")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("run", help="run a saved (and possibly edited) plan.json")
    common(p, files=False)
    p.add_argument("--plan", required=True)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("chat", help="interactive LangChain agent (needs a tool-calling model)")
    common(p)
    p.set_defaults(fn=cmd_chat)

    p = sub.add_parser("clean", help="drop intermediate tables (or --all cached data)")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_clean)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
