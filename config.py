"""Settings, read from environment variables / .env."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


# ---- Internal LLM (Bearer key over HTTP) ----
LLM_URL = os.getenv("INTERNAL_LLM_URL", "")
LLM_KEY = os.getenv("INTERNAL_LLM_KEY", "")
LLM_MODEL = os.getenv("INTERNAL_LLM_MODEL", "")
LLM_MODE = os.getenv("INTERNAL_LLM_MODE", "openai").lower()  # openai | custom
LLM_VERIFY_SSL = _bool("INTERNAL_LLM_VERIFY_SSL", True)  # set false to skip TLS verification
LLM_CA_BUNDLE = os.getenv("INTERNAL_LLM_CA_BUNDLE", "")  # preferred over disabling verification
LLM_TIMEOUT = _int("INTERNAL_LLM_TIMEOUT", 180)

# What the LLM may see:  stats_only  = column names + statistics, never cell values
#                        masked_samples = also a few masked example values / value changes
LLM_DATA_MODE = os.getenv("LLM_DATA_MODE", "stats_only").lower()

# ---- Paths ----
WORK_DIR = Path(os.getenv("WORK_DIR", "work"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output"))

# ---- Ingest ----
EXCEL_READER = os.getenv("EXCEL_READER", "auto").lower()  # auto | calamine (fast, RAM-hungry) | openpyxl (slow, low memory)
CHUNK_ROWS = _int("CHUNK_ROWS", 50_000)

# ---- DuckDB ----
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "")  # e.g. 8GB (empty = DuckDB default, 80% of RAM)
DUCKDB_THREADS = _int("DUCKDB_THREADS", 0)  # 0 = all cores

# ---- Reconciliation ----
EXPORT_FORMAT = os.getenv("EXPORT_FORMAT", "csv").lower()  # csv | parquet
MAX_DETAIL_PER_COLUMN = _int("MAX_DETAIL_PER_COLUMN", 100_000)  # cap on exported mismatch rows per column
BATCH_COLUMNS = _int("BATCH_COLUMNS", 20)  # columns compared per pass (bounds memory)
MAX_HINT_COLUMNS = _int("MAX_HINT_COLUMNS", 40)  # columns that get pattern analysis
REPORT_SAMPLE_ROWS = _int("REPORT_SAMPLE_ROWS", 1000)  # rows per detail sheet in the Excel report
