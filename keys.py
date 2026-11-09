"""Join-key normalisation and key quality evaluation (all in DuckDB SQL)."""
from __future__ import annotations

from db import q

VALID_NORMALIZERS = ("upper", "strip_zeros", "numeric")


def norm_key_sql(col_sql: str, norms) -> str:
    """SQL for one key component. Always trims, blanks -> NULL; optional extra normalisers."""
    e = f"NULLIF(trim({col_sql}), '')"
    for n in norms or []:
        if n == "upper":
            e = f"upper({e})"
        elif n == "strip_zeros":  # "00123" -> "123", "000" -> "0"
            e = f"COALESCE(NULLIF(ltrim({e}, '0'), ''), CASE WHEN {e} IS NOT NULL THEN '0' END)"
        elif n == "numeric":  # "1", "1.0", "1.00" -> same canonical text
            e = f"COALESCE(CAST(TRY_CAST({e} AS DECIMAL(38,6)) AS VARCHAR), {e})"
        else:
            raise ValueError(f"Unknown key normaliser '{n}' (valid: {', '.join(VALID_NORMALIZERS)})")
    return e


def key_exprs(keys: list, side: str) -> list:
    return [norm_key_sql(q(k[side]), k.get("normalize")) for k in keys]


def _combined(exprs: list) -> str:
    if len(exprs) == 1:
        return "k0"
    return "concat_ws(chr(31), " + ", ".join(f"k{i}" for i in range(len(exprs))) + ")"


def _side_sql(table: str, exprs: list) -> str:
    inner = ", ".join(f"{e} AS k{i}" for i, e in enumerate(exprs))
    notnull = " AND ".join(f"k{i} IS NOT NULL" for i in range(len(exprs)))
    return f"SELECT {_combined(exprs)} AS k FROM (SELECT {inner} FROM {table}) WHERE {notnull}"


def evaluate_key(con, left_table: str, right_table: str, keys: list) -> dict:
    """How good is this key? Uniqueness on each side and how many key values appear on both sides."""
    ls = _side_sql(left_table, key_exprs(keys, "left"))
    rs = _side_sql(right_table, key_exprs(keys, "right"))
    row = con.execute(
        f"""WITH l AS ({ls}), r AS ({rs}),
                 ld AS (SELECT DISTINCT k FROM l), rd AS (SELECT DISTINCT k FROM r)
            SELECT (SELECT count(*) FROM l), (SELECT count(*) FROM ld),
                   (SELECT count(*) FROM r), (SELECT count(*) FROM rd),
                   (SELECT count(*) FROM ld SEMI JOIN rd USING (k))"""
    ).fetchone()
    l_keyed, l_dist, r_keyed, r_dist, common = row
    l_total = con.execute(f"SELECT count(*) FROM {left_table}").fetchone()[0]
    r_total = con.execute(f"SELECT count(*) FROM {right_table}").fetchone()[0]
    uniq_l = l_dist / l_keyed if l_keyed else 0.0
    uniq_r = r_dist / r_keyed if r_keyed else 0.0
    overlap = common / min(l_dist, r_dist) if min(l_dist, r_dist) else 0.0
    return {
        "left_rows": l_total, "right_rows": r_total,
        "left_blank_keys": l_total - l_keyed, "right_blank_keys": r_total - r_keyed,
        "left_duplicate_rows": l_keyed - l_dist, "right_duplicate_rows": r_keyed - r_dist,
        "uniq_left": round(uniq_l, 5), "uniq_right": round(uniq_r, 5),
        "common_keys": common, "overlap": round(overlap, 5),
        "score": round(min(uniq_l, uniq_r) * overlap, 5),
    }
