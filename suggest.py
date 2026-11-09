"""Deterministic suggestions: column mapping, key columns, comparison rules.

The LLM later reviews / refines these, but everything here is verifiable with plain statistics.
"""
from __future__ import annotations

import difflib
import itertools
import re

from keys import evaluate_key

SYN = {
    "no": "number", "num": "number", "nbr": "number", "nr": "number", "amt": "amount", "qty": "quantity",
    "dt": "date", "desc": "description", "descr": "description", "cust": "customer", "acct": "account",
    "acc": "account", "ref": "reference", "txn": "transaction", "trans": "transaction", "curr": "currency",
    "ccy": "currency", "addr": "address", "dept": "department", "val": "value", "identifier": "id",
}
KEY_HINTS = {"id", "number", "code", "key", "reference", "account", "invoice", "order", "sku",
             "transaction", "policy", "uuid", "guid", "document", "record"}
CONFIDENT_SCORE, CONFIDENT_MARGIN, MIN_SCORE = 0.9, 0.1, 0.55


def _tokens(name) -> list:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).lower().strip()
    return [SYN.get(t, t) for t in s.split()]


def name_score(a, b) -> float:
    if str(a).strip().lower() == str(b).strip().lower():
        return 1.0
    ta, tb = _tokens(a), _tokens(b)
    if ta == tb:
        return 0.98
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb) if (sa | sb) else 0.0
    ratio = difflib.SequenceMatcher(None, "".join(ta), "".join(tb)).ratio()
    return max(jac * 0.95, ratio * 0.9)


def _type_bonus(tl: str, tr: str) -> float:
    if tl == tr:
        return 0.05
    pair = {tl, tr}
    if pair <= {"text", "code"}:
        return 0.0
    if pair <= {"numeric", "code"}:
        return -0.05
    if "empty" in pair:
        return 0.0
    return -0.3


def pair_score(pl: dict, pr: dict) -> float:
    s = name_score(pl["name"], pr["name"]) + _type_bonus(pl["type"], pr["type"])
    dl, dr = pl["distinct"], pr["distinct"]
    if dl and dr and min(dl, dr) / max(dl, dr) < 0.05:
        s -= 0.1
    return s


def suggest_mapping(prof_l: dict, prof_r: dict) -> dict:
    L, R = prof_l["columns"], prof_r["columns"]
    scores = {}
    by_l, by_r = {}, {}
    for a in L:
        for b in R:
            s = pair_score(a, b)
            scores[(a["name"], b["name"])] = s
            by_l.setdefault(a["name"], []).append((s, b["name"]))
            by_r.setdefault(b["name"], []).append((s, a["name"]))
    for d in (by_l, by_r):
        for v in d.values():
            v.sort(reverse=True)

    taken_l, taken_r, pairs = set(), set(), []
    for (ln, rn), s in sorted(scores.items(), key=lambda kv: -kv[1]):
        if s < MIN_SCORE:
            break
        if ln in taken_l or rn in taken_r:
            continue
        taken_l.add(ln)
        taken_r.add(rn)
        second_l = next((x for x, other in by_l[ln] if other != rn), 0.0)
        second_r = next((x for x, other in by_r[rn] if other != ln), 0.0)
        margin = s - max(second_l, second_r)
        status = "confident" if (s >= CONFIDENT_SCORE and margin >= CONFIDENT_MARGIN) else "review"
        pairs.append({"left": ln, "right": rn, "score": round(s, 3), "margin": round(margin, 3), "status": status})

    order = {c["name"]: i for i, c in enumerate(L)}
    pairs.sort(key=lambda p: order[p["left"]])
    return {
        "pairs": pairs,
        "unmapped_left": [c["name"] for c in L if c["name"] not in taken_l],
        "unmapped_right": [c["name"] for c in R if c["name"] not in taken_r],
    }


# ----------------------------------------------------------------------------- keys
def _variants(ptype: str) -> list:
    v = [[]]
    if ptype in ("text", "code"):
        v += [["upper"], ["strip_zeros"], ["upper", "strip_zeros"]]
    if ptype in ("numeric", "code"):
        v += [["numeric"], ["strip_zeros"]]
    return v


def suggest_keys(con, lt: str, rt: str, pairs: list, pl_by: dict, pr_by: dict, progress=print) -> dict:
    warnings, cands = [], []
    for p in pairs:
        L, R = pl_by[p["left"]], pr_by[p["right"]]
        if "empty" in (L["type"], R["type"]) or L["type"] == "date":
            continue
        if L["null_pct"] > 5 or R["null_pct"] > 5:
            continue
        hint = any(t in KEY_HINTS for t in _tokens(p["left"]))
        prelim = min(L["uniq_ratio"], R["uniq_ratio"]) + (0.15 if hint else 0.0)
        if min(L["uniq_ratio"], R["uniq_ratio"]) >= 0.5:
            cands.append((prelim, p, L["type"]))
    cands.sort(key=lambda x: -x[0])
    shortlist = cands[:8]

    singles = []
    for _, p, ptype in shortlist:
        best = None
        for norms in _variants(ptype):
            key = [{"left": p["left"], "right": p["right"], "normalize": norms}]
            ev = evaluate_key(con, lt, rt, key)
            if best is None or ev["score"] > best[1]["score"] + 1e-9:
                best = (key, ev)
            if ev["uniq_left"] >= 0.999 and ev["uniq_right"] >= 0.999 and ev["overlap"] >= 0.98:
                break  # good enough, don't try more variants
        singles.append(best)
    singles.sort(key=lambda x: -x[1]["score"])

    def good(ev):
        return ev["uniq_left"] >= 0.999 and ev["uniq_right"] >= 0.999 and ev["overlap"] >= 0.5

    def near_unique(ev):  # a few genuine duplicates must not push us into inventing a composite key
        return min(ev["uniq_left"], ev["uniq_right"]) >= 0.99 and ev["overlap"] >= 0.5

    chosen = None
    if singles and good(singles[0][1]):
        chosen = singles[0]
    elif singles and near_unique(singles[0][1]):
        chosen = singles[0]
        warnings.append("Key column is almost unique; the duplicate keys are reported and paired in file order.")
    else:  # try composite keys from the best few single columns
        pool = [s for s in singles if s[1]["overlap"] >= 0.5][:6]
        combos = []
        for a, b in itertools.combinations(pool, 2):
            key = a[0] + b[0]
            combos.append((key, evaluate_key(con, lt, rt, key)))
        combos.sort(key=lambda x: -x[1]["score"])
        if combos and good(combos[0][1]):
            chosen = combos[0]
        elif singles and singles[0][1]["score"] >= 0.5 and min(singles[0][1]["uniq_left"], singles[0][1]["uniq_right"]) >= 0.9:
            chosen = singles[0]
            warnings.append("Best key is not fully unique; duplicate keys will be paired in file order.")

    alternatives = [{"keys": k, "evaluation": e} for k, e in (singles[:5])]
    if not chosen:
        warnings.append("No reliable key column found. Rows would be matched by position; please specify a key.")
        return {"keys": [], "evaluation": None, "alternatives": alternatives, "warnings": warnings}
    return {"keys": chosen[0], "evaluation": chosen[1], "alternatives": alternatives, "warnings": warnings}


# ----------------------------------------------------------------------------- rules
def default_rule(tl: str, tr: str) -> str:
    if tl == "numeric" and tr == "numeric":
        return "numeric"
    if tl == "date" and tr == "date":
        return "date"
    return "exact"
