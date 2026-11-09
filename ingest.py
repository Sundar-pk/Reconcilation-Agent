"""Load an Excel sheet / CSV / Parquet file into a DuckDB table.

Design choices that matter at 1M rows x 100 columns:
  * Excel is read with calamine (Rust) and written to DuckDB in chunks, never as one big object.
  * Every column is stored as VARCHAR in canonical text form (1.0 -> "1", dates -> ISO). Typing is
    applied at comparison time, which avoids failures on mixed-type Excel columns and keeps
    identifiers like "00123" intact.
  * Blank / whitespace-only cells become NULL. Fully empty rows are dropped.
  * A `__row` column holds the original Excel row number, so every finding can be traced back.
  * Loaded tables are cached (by file path, size, mtime, sheet), so re-runs skip the slow step.
"""
from __future__ import annotations

import datetime as dt
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import config
from db import columns_of, lit, q, table_exists

CSV_EXT = {".csv", ".tsv", ".txt"}
PARQUET_EXT = {".parquet"}
EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".xlsb"}


# ----------------------------------------------------------------------------- sheets
def list_sheets(path) -> list:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in CSV_EXT or ext in PARQUET_EXT:
        return ["(file)"]
    if ext in (".xlsx", ".xlsm"):  # cheap: read workbook.xml only, not the whole file
        try:
            with zipfile.ZipFile(p) as zf:
                root = ET.fromstring(zf.read("xl/workbook.xml"))
            names = [el.get("name") for el in root.iter() if el.tag.endswith("}sheet")]
            if names:
                return names
        except Exception:
            pass
    from python_calamine import CalamineWorkbook

    return list(CalamineWorkbook.from_path(str(p)).sheet_names)


# ----------------------------------------------------------------------------- rendering
def _r_str(v):
    return v if (v and not v.isspace()) else None


def _r_float(v):
    if v != v or v in (float("inf"), float("-inf")):
        return None
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def _r_datetime(v):
    if v.hour or v.minute or v.second or v.microsecond:
        return v.isoformat(sep=" ")
    return v.date().isoformat()


_RENDER = {
    str: _r_str,
    float: _r_float,
    int: str,
    bool: lambda v: "TRUE" if v else "FALSE",
    dt.datetime: _r_datetime,
    dt.date: lambda v: v.isoformat(),
    dt.time: lambda v: v.isoformat(),
    dt.timedelta: str,
}


def render(v):
    if v is None:
        return None
    return _RENDER.get(type(v), str)(v)


def dedupe_names(raw) -> list:
    out, used = [], set()
    for i, n in enumerate(raw):
        name = str(n).strip() if n not in (None, "") else ""
        if not name:
            name = f"col_{i + 1}"
        if name.lower() == "__row":
            name = "row_"
        base, k = name, 1
        while name.lower() in used:
            k += 1
            name = f"{base}_{k}"
        used.add(name.lower())
        out.append(name)
    return out


