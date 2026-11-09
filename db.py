"""DuckDB connection helpers and SQL quoting."""
from __future__ import annotations

import config


def q(name) -> str:
    """Quote an identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def lit(value) -> str:
    """Quote a string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def connect(path=None):
    import duckdb

    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.WORK_DIR / "tmp"
    tmp.mkdir(exist_ok=True)
    con = duckdb.connect(str(path or (config.WORK_DIR / "recon.duckdb")))
    con.execute(f"SET temp_directory={lit(tmp.as_posix())}")  # lets big joins spill to disk
    if config.DUCKDB_MEMORY_LIMIT:
        con.execute(f"SET memory_limit={lit(config.DUCKDB_MEMORY_LIMIT)}")
    if config.DUCKDB_THREADS:
        con.execute(f"SET threads={int(config.DUCKDB_THREADS)}")
    return con


def table_exists(con, table: str) -> bool:
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]).fetchone()[0] > 0


def columns_of(con, table: str, exclude=("__row",)) -> list:
    return [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall() if r[0] not in exclude]
