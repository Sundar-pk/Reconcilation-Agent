"""Generate a LEFT / RIGHT dataset pair with KNOWN injected differences, plus the expected results.

    python gen_test_data.py --rows 20000 --cols 100 --out testdata --xlsx

Writes  left.csv  right.csv  expected.json  (and pair.xlsx with sheets "Left" / "Right" when --xlsx).

What differs between the two sides (so any engine can be verified against expected.json):
  * right is shuffled and some columns are renamed ("invoice_no" -> "Invoice Number", ...)
  * every 100th left row is missing on the right        -> only_in_left
  * rows i % 1000 == 999 appear twice on the left        -> duplicate keys (2nd copy unmatched)
  * N/200 extra rows exist only on the right             -> only_in_right
  * every 80th right invoice number has a trailing space (key must still match)
  * amount_2 +0.01 (i%50==1), amount_5 +5 (i%70==2), status_3 lower-cased (i%40==3),
    status_6 blanked (i%60==5), date_4 +1 day (i%90==4)
"""
import argparse
import json
import time
from pathlib import Path

import duckdb


def _col_defs(cols: int):
    defs = [("invoice_no", "'INV' || lpad(CAST(i AS VARCHAR), 8, '0')"),
            ("customer_code", "lpad(CAST(i % 750 AS VARCHAR), 6, '0')")]  # repeats on purpose: NOT a key
    kinds = ["num", "txt", "date"]
    for k in range(2, cols):
        kind = kinds[(k - 2) % 3]
        if kind == "num":
            defs.append((f"amount_{k}", f"round(((i * {k} + 7) % 100003) / 7.0, 2)"))
        elif kind == "txt":
            defs.append((f"status_{k}", f"(['OPEN','CLOSED','PENDING','HOLD'])[(i * {k}) % 4 + 1]"))
        else:
            defs.append((f"date_{k}", f"DATE '2020-01-01' + CAST((i * {k}) % 1500 AS INTEGER)"))
    return defs


def generate(rows: int, cols: int, out_dir, xlsx: bool = False, log=print) -> dict:
    assert cols >= 7, "need at least 7 columns"
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    N, extra = rows, rows // 200
    defs = _col_defs(cols)
    gen_db = out / "gen.duckdb"
    gen_db.unlink(missing_ok=True)
    con = duckdb.connect(str(gen_db))  # disk-backed so 1M x 100 does not need all the RAM
    con.execute("SET memory_limit='3GB'")
    con.execute(f"SET temp_directory='{(out / 'gen_tmp').as_posix()}'")
    t0 = time.time()

    exprs = ", ".join(f"{e} AS {n}" for n, e in defs)
    con.execute(f"CREATE TABLE base AS SELECT i, {exprs} FROM range({N + extra}) t(i)")
    names = [n for n, _ in defs]

    # LEFT: rows 0..N-1, then the duplicated rows appended at the end
    con.execute(f"CREATE TABLE left_t AS SELECT {', '.join(names)} FROM base WHERE i < {N} ORDER BY i")
    con.execute(f"INSERT INTO left_t SELECT {', '.join(names)} FROM base WHERE i < {N} AND i % 1000 = 999 ORDER BY i")

    # RIGHT: modified, renamed, shuffled
    rename = {"invoice_no": "Invoice Number", "customer_code": "Customer Code", "amount_2": "Amt_2", "status_3": "Status 3"}
    mods = {
        "invoice_no": f"CASE WHEN i < {N} AND i % 80 = 7 THEN invoice_no || ' ' ELSE invoice_no END",
        "amount_2": f"CASE WHEN i < {N} AND i % 50 = 1 THEN round(amount_2 + 0.01, 2) ELSE amount_2 END",
        "amount_5": f"CASE WHEN i < {N} AND i % 70 = 2 THEN round(amount_5 + 5, 2) ELSE amount_5 END",
        "status_3": f"CASE WHEN i < {N} AND i % 40 = 3 THEN lower(status_3) ELSE status_3 END",
        "status_6": f"CASE WHEN i < {N} AND i % 60 = 5 THEN NULL ELSE status_6 END",
        "date_4": f"CASE WHEN i < {N} AND i % 90 = 4 THEN date_4 + 1 ELSE date_4 END",
    }
    sel = ", ".join(f"{mods.get(n, n)} AS \"{rename.get(n, n)}\"" for n in names)
    con.execute(f"CREATE TABLE right_t AS SELECT {sel} FROM base WHERE i % 100 <> 0 OR i >= {N} ORDER BY hash(i)")

    # independent oracle, straight from the generation formulas
    def one(sql):
        return con.execute(sql).fetchone()[0]

    m = f"i < {N} AND i % 100 <> 0"
    any_diff = "(i % 50 = 1 OR i % 70 = 2 OR i % 40 = 3 OR i % 60 = 5 OR i % 90 = 4)"
    r = lambda w: one(f"SELECT count(*) FROM range({N}) t(i) WHERE {w}")  # noqa: E731
    n_zero, n_dup, matched = r("i % 100 = 0"), r("i % 1000 = 999"), r(m)
    expected = {
        "rows": N, "cols": cols,
        "left_rows": N + n_dup, "right_rows": matched + extra, "matched": matched,
        "only_in_left": n_zero + n_dup, "only_in_right": extra,
        "left_duplicate_rows": n_dup, "left_duplicate_keys": n_dup, "right_duplicate_rows": 0,
        "rows_with_differences": r(f"{m} AND {any_diff}"),
        "identical_rows": matched - r(f"{m} AND {any_diff}"),
        "mismatches": {"amount_2": r(f"{m} AND i % 50 = 1"), "amount_5": r(f"{m} AND i % 70 = 2"),
                       "status_3": r(f"{m} AND i % 40 = 3"), "status_6": r(f"{m} AND i % 60 = 5"),
                       "date_4": r(f"{m} AND i % 90 = 4")},
    }
    (out / "expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")

    con.execute(f"COPY left_t TO '{(out / 'left.csv').as_posix()}' (HEADER, DELIMITER ',')")
    con.execute(f"COPY right_t TO '{(out / 'right.csv').as_posix()}' (HEADER, DELIMITER ',')")
    log(f"  CSV written in {time.time() - t0:.1f}s")

    if xlsx:
        import xlsxwriter

        t1 = time.time()
        wb = xlsxwriter.Workbook(str(out / "pair.xlsx"), {"constant_memory": True, "default_date_format": "yyyy-mm-dd",
                                                          "strings_to_formulas": False})
        for sheet, table in (("Left", "left_t"), ("Right", "right_t")):
            ws = wb.add_worksheet(sheet)
            cur = con.execute(f"SELECT * FROM {table}")
            ws.write_row(0, 0, [d[0] for d in cur.description])
            rno = 1
            while True:
                batch = cur.fetchmany(20000)
                if not batch:
                    break
                for row in batch:
                    ws.write_row(rno, 0, row)
                    rno += 1
        wb.close()
        log(f"  XLSX written in {time.time() - t1:.1f}s")
    con.close()
    gen_db.unlink(missing_ok=True)
    return expected


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--cols", type=int, default=100)
    ap.add_argument("--out", default="testdata")
    ap.add_argument("--xlsx", action="store_true", help="also write pair.xlsx (sheets Left / Right)")
    a = ap.parse_args()
    exp = generate(a.rows, a.cols, a.out, a.xlsx)
    print(json.dumps({k: v for k, v in exp.items() if k != "mismatches"}, indent=2))
