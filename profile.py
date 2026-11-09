"""Column profiling: type inference and statistics, computed inside DuckDB.

Counts / approximate distincts use the whole table; type inference uses a systematic sample
(~50k rows), which keeps this fast even at 1M rows x 100 columns. Date parsing is only attempted
on columns that are not already clearly numeric.
"""
from __future__ import annotations

from db import columns_of, q

SAMPLE_TARGET = 50_000
N_STATS = 8  # per-column values returned by the sample query


def profile(con, table: str, sample_target: int = SAMPLE_TARGET) -> dict:
    cols = columns_of(con, table)
    n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    out = {"table": table, "rows": n, "columns": []}
    if n == 0 or not cols:
        return out

    sel = []
    for c in cols:
        sel += [f"count({q(c)})", f"approx_count_distinct({q(c)})"]
    full = con.execute(f"SELECT {', '.join(sel)} FROM {table}").fetchone()

    step = max(1, n // sample_target)
    where = f"WHERE __row % {step} = 0"
    ssel = []
    for c in cols:
        qc = q(c)
        ssel += [
            f"count({qc})",
            f"count(TRY_CAST({qc} AS DOUBLE))",
            f"count(*) FILTER (WHERE regexp_matches({qc}, '^0[0-9]+$'))",
            f"min(TRY_CAST({qc} AS DOUBLE))",
            f"max(TRY_CAST({qc} AS DOUBLE))",
            f"avg(length({qc}))",
            f"count(*) FILTER (WHERE {qc} <> trim({qc}))",
            f"approx_top_k({qc}, 3)",
        ]
    samp = con.execute(f"SELECT {', '.join(ssel)} FROM {table} {where}").fetchone()

    # dates: only for non-empty columns that are not already clearly numeric
    need_date = [i for i in range(len(cols)) if samp[N_STATS * i] and samp[N_STATS * i + 1] / samp[N_STATS * i] < 0.98]
    date_n = {}
    if need_date:
        dsel = [f"count(CAST(TRY_CAST({q(cols[i])} AS TIMESTAMP) AS DATE))" for i in need_date]
        date_n = dict(zip(need_date, con.execute(f"SELECT {', '.join(dsel)} FROM {table} {where}").fetchone()))

    for i, c in enumerate(cols):
        nn, distinct = full[2 * i], full[2 * i + 1]
        ns, num_n, lead0, mn, mx, avg_len, padded, top = samp[N_STATS * i: N_STATS * i + N_STATS]
        if ns == 0:
            ctype = "empty"
        else:
            nr, lr = num_n / ns, lead0 / ns
            dr = date_n.get(i, 0) / ns
            if nr >= 0.98 and lr < 0.01:
                ctype = "numeric"
            elif nr >= 0.98:
                ctype = "code"  # numeric-looking identifiers with leading zeros (zip codes, ids)
            elif dr >= 0.98:
                ctype = "date"
            else:
                ctype = "text"
        distinct = min(distinct, nn)
        out["columns"].append({
            "name": c,
            "type": ctype,
            "non_null": nn,
            "null_pct": round(100.0 * (n - nn) / n, 2),
            "distinct": distinct,
            "uniq_ratio": round(distinct / nn, 4) if nn else 0.0,
            "min": mn if ctype == "numeric" else None,
            "max": mx if ctype == "numeric" else None,
            "avg_len": round(avg_len, 1) if avg_len is not None else None,
            "padded_pct": round(100.0 * padded / ns, 1) if ns else 0.0,
            "top_values": list(top) if top else [],
        })
    return out