# ----------------------------------------------------------------------------- registry / cache
def _ensure_registry(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS _sources (tbl VARCHAR PRIMARY KEY, fingerprint VARCHAR, path VARCHAR, "
        "sheet VARCHAR, header_row INTEGER, rows BIGINT, cols INTEGER, seconds DOUBLE, dropped_empty BIGINT)"
    )


def _fingerprint(path: Path, sheet, header_row) -> str:
    st = path.stat()
    return f"{path.resolve()}|{sheet}|{header_row}|{st.st_size}|{int(st.st_mtime)}"


def get_info(con, table: str, cached: bool = False) -> dict:
    _ensure_registry(con)
    row = con.execute(
        "SELECT path, sheet, header_row, rows, cols, seconds, dropped_empty FROM _sources WHERE tbl = ?", [table]
    ).fetchone()
    if not row:
        raise KeyError(f"{table} has not been loaded")
    return {
        "table": table, "path": row[0], "sheet": row[1], "header_row": row[2], "rows": row[3],
        "columns": columns_of(con, table), "seconds": row[5], "dropped_empty_rows": row[6], "cached": cached,
    }


# ----------------------------------------------------------------------------- main entry
def ingest(con, path, sheet, table, header_row: int = 0, progress=print, reader: str | None = None) -> dict:
    """Load `path` (+ `sheet`) into `table`. header_row: 1-based Excel row, 0 = first non-empty row."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    _ensure_registry(con)
    ext = path.suffix.lower()
    if ext in CSV_EXT | PARQUET_EXT:
        sheet = "(file)"
    elif sheet is None:
        sheet = list_sheets(path)[0]

    fp = _fingerprint(path, sheet, header_row)
    cached = con.execute("SELECT fingerprint FROM _sources WHERE tbl = ?", [table]).fetchone()
    if cached and cached[0] == fp and table_exists(con, table):
        info = get_info(con, table, cached=True)
        progress(f"  {path.name} [{sheet}] -> {table}: {info['rows']:,} rows x {len(info['columns'])} cols (cached)")
        return info

    t0 = time.time()
    con.execute(f"DROP TABLE IF EXISTS {table}")
    dropped = 0
    if ext in CSV_EXT:
        _load_csv(con, path, table, header_row)
    elif ext in PARQUET_EXT:
        _load_parquet(con, path, table)
    elif ext in EXCEL_EXT:
        dropped = _load_excel(con, path, sheet, table, header_row, progress, reader or config.EXCEL_READER)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    ncols = len(columns_of(con, table))
    secs = round(time.time() - t0, 1)
    con.execute("DELETE FROM _sources WHERE tbl = ?", [table])
    con.execute(
        "INSERT INTO _sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [table, fp, str(path), sheet, header_row, rows, ncols, secs, dropped],
    )
    progress(f"  {path.name} [{sheet}] -> {table}: {rows:,} rows x {ncols} cols in {secs}s")
    return get_info(con, table)


# ----------------------------------------------------------------------------- CSV / Parquet
def _blank_to_null(col: str) -> str:
    c = q(col)
    return f"CASE WHEN trim({c}) = '' THEN NULL ELSE {c} END"


def _load_csv(con, path: Path, table: str, header_row: int):
    hdr = header_row if header_row and header_row > 0 else 1
    delim = ", delim='\\t'" if path.suffix.lower() == ".tsv" else ""
    rel = (f"read_csv({lit(path.as_posix())}, header=true, all_varchar=true, skip={hdr - 1}, "
           f"null_padding=true{delim})")
    raw = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
    names = dedupe_names(raw)
    sel = ", ".join(f"{_blank_to_null(r)} AS {q(n)}" for r, n in zip(raw, names))
    con.execute(
        f"CREATE TABLE {table} AS SELECT CAST(row_number() OVER () AS BIGINT) + {hdr} AS __row, {sel} FROM {rel}"
    )


def _load_parquet(con, path: Path, table: str):
    rel = f"read_parquet({lit(path.as_posix())})"
    raw = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
    names = dedupe_names(raw)
    sel = ", ".join(
        f"CASE WHEN trim(CAST({q(r)} AS VARCHAR)) = '' THEN NULL ELSE CAST({q(r)} AS VARCHAR) END AS {q(n)}"
        for r, n in zip(raw, names)
    )
    con.execute(f"CREATE TABLE {table} AS SELECT CAST(row_number() OVER () AS BIGINT) AS __row, {sel} FROM {rel}")


# ----------------------------------------------------------------------------- Excel
CALAMINE_BYTES_PER_CELL = 70  # measured ~60 B/cell on typical data; rounded up


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def sheet_cells(path: Path, sheet: str):
    """Rows x columns of an .xlsx sheet from its <dimension> tag (cheap: reads only the start of the sheet XML)."""
    import re

    try:
        with zipfile.ZipFile(path) as zf:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rid_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            rid = next(el.get(rid_key) for el in wb.iter() if el.tag.endswith("}sheet") and el.get("name") == sheet)
            target = next(r.get("Target") for r in rels if r.get("Id") == rid).lstrip("/")
            target = target if target.startswith("xl/") else "xl/" + target
            with zf.open(target) as f:
                head = f.read(8192).decode("utf-8", "ignore")
        m = re.search(r'<dimension ref="([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?"', head)
        if not m:
            return None
        c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4) or m.group(2))
        return (r2 - r1 + 1) * (_col_number(c2) - _col_number(c1) + 1)
    except Exception:
        return None


def available_ram_bytes():
    try:
        with open("/proc/meminfo") as fh:  # Linux
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:  # Windows
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                       [(n, ctypes.c_ulonglong) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile",
                                                          "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual",
                                                          "ullAvailExtendedVirtual")]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return int(st.ullAvailPhys)
    except Exception:
        return None


def choose_reader(path: Path, sheet: str, reader: str, progress=print) -> str:
    """'auto' = calamine (fast) unless the sheet is too big for the free RAM, then openpyxl (slow, flat memory)."""
    if reader != "auto":
        return reader
    cells = sheet_cells(path, sheet) if path.suffix.lower() in (".xlsx", ".xlsm") else None
    avail = available_ram_bytes()
    if cells and avail and cells * CALAMINE_BYTES_PER_CELL > 0.6 * avail:
        progress(f"  Sheet '{sheet}' has about {cells / 1e6:.0f}M cells: the fast Excel reader would need roughly "
                 f"{cells * CALAMINE_BYTES_PER_CELL / 1e9:.1f} GB of the {avail / 1e9:.1f} GB free RAM. "
                 f"Using the low-memory reader instead (several times slower). Saving the sheet as CSV is much faster.")
        return "openpyxl"
    return "calamine"


def _row_iterator(path: Path, sheet: str, reader: str):
    """Yield rows (sequences of raw cell values). The n-th row yielded is Excel row n."""
    if reader == "calamine":
        try:
            from python_calamine import CalamineWorkbook
        except ImportError:
            reader = "openpyxl"
        else:
            ws = CalamineWorkbook.from_path(str(path)).get_sheet_by_name(sheet)
            return ws.iter_rows()
    if reader == "openpyxl":
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        ws = wb[sheet]
        if hasattr(ws, "reset_dimensions"):
            ws.reset_dimensions()  # do not trust a wrong <dimension> tag
        return ws.iter_rows(values_only=True)
    raise ValueError(f"Unknown EXCEL_READER: {reader} (use auto, calamine or openpyxl)")


def _load_excel(con, path: Path, sheet: str, table: str, header_row: int, progress, reader: str) -> int:
    import pyarrow as pa

    reader = choose_reader(path, sheet, reader, progress)
    it = iter(_row_iterator(path, sheet, reader))
    rownum, header = 0, None
    for row in it:
        rownum += 1
        if header_row and rownum < header_row:
            continue
        vals = [render(v) for v in row]
        if header_row == 0 and not any(v is not None for v in vals):
            continue  # auto-detect: skip leading blank rows
        header = vals
        break
    if header is None:
        raise ValueError(f"Sheet '{sheet}' in {path.name} is empty")

    names = dedupe_names(header)
    width = len(names)
    con.execute(f"CREATE TABLE {table} (__row BIGINT, " + ", ".join(f"{q(n)} VARCHAR" for n in names) + ")")
    nonblank = " OR ".join(f"{q(n)} IS NOT NULL" for n in names)

    state = {"loaded": 0, "dropped": 0, "next_report": 200_000}

    def flush(chunk, first_row):
        if len({len(r) for r in chunk}) != 1 or len(chunk[0]) != width:  # ragged rows: pad / trim
            chunk = [(list(r) + [None] * width)[:width] for r in chunk]
        arrays = [pa.array(list(map(render, col)), type=pa.string()) for col in zip(*chunk)]
        ids = pa.array(range(first_row, first_row + len(chunk)), type=pa.int64())
        tbl = pa.Table.from_arrays([ids] + arrays, names=["__row"] + names)
        con.register("_chunk", tbl)
        try:
            before = con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]
            con.execute(f"INSERT INTO {table} SELECT * FROM _chunk WHERE {nonblank}").fetchall()
            added = con.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0] - before
        finally:
            con.unregister("_chunk")
        state["dropped"] += len(chunk) - added
        state["loaded"] += len(chunk)
        if state["loaded"] >= state["next_report"]:
            progress(f"    ... {state['loaded']:,} rows read")
            state["next_report"] += 200_000

    chunk, first = [], rownum + 1
    for row in it:
        rownum += 1
        chunk.append(row)
        if len(chunk) >= config.CHUNK_ROWS:
            flush(chunk, first)
            chunk, first = [], rownum + 1
    if chunk:
        flush(chunk, first)
    return state["dropped"]
