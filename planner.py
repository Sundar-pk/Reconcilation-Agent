"""Reconciliation plan = key columns + column mapping + compare rules.

1. Heuristics propose a plan from column names, types and statistics (verified against the data).
2. The LLM (optional) reviews it. It sees column names and statistics only (never cell values),
   unless LLM_DATA_MODE=masked_samples. Its suggestions are validated and cannot override
   mappings the heuristics were confident about.
3. validate_plan() checks the result. A human approves it before anything runs.
"""
from __future__ import annotations

import json
import re

import config
from engine import RULES, label_of
from keys import VALID_NORMALIZERS, evaluate_key
from profile import profile
from suggest import default_rule, suggest_keys, suggest_mapping

PLAN_SYSTEM = """You are a data-reconciliation analyst helping to compare two tables (LEFT and RIGHT).
You receive column statistics (not the data itself) and a draft plan produced by heuristics.
Reply with ONE JSON object and nothing else, in exactly this shape:
{
  "mapping": [{"left": "<left column>", "right": "<right column>", "rule": "exact|text|numeric|date|datetime|ignore", "tolerance": 0}],
  "keys": [{"left": "<left column>", "right": "<right column>", "normalize": []}],
  "ignore_left": [],
  "ignore_right": [],
  "notes": ["short sentences"]
}
Guidance:
- "mapping": list ONLY pairs you want to add or change: resolve "needs_review" items and pair unmapped columns
  that clearly mean the same thing, or set a better rule. Do not repeat confident pairs unless changing their rule.
- Use only column names that appear in the input, exactly as written. Each column can be in at most one pair.
- "keys": leave empty unless the draft key looks wrong. A good key is unique on both sides and its values overlap
  between the sides (see key_candidates). normalize options: "upper", "strip_zeros", "numeric".
- rule: "numeric" for amounts/quantities (tolerance = absolute rounding allowance, 0 if none is justified),
  "date" for calendar dates, "text" to ignore case/extra spaces, otherwise "exact".
- Never invent column names or numbers. If unsure, leave things unchanged and say why in "notes"."""


# ----------------------------------------------------------------------------- helpers
def _mask(v) -> str:
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", str(v))
    s = re.sub(r"\d{5,}", lambda m: m.group(0)[:2] + "#" * (len(m.group(0)) - 2), s)
    return s[:40]


def _compact(p: dict, mode: str) -> dict:
    d = {k: p[k] for k in ("name", "type", "null_pct", "distinct", "uniq_ratio")}
    if p["type"] == "numeric":
        d["min"], d["max"] = p["min"], p["max"]
    if mode == "masked_samples":
        d["examples"] = [_mask(v) for v in p["top_values"]]
    return d


def _src(info: dict) -> dict:
    return {"path": info["path"], "sheet": info["sheet"], "header_row": info.get("header_row", 0)}


# ----------------------------------------------------------------------------- plan building
def heuristic_plan(con, linfo, rinfo, pl, pr, progress=print) -> dict:
    pl_by = {c["name"]: c for c in pl["columns"]}
    pr_by = {c["name"]: c for c in pr["columns"]}
    m = suggest_mapping(pl, pr)
    k = suggest_keys(con, linfo["table"], rinfo["table"], m["pairs"], pl_by, pr_by, progress)
    kl = {x["left"] for x in k["keys"]}
    kr = {x["right"] for x in k["keys"]}
    columns = []
    for p in m["pairs"]:
        if p["left"] in kl and p["right"] in kr:
            continue
        columns.append({"left": p["left"], "right": p["right"],
                        "rule": default_rule(pl_by[p["left"]]["type"], pr_by[p["right"]]["type"]),
                        "tolerance": 0.0, "match": p["status"], "score": p["score"]})
    return {
        "version": 1, "left": _src(linfo), "right": _src(rinfo), "keys": k["keys"], "columns": columns,
        "unmapped_left": m["unmapped_left"], "unmapped_right": m["unmapped_right"],
        "notes": list(k["warnings"]), "key_evaluation": k["evaluation"], "key_alternatives": k["alternatives"],
    }


def propose_plan(con, linfo, rinfo, llm=None, data_mode: str | None = None, progress=print):
    """Returns (plan, left_profile, right_profile)."""
    mode = data_mode or config.LLM_DATA_MODE
    progress("Profiling columns ...")
    pl, pr = profile(con, linfo["table"]), profile(con, rinfo["table"])
    progress("Building plan (mapping, keys, rules) ...")
    plan = heuristic_plan(con, linfo, rinfo, pl, pr, progress)
    if llm is not None:
        progress("Asking the LLM to review the plan ...")
        try:
            _refine_with_llm(con, llm, plan, pl, pr, linfo, rinfo, mode)
        except Exception as exc:  # the heuristic plan is still usable
            plan["notes"].append(f"LLM review skipped ({type(exc).__name__}: {str(exc)[:200]}). Heuristic plan kept.")
    errors, warnings = validate_plan(plan, [c["name"] for c in pl["columns"]], [c["name"] for c in pr["columns"]])
    plan["validation"] = {"errors": errors, "warnings": warnings}
    return plan, pl, pr


