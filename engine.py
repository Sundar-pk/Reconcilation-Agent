"""The reconciliation engine. Pure DuckDB SQL: no LLM, no row-by-row Python.

Pipeline
  1. narrow key tables (lk, rk): normalised key + occurrence number, so duplicate keys pair up in order
  2. pairs: FULL OUTER JOIN of the key tables -> matched / only-left / only-right
  3. diffs_N: per-column mismatch flags for matched pairs, computed in batches of columns (bounded memory)
  4. aggregates, totals, pattern hints (all small result sets)
  5. exports of full detail (value mismatches, unmatched rows, duplicate keys) straight from DuckDB

Compare rules (per column):
  exact     raw text equality (blank == blank)
  text      trim + collapse spaces + case-insensitive
  numeric   both sides cast to DOUBLE, |a-b| <= tolerance (non-numeric values compared as text)
  date      compare calendar dates      datetime  compare timestamps      ignore  skip
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

import config
from db import lit, q
from keys import key_exprs

RULES = ("exact", "text", "numeric", "date", "datetime", "ignore")
EPS = 1e-9  # float noise allowance on top of the tolerance


def label_of(c: dict) -> str:
    return c["left"] if c["left"] == c["right"] else f"{c['left']} / {c['right']}"


def mismatch_sql(rule: str, lx: str, rx: str, tol: float = 0.0) -> str:
    """SQL boolean: TRUE when the two values differ under `rule`."""
    if rule == "exact":
        return f"({lx} IS DISTINCT FROM {rx})"
    if rule == "text":
        def norm(x):
            return f"NULLIF(lower(regexp_replace(trim({x}), '\\s+', ' ', 'g')), '')"
        return f"({norm(lx)} IS DISTINCT FROM {norm(rx)})"
    if rule == "numeric":
        a, b = f"TRY_CAST({lx} AS DOUBLE)", f"TRY_CAST({rx} AS DOUBLE)"
        return (f"(CASE WHEN {a} IS NOT NULL AND {b} IS NOT NULL THEN abs({a} - {b}) > {float(tol)!r} + {EPS} "
                f"WHEN {a} IS NULL AND {b} IS NULL THEN ({lx} IS DISTINCT FROM {rx}) ELSE TRUE END)")
    if rule in ("date", "datetime"):
        if rule == "date":
            a, b = f"CAST(TRY_CAST({lx} AS TIMESTAMP) AS DATE)", f"CAST(TRY_CAST({rx} AS TIMESTAMP) AS DATE)"
        else:
            a, b = f"TRY_CAST({lx} AS TIMESTAMP)", f"TRY_CAST({rx} AS TIMESTAMP)"
        return (f"(CASE WHEN {a} IS NOT NULL AND {b} IS NOT NULL THEN {a} <> {b} "
                f"WHEN {a} IS NULL AND {b} IS NULL THEN ({lx} IS DISTINCT FROM {rx}) ELSE TRUE END)")
    raise ValueError(f"Unknown rule '{rule}' (valid: {', '.join(RULES)})")


def _build_key_table(con, name: str, src: str, keys: list, side: str):
    if keys:
        exprs = key_exprs(keys, side)
        inner = ", ".join(f"{e} AS k{i}" for i, e in enumerate(exprs))
        ks = ", ".join(f"k{i}" for i in range(len(exprs)))
        notnull = " AND ".join(f"k{i} IS NOT NULL" for i in range(len(exprs)))
        con.execute(
            f"CREATE OR REPLACE TABLE {name} AS SELECT __row, {ks}, "
            f"row_number() OVER (PARTITION BY {ks} ORDER BY __row) AS occ "
            f"FROM (SELECT __row, {inner} FROM {src}) WHERE {notnull}"
        )
    else:  # no key: match row n with row n
        con.execute(
            f"CREATE OR REPLACE TABLE {name} AS SELECT __row, row_number() OVER (ORDER BY __row) AS occ FROM {src}"
        )


def _copy(con, select_sql: str, path: Path, fmt: str):
    opts = "FORMAT PARQUET" if fmt == "parquet" else "FORMAT CSV, HEADER"
    con.execute(f"COPY ({select_sql}) TO {lit(path.as_posix())} ({opts})")


def _f(x):
    return None if x is None else float(x)


def reconcile(con, plan: dict, out_dir, lt: str = "src_left", rt: str = "src_right", progress=print,
              export_format: str | None = None, max_detail: int | None = None,
              batch_cols: int | None = None) -> dict:
    fmt = (export_format or config.EXPORT_FORMAT).lower()
    ext = "parquet" if fmt == "parquet" else "csv"
    max_detail = max_detail or config.MAX_DETAIL_PER_COLUMN
    batch_cols = max(1, batch_cols or config.BATCH_COLUMNS)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    keys = plan.get("keys", [])
    key_l, key_r = {k["left"] for k in keys}, {k["right"] for k in keys}
    cols = [c for c in plan["columns"] if c.get("rule", "exact") != "ignore"
            and c["left"] not in key_l and c["right"] not in key_r]
    timings, t_all = {}, time.time()

    def done(name, t0):
        timings[name] = round(time.time() - t0, 2)
        progress(f"  {name}: {timings[name]}s")

    # 1+2: keys and pairing ------------------------------------------------------------------
    t0 = time.time()
    _build_key_table(con, "lk", lt, keys, "left")
    _build_key_table(con, "rk", rt, keys, "right")
    cond = [f"l.k{i} = r.k{i}" for i in range(len(keys))] + ["l.occ = r.occ"]
    if keys:
        disp = ", ".join(f"COALESCE(l.k{i}, r.k{i})" for i in range(len(keys)))
        key_str = (f"concat_ws(' | ', {disp}) || CASE WHEN COALESCE(l.occ, r.occ) > 1 "
                   f"THEN ' #' || CAST(COALESCE(l.occ, r.occ) AS VARCHAR) ELSE '' END")
    else:
        key_str = "'row ' || CAST(COALESCE(l.occ, r.occ) AS VARCHAR)"
    con.execute(
        f"CREATE OR REPLACE TABLE pairs AS SELECT l.__row AS l_row, r.__row AS r_row, {key_str} AS key_str "
        f"FROM lk l FULL OUTER JOIN rk r ON {' AND '.join(cond)}"
    )
    con.execute("CREATE OR REPLACE TABLE mp AS SELECT l_row, r_row, key_str FROM pairs "
                "WHERE l_row IS NOT NULL AND r_row IS NOT NULL ORDER BY l_row")
    left_rows = con.execute(f"SELECT count(*) FROM {lt}").fetchone()[0]
    right_rows = con.execute(f"SELECT count(*) FROM {rt}").fetchone()[0]
    left_keyed = con.execute("SELECT count(*) FROM lk").fetchone()[0]
    right_keyed = con.execute("SELECT count(*) FROM rk").fetchone()[0]
    matched = con.execute("SELECT count(*) FROM mp").fetchone()[0]
    dup = {}
    for side, tbl in (("left", "lk"), ("right", "rk")):
        r = con.execute(f"SELECT count(*) FILTER (WHERE occ > 1), count(*) FILTER (WHERE occ = 2) FROM {tbl}").fetchone()
        dup[side] = {"rows": r[0], "keys": r[1]} if keys else {"rows": 0, "keys": 0}
    done("pairing", t0)

    # 3: per-column mismatch flags in batches ---------------------------------------------------
    t0 = time.time()
    entries = []
    for i, c in enumerate(cols):
        entries.append({
            "left": c["left"], "right": c["right"], "label": label_of(c), "rule": c.get("rule", "exact"),
            "tolerance": float(c.get("tolerance", 0.0) or 0.0), "idx": i, "batch": i // batch_cols,
        })
    batches = [entries[i:i + batch_cols] for i in range(0, len(entries), batch_cols)]

    con.execute("DROP TABLE IF EXISTS rowflags")
    for bi, batch in enumerate(batches):
        inner, outer = [], []
        for e in batch:
            i = e["idx"]
            lx, rx = "l." + q(e["left"]), "r." + q(e["right"])
            inner.append(f"{mismatch_sql(e['rule'], lx, rx, e['tolerance'])} AS d{i}")
            outer.append(f"x.d{i}")
            if e["rule"] == "numeric":
                inner.append(f"TRY_CAST({lx} AS DOUBLE) AS xl{i}")
                inner.append(f"TRY_CAST({rx} AS DOUBLE) AS xr{i}")
                outer += [f"x.xl{i}", f"x.xr{i}"]
        nd = " + ".join(f"CAST(x.d{e['idx']} AS INTEGER)" for e in batch)
        con.execute(f"DROP TABLE IF EXISTS diffs_{bi}")
        con.execute(
            f"CREATE TABLE diffs_{bi} AS SELECT x.l_row, x.r_row, {', '.join(outer)}, ({nd}) AS nd "
            f"FROM (SELECT p.l_row, p.r_row, {', '.join(inner)} "
            f"FROM mp p JOIN {lt} l ON l.__row = p.l_row JOIN {rt} r ON r.__row = p.r_row) x"
        )
        if bi == 0:
            con.execute("CREATE TABLE rowflags AS SELECT l_row, r_row, nd FROM diffs_0")
        else:
            con.execute(f"CREATE TABLE rowflags_new AS SELECT f.l_row, f.r_row, f.nd + b.nd AS nd "
                        f"FROM rowflags f JOIN diffs_{bi} b USING (l_row)")
            con.execute("DROP TABLE rowflags")
            con.execute("ALTER TABLE rowflags_new RENAME TO rowflags")
        progress(f"    compared columns {batch[0]['idx'] + 1}-{batch[-1]['idx'] + 1} of {len(entries)}")
    if not batches:
        con.execute("CREATE TABLE rowflags AS SELECT l_row, r_row, 0 AS nd FROM mp")
    done("column comparison", t0)

    # 4: aggregates ---------------------------------------------------------------------------
    t0 = time.time()
    for bi, batch in enumerate(batches):
        sel = []
        for e in batch:
            i = e["idx"]
            sel.append(f"sum(CAST(d{i} AS INTEGER))")
            if e["rule"] == "numeric":
                sel += [f"sum(xl{i})", f"sum(xr{i})", f"max(abs(xl{i} - xr{i})) FILTER (WHERE d{i})"]
        row = con.execute(f"SELECT {', '.join(sel)} FROM diffs_{bi}").fetchone()
        p = 0
        for e in batch:
            e["mismatches"] = int(row[p] or 0)
            p += 1
            if e["rule"] == "numeric":
                e["sum_left_matched"], e["sum_right_matched"] = _f(row[p]), _f(row[p + 1])
                e["sum_diff_matched"] = (e["sum_left_matched"] or 0.0) - (e["sum_right_matched"] or 0.0)
                e["max_abs_diff"] = _f(row[p + 2])
                p += 3
            e["mismatch_pct"] = round(100.0 * e["mismatches"] / matched, 4) if matched else 0.0
    # totals over ALL rows (including unmatched), numeric columns only
    num = [e for e in entries if e["rule"] == "numeric"]
    if num:
        for side, tbl in (("left", lt), ("right", rt)):
            sel = ", ".join(f"sum(TRY_CAST({q(e[side])} AS DOUBLE))" for e in num)
            for e, v in zip(num, con.execute(f"SELECT {sel} FROM {tbl}").fetchone()):
                e[f"total_{side}"] = _f(v)
        for e in num:
            e["total_diff"] = (e["total_left"] or 0.0) - (e["total_right"] or 0.0)
    hist = con.execute("SELECT nd, count(*) FROM rowflags GROUP BY nd ORDER BY nd LIMIT 25").fetchall()
    identical = con.execute("SELECT count(*) FILTER (WHERE nd = 0) FROM rowflags").fetchone()[0]
    done("aggregates", t0)

    # 4b: pattern hints for the most-mismatching columns ---------------------------------------------
    t0 = time.time()
    worst = sorted([e for e in entries if e["mismatches"] > 0], key=lambda e: -e["mismatches"])
    for e in worst[:config.MAX_HINT_COLUMNS]:
        _hints(con, e, lt, rt)
    done("pattern analysis", t0)

    # 5: exports --------------------------------------------------------------------------------
    t0 = time.time()
    files = {}
    vm_path = out_dir / f"value_mismatches.{ext}"
    parts = []
    for e in worst:
        i, bt = e["idx"], f"diffs_{e['batch']}"
        diff = f"d.xl{i} - d.xr{i}" if e["rule"] == "numeric" else "CAST(NULL AS DOUBLE)"
        parts.append(
            f"(SELECT m.key_str AS key, d.l_row AS left_row, d.r_row AS right_row, {lit(e['label'])} AS column_name, "
            f"l.{q(e['left'])} AS left_value, r.{q(e['right'])} AS right_value, {diff} AS numeric_diff "
            f"FROM {bt} d JOIN mp m ON m.l_row = d.l_row JOIN {lt} l ON l.__row = d.l_row "
            f"JOIN {rt} r ON r.__row = d.r_row WHERE d.d{i} LIMIT {int(max_detail)})"
        )
    empty = ("SELECT CAST(NULL AS VARCHAR) AS key, CAST(NULL AS BIGINT) AS left_row, CAST(NULL AS BIGINT) AS right_row, "
             "CAST(NULL AS VARCHAR) AS column_name, CAST(NULL AS VARCHAR) AS left_value, "
             "CAST(NULL AS VARCHAR) AS right_value, CAST(NULL AS DOUBLE) AS numeric_diff WHERE FALSE")
    _copy(con, " UNION ALL ".join(parts) if parts else empty, vm_path, fmt)
    files["value_mismatches"] = str(vm_path)

    for side, tbl, mine, other, name in (("left", lt, "l_row", "r_row", "only_in_left"),
                                         ("right", rt, "r_row", "l_row", "only_in_right")):
        path = out_dir / f"{name}.{ext}"
        _copy(con,
              f"SELECT s.__row AS source_row, CASE WHEN p.{mine} IS NULL THEN 'blank_key' ELSE 'not_matched' END AS reason, "
              f"s.* EXCLUDE (__row) FROM {tbl} s LEFT JOIN pairs p ON p.{mine} = s.__row WHERE p.{other} IS NULL",
              path, fmt)
        files[name] = str(path)

    if keys:
        ks = ", ".join(f"k{i}" for i in range(len(keys)))
        for side, tbl in (("left", "lk"), ("right", "rk")):
            if dup[side]["rows"]:
                path = out_dir / f"duplicate_keys_{side}.{ext}"
                _copy(con, f"SELECT {ks}, count(*) AS occurrences, min(__row) AS first_row, max(__row) AS last_row "
                           f"FROM {tbl} GROUP BY {ks} HAVING count(*) > 1 ORDER BY occurrences DESC LIMIT 100000",
                      path, fmt)
                files[f"duplicate_keys_{side}"] = str(path)
    done("exports", t0)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "left": {"path": plan["left"].get("path"), "sheet": plan["left"].get("sheet"), "rows": left_rows},
        "right": {"path": plan["right"].get("path"), "sheet": plan["right"].get("sheet"), "rows": right_rows},
        "keys": keys,
        "key_mode": "key" if keys else "row position",
        "counts": {
            "left_rows": left_rows, "right_rows": right_rows, "matched": matched,
            "only_in_left": left_rows - matched, "only_in_right": right_rows - matched,
            "left_blank_key_rows": left_rows - left_keyed, "right_blank_key_rows": right_rows - right_keyed,
            "left_duplicate_rows": dup["left"]["rows"], "left_duplicate_keys": dup["left"]["keys"],
            "right_duplicate_rows": dup["right"]["rows"], "right_duplicate_keys": dup["right"]["keys"],
            "identical_rows": identical, "rows_with_differences": matched - identical,
            "compared_columns": len(entries),
            "match_rate_left_pct": round(100.0 * matched / left_rows, 4) if left_rows else 0.0,
            "match_rate_right_pct": round(100.0 * matched / right_rows, 4) if right_rows else 0.0,
        },
        "differing_columns_per_row": [{"differing_columns": int(a), "rows": int(b)} for a, b in hist],
        "columns": entries,
        "not_compared": {"unmapped_left": plan.get("unmapped_left", []), "unmapped_right": plan.get("unmapped_right", []),
                         "ignored": [label_of(c) for c in plan["columns"] if c.get("rule") == "ignore"]},
        "files": files,
        "settings": {"export_format": fmt, "max_detail_per_column": max_detail, "batch_columns": batch_cols},
        "timings_seconds": {**timings, "total": round(time.time() - t_all, 2)},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    files["summary"] = str(out_dir / "summary.json")
    return summary


# ----------------------------------------------------------------------------- hints
def _hints(con, e: dict, lt: str, rt: str):
    i, bt = e["idx"], f"diffs_{e['batch']}"
    if e["rule"] == "numeric":
        r = con.execute(
            f"SELECT count(*) FILTER (WHERE xl IS NOT NULL AND xr IS NOT NULL AND abs(xl - xr) <= 0.01 + {EPS}), "
            f"count(*) FILTER (WHERE xl IS NOT NULL AND xr IS NOT NULL AND abs(xl - xr) <= 1 + {EPS}), "
            f"count(*) FILTER (WHERE xl > xr), count(*) FILTER (WHERE xl < xr), "
            f"count(*) FILTER (WHERE xl IS NULL OR xr IS NULL) "
            f"FROM (SELECT xl{i} AS xl, xr{i} AS xr FROM {bt} WHERE d{i})"
        ).fetchone()
        e["hints"] = {"within_0.01": r[0], "within_1": r[1], "left_greater": r[2], "left_smaller": r[3],
                      "one_side_blank_or_non_numeric": r[4]}
        top = con.execute(
            f"SELECT round(xl{i} - xr{i}, 6) AS v, count(*) AS c FROM {bt} WHERE d{i} AND xl{i} IS NOT NULL "
            f"AND xr{i} IS NOT NULL GROUP BY 1 ORDER BY c DESC, abs(v) LIMIT 5"
        ).fetchall()
        e["top_numeric_diffs"] = [{"left_minus_right": _f(v), "rows": c} for v, c in top]
        return
    lx, rx = f"l.{q(e['left'])}", f"r.{q(e['right'])}"
    base = (f"FROM {bt} d JOIN {lt} l ON l.__row = d.l_row JOIN {rt} r ON r.__row = d.r_row WHERE d.d{i}")
    r = con.execute(
        f"SELECT count(*) FILTER (WHERE lower(trim({lx})) = lower(trim({rx}))), "
        f"count(*) FILTER (WHERE {lx} IS NULL AND {rx} IS NOT NULL), count(*) FILTER (WHERE {rx} IS NULL AND {lx} IS NOT NULL), "
        f"count(*) FILTER (WHERE TRY_CAST({lx} AS DOUBLE) IS NOT NULL AND TRY_CAST({lx} AS DOUBLE) = TRY_CAST({rx} AS DOUBLE)) {base}"
    ).fetchone()
    e["hints"] = {"equal_ignoring_case_and_spaces": r[0], "left_blank_right_has_value": r[1],
                  "right_blank_left_has_value": r[2], "equal_as_numbers": r[3]}
    top = con.execute(f"SELECT {lx}, {rx}, count(*) AS c {base} GROUP BY 1, 2 ORDER BY c DESC LIMIT 5").fetchall()
    e["top_value_changes"] = [{"left": a, "right": b, "rows": c} for a, b, c in top]  # contains cell values


def mismatch_samples(con, e: dict, n: int = 5, lt: str = "src_left", rt: str = "src_right") -> list:
    """A few mismatching rows for one column entry (contains cell values)."""
    i, bt = e["idx"], f"diffs_{e['batch']}"
    rows = con.execute(
        f"SELECT m.key_str, d.l_row, d.r_row, l.{q(e['left'])}, r.{q(e['right'])} FROM {bt} d "
        f"JOIN mp m ON m.l_row = d.l_row JOIN {lt} l ON l.__row = d.l_row JOIN {rt} r ON r.__row = d.r_row "
        f"WHERE d.d{i} ORDER BY d.l_row LIMIT {int(n)}"
    ).fetchall()
    return [{"key": k, "left_row": a, "right_row": b, "left_value": lv, "right_value": rv} for k, a, b, lv, rv in rows]


def cleanup(con):
    """Drop intermediate tables (keeps the loaded source tables)."""
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name IN ('lk','rk','pairs','mp','rowflags') "
        "OR table_name LIKE 'diffs\\_%' ESCAPE '\\'").fetchall()]
    for n in names:
        con.execute(f"DROP TABLE IF EXISTS {n}")