def _refine_with_llm(con, llm, plan, pl, pr, linfo, rinfo, mode):
    from llm import llm_json

    pl_by = {c["name"]: c for c in pl["columns"]}
    pr_by = {c["name"]: c for c in pr["columns"]}
    review = [c for c in plan["columns"] if c["match"] == "review"]
    payload = {
        "left_rows": pl["rows"], "right_rows": pr["rows"],
        "draft_keys": plan["keys"], "draft_key_quality": plan["key_evaluation"],
        "key_candidates": [{"keys": a["keys"], "quality": a["evaluation"]} for a in plan["key_alternatives"]],
        "confident_pairs": [[c["left"], c["right"], c["rule"]] for c in plan["columns"] if c["match"] == "confident"],
        "needs_review": [{"left": _compact(pl_by[c["left"]], mode), "right": _compact(pr_by[c["right"]], mode),
                          "name_score": c["score"], "draft_rule": c["rule"]} for c in review],
        "unmapped_left": [_compact(pl_by[n], mode) for n in plan["unmapped_left"]],
        "unmapped_right": [_compact(pr_by[n], mode) for n in plan["unmapped_right"]],
        "data_mode": mode,
    }
    resp = llm_json(llm, PLAN_SYSTEM, json.dumps(payload, default=str))
    plan["notes"] += _apply_llm(con, plan, resp, linfo, rinfo, pl_by, pr_by)
    plan["llm_notes"] = [str(n)[:300] for n in (resp.get("notes") or [])][:10]


def _apply_llm(con, plan, resp, linfo, rinfo, pl_by, pr_by) -> list:
    notes = []
    lset, rset = set(pl_by), set(pr_by)
    key_l = {k["left"] for k in plan["keys"]}
    key_r = {k["right"] for k in plan["keys"]}

    for m in resp.get("mapping") or []:
        l, r = m.get("left"), m.get("right")
        if l not in lset or r not in rset:
            notes.append(f"LLM suggested unknown columns {l!r} / {r!r}; ignored.")
            continue
        if l in key_l or r in key_r:
            notes.append(f"LLM tried to remap key column {l!r}/{r!r}; ignored.")
            continue
        by_l = {c["left"]: c for c in plan["columns"]}
        by_r = {c["right"]: c for c in plan["columns"]}
        cur = by_l.get(l)
        if cur is None or cur["right"] != r:
            cl, cr = by_l.get(l), by_r.get(r)
            if (cl and cl["match"] == "confident") or (cr and cr["match"] == "confident"):
                notes.append(f"LLM tried to change a confident mapping ({l!r} / {r!r}); ignored.")
                continue
            for old in (cl, cr):
                if old and old in plan["columns"]:
                    plan["columns"].remove(old)
                    plan["unmapped_left"].append(old["left"]) if old["left"] != l else None
                    plan["unmapped_right"].append(old["right"]) if old["right"] != r else None
            plan["unmapped_left"] = [x for x in plan["unmapped_left"] if x != l]
            plan["unmapped_right"] = [x for x in plan["unmapped_right"] if x != r]
            cur = {"left": l, "right": r, "rule": default_rule(pl_by[l]["type"], pr_by[r]["type"]),
                   "tolerance": 0.0, "match": "llm", "score": None}
            plan["columns"].append(cur)
        rule = m.get("rule")
        if rule in RULES:
            cur["rule"] = rule
        try:
            tol = float(m.get("tolerance", cur.get("tolerance", 0.0)) or 0.0)
            if tol >= 0:
                cur["tolerance"] = tol
        except (TypeError, ValueError):
            pass

    new_keys = [k for k in (resp.get("keys") or []) if isinstance(k, dict)]
    if new_keys:
        try:
            ok = all(k.get("left") in lset and k.get("right") in rset for k in new_keys)
            norms = [[n for n in (k.get("normalize") or []) if n in VALID_NORMALIZERS] for k in new_keys]
            cand = [{"left": k["left"], "right": k["right"], "normalize": n} for k, n in zip(new_keys, norms)] if ok else []
            if cand and cand != plan["keys"]:
                ev = evaluate_key(con, linfo["table"], rinfo["table"], cand)
                cur_ev = plan["key_evaluation"]
                if cur_ev is None or ev["score"] > cur_ev["score"] + 0.02:
                    set_keys(plan, cand, pl_by, pr_by)
                    plan["key_evaluation"] = ev
                    notes.append("Key changed on LLM suggestion (verified better against the data).")
                else:
                    notes.append("LLM suggested a different key, but the data does not support it; draft key kept.")
        except Exception as exc:
            notes.append(f"LLM key suggestion rejected: {exc}")

    for name in resp.get("ignore_left") or []:
        for c in plan["columns"]:
            if c["left"] == name and c["match"] != "confident":
                c["rule"] = "ignore"
    for name in resp.get("ignore_right") or []:
        for c in plan["columns"]:
            if c["right"] == name and c["match"] != "confident":
                c["rule"] = "ignore"
    return notes


def set_keys(plan: dict, keys: list, pl_by: dict, pr_by: dict):
    """Replace the plan's key columns, moving old key columns back into the compared columns."""
    old = plan["keys"]
    new_l, new_r = {k["left"] for k in keys}, {k["right"] for k in keys}
    plan["columns"] = [c for c in plan["columns"] if c["left"] not in new_l and c["right"] not in new_r]
    have = {c["left"] for c in plan["columns"]}
    for k in old:
        if k["left"] not in new_l and k["right"] not in new_r and k["left"] not in have:
            plan["columns"].append({"left": k["left"], "right": k["right"],
                                    "rule": default_rule(pl_by[k["left"]]["type"], pr_by[k["right"]]["type"]),
                                    "tolerance": 0.0, "match": "confident", "score": None})
    plan["unmapped_left"] = [x for x in plan["unmapped_left"] if x not in new_l]
    plan["unmapped_right"] = [x for x in plan["unmapped_right"] if x not in new_r]
    plan["keys"] = keys


# ----------------------------------------------------------------------------- edits (CLI overrides / chat tool)
def apply_changes(plan: dict, changes: dict, pl_by: dict, pr_by: dict) -> list:
    """changes = {"keys": [...], "set_rule": [{"column": "Amount", "rule": "numeric", "tolerance": 0.01}],
                  "ignore": ["col"], "map": [{"left": "A", "right": "B", "rule": "exact"}]}"""
    msgs = []
    if changes.get("keys") is not None:
        keys = []
        for k in changes["keys"]:
            if k.get("left") not in pl_by or k.get("right") not in pr_by:
                raise ValueError(f"Unknown key column(s): {k}")
            norms = k.get("normalize") or []
            bad = [n for n in norms if n not in VALID_NORMALIZERS]
            if bad:
                raise ValueError(f"Unknown normaliser {bad}; valid: {VALID_NORMALIZERS}")
            keys.append({"left": k["left"], "right": k["right"], "normalize": norms})
        set_keys(plan, keys, pl_by, pr_by)
        plan["key_evaluation"] = None
        msgs.append(f"keys set to {[k['left'] + '=' + k['right'] for k in keys]}")
    for m in changes.get("map") or []:
        l, r = m.get("left"), m.get("right")
        if l not in pl_by or r not in pr_by:
            raise ValueError(f"Unknown column in map: {m}")
        plan["columns"] = [c for c in plan["columns"] if c["left"] != l and c["right"] != r]
        plan["unmapped_left"] = [x for x in plan["unmapped_left"] if x != l]
        plan["unmapped_right"] = [x for x in plan["unmapped_right"] if x != r]
        plan["columns"].append({"left": l, "right": r, "rule": m.get("rule") or default_rule(pl_by[l]["type"], pr_by[r]["type"]),
                                "tolerance": float(m.get("tolerance", 0) or 0), "match": "user", "score": None})
        msgs.append(f"mapped {l} -> {r}")
    for s in changes.get("set_rule") or []:
        name = s.get("column")
        hit = [c for c in plan["columns"] if name in (c["left"], c["right"], label_of(c))]
        if not hit:
            raise ValueError(f"No compared column named {name!r}")
        if s.get("rule") not in RULES:
            raise ValueError(f"Unknown rule {s.get('rule')!r}; valid: {RULES}")
        for c in hit:
            c["rule"] = s["rule"]
            if "tolerance" in s:
                c["tolerance"] = float(s["tolerance"] or 0)
        msgs.append(f"{name}: rule={s['rule']}" + (f", tolerance={s['tolerance']}" if "tolerance" in s else ""))
    for name in changes.get("ignore") or []:
        hit = [c for c in plan["columns"] if name in (c["left"], c["right"], label_of(c))]
        if not hit:
            raise ValueError(f"No compared column named {name!r}")
        for c in hit:
            c["rule"] = "ignore"
        msgs.append(f"ignoring {name}")
    errors, warnings = validate_plan(plan, list(pl_by), list(pr_by))
    plan["validation"] = {"errors": errors, "warnings": warnings}
    return msgs


# ----------------------------------------------------------------------------- validation / display
def validate_plan(plan: dict, lcols: list, rcols: list):
    errors, warnings = [], []
    lset, rset = set(lcols), set(rcols)
    used_l, used_r = set(), set()
    for k in plan.get("keys", []):
        if k["left"] not in lset or k["right"] not in rset:
            errors.append(f"Key column not found: {k['left']} / {k['right']}")
        bad = [n for n in k.get("normalize", []) if n not in VALID_NORMALIZERS]
        if bad:
            errors.append(f"Unknown key normaliser {bad} for {k['left']}")
        used_l.add(k["left"])
        used_r.add(k["right"])
    for c in plan.get("columns", []):
        if c["left"] not in lset:
            errors.append(f"Left column not found: {c['left']}")
        if c["right"] not in rset:
            errors.append(f"Right column not found: {c['right']}")
        if c.get("rule", "exact") not in RULES:
            errors.append(f"Unknown rule {c.get('rule')!r} for {label_of(c)}")
        if float(c.get("tolerance", 0) or 0) < 0:
            errors.append(f"Negative tolerance for {label_of(c)}")
        if c["left"] in used_l:
            errors.append(f"Left column used twice: {c['left']}")
        if c["right"] in used_r:
            errors.append(f"Right column used twice: {c['right']}")
        used_l.add(c["left"])
        used_r.add(c["right"])
    if not plan.get("keys"):
        warnings.append("No key columns: rows will be matched by position (row n with row n).")
    ev = plan.get("key_evaluation")
    if ev:
        if ev["overlap"] < 0.5:
            warnings.append(f"Key values overlap only {ev['overlap']:.0%} between the two sides.")
        if ev["left_duplicate_rows"] or ev["right_duplicate_rows"]:
            warnings.append(f"Key is not unique ({ev['left_duplicate_rows']:,} left / {ev['right_duplicate_rows']:,} right "
                            "duplicate rows); duplicates are paired in file order.")
    review = [c for c in plan.get("columns", []) if c.get("match") == "review"]
    if review:
        warnings.append(f"{len(review)} column pair(s) were matched by similar name only; please check them.")
    return errors, warnings


def plan_text(plan: dict, max_rows: int = 60) -> str:
    L, R = plan["left"], plan["right"]
    out = [f"LEFT : {L['path']} [{L['sheet']}]", f"RIGHT: {R['path']} [{R['sheet']}]", ""]
    if plan["keys"]:
        out.append("Match rows on:")
        for k in plan["keys"]:
            n = f"  (normalise: {', '.join(k['normalize'])})" if k.get("normalize") else ""
            out.append(f"  {k['left']}  =  {k['right']}{n}")
        ev = plan.get("key_evaluation")
        if ev:
            out.append(f"  key quality: unique {ev['uniq_left']:.2%} / {ev['uniq_right']:.2%}, "
                       f"{ev['overlap']:.2%} of keys found on both sides")
    else:
        out.append("Match rows on: ROW POSITION (no key found)")
    out.append("")
    cols = plan["columns"]
    out.append(f"Compared columns ({len([c for c in cols if c['rule'] != 'ignore'])} of {len(cols)} mapped):")
    out.append(f"  {'LEFT':<28}{'RIGHT':<28}{'RULE':<10}{'TOL':<8}MATCH")
    for c in cols[:max_rows]:
        tol = f"{c['tolerance']:g}" if c["rule"] == "numeric" else ""
        out.append(f"  {c['left'][:26]:<28}{c['right'][:26]:<28}{c['rule']:<10}{tol:<8}{c.get('match', '')}")
    if len(cols) > max_rows:
        out.append(f"  ... {len(cols) - max_rows} more (see plan.json)")
    if plan["unmapped_left"]:
        out.append(f"Only in LEFT (not compared): {', '.join(plan['unmapped_left'][:20])}"
                   + (" ..." if len(plan["unmapped_left"]) > 20 else ""))
    if plan["unmapped_right"]:
        out.append(f"Only in RIGHT (not compared): {', '.join(plan['unmapped_right'][:20])}"
                   + (" ..." if len(plan["unmapped_right"]) > 20 else ""))
    v = plan.get("validation", {})
    for e in v.get("errors", []):
        out.append(f"ERROR  : {e}")
    for w in v.get("warnings", []):
        out.append(f"WARNING: {w}")
    for n in plan.get("notes", []):
        if n not in v.get("warnings", []):
            out.append(f"NOTE   : {n}")
    for n in plan.get("llm_notes", []):
        out.append(f"LLM    : {n}")
    return "\n".join(out)
