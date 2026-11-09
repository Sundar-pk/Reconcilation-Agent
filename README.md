# Reconcilation-Agent
A Sample Reconciliation agent to perform comparison of 2 excel files with minimum 1 million data volume using Lang chain and Duck DB 

Compares **any two Excel files, two sheets of one workbook, or CSV / Parquet files**, built for
**1M+ rows x 100+ columns**. A LangChain agent (your internally hosted Qwen / gpt-oss-120b) plans
and explains. A DuckDB engine does the actual comparison.

> **The LLM never reads your data.** At this size it cannot, and it would get the arithmetic wrong.
> It sees column names and statistics only (never cell values, unless you opt in).
> Every number in the output is computed by SQL.

## How it works

```
Excel / CSV / Parquet                                       Excel report + CSV detail files
        │                                                              ▲
   ingest (calamine → DuckDB, chunked)                                 │
        │                                                      explain (LLM, statistics only)
   profile  (types, nulls, distincts)                                  ▲
        │                                                              │
   plan = key columns + column mapping + compare rules ──► YOU APPROVE ──► engine (DuckDB SQL)
        ▲                                                       full outer join on the key,
   heuristics (verified on the data) + LLM review               per-column diff in batches
```

* **Plan**: the agent finds the key (checks uniqueness and overlap on the real data, tries
  `upper` / `strip_zeros` / `numeric` variants and composite keys), maps columns even when names
  differ (`Invoice No` ≈ `InvoiceNumber`), and picks a rule per column. You review it before anything runs.
* **Rules** per column: `exact`, `text` (trim, case-insensitive), `numeric` (+ tolerance),
  `date`, `datetime`, `ignore`.
* **Results**: matched, only-in-left, only-in-right, blank keys, duplicate keys (paired in file
  order), identical vs differing rows, per-column mismatches, numeric totals (left vs right),
  and pattern hints ("all 12,000 differences are within 0.01", "differs only by letter case").
* **Source row numbers** are kept, so every finding points back to the Excel row.

## Setup (Windows)

```
cd D:\Reconcilation-Agent
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # fill in INTERNAL_LLM_URL / KEY / MODEL
python check_llm.py           # confirms your endpoint, Bearer key and JSON replies work
```

`duckdb` is just a Python package (no server, no admin rights). Everything runs locally.

## Use it

```
python main.py sheets --left book.xlsx                       # list sheets

# two sheets of one workbook (the first two sheets by default)
python main.py reconcile --left book.xlsx --left-sheet Jan --right-sheet Feb

# two files
python main.py reconcile --left ledger.xlsx --right bank.xlsx

# steer the plan
python main.py reconcile --left a.xlsx --right b.xlsx ^
    --key "InvoiceNo=Invoice Number:upper" ^
    --rule "Amount=numeric:0.01" --rule "Status=text" --ignore "Last Modified"

# review / edit first, run later
python main.py plan --left a.xlsx --right b.xlsx             # writes output\run_...\plan.json
python main.py run  --plan output\run_...\plan.json

# interactive agent (needs a tool-calling model)
python main.py chat --left a.xlsx --right b.xlsx
```

Each run writes to `output\run_<timestamp>\`:

| File | Content |
|---|---|
| `reconciliation_report.xlsx` | Summary, narrative, per-column results, capped samples, the plan |
| `value_mismatches.csv` | **Every** differing cell: key, source rows, column, left value, right value, numeric diff |
| `only_in_left.csv`, `only_in_right.csv` | Full unmatched rows with source row number and reason |
| `duplicate_keys_*.csv` | Keys that occur more than once |
| `summary.json`, `plan.json`, `narrative.md` | Machine-readable results, the approved plan, the explanation |

Excel cannot hold more than 1,048,576 rows, so the workbook holds the first 1,000 rows per detail
sheet and the CSV files hold everything (use `EXPORT_FORMAT=parquet` for smaller, faster files).

## Your environment

* **Bearer key + SSL**: the key is sent as `Authorization: Bearer <key>`. `INTERNAL_LLM_VERIFY_SSL=false`
  skips certificate verification, as requested. That also removes protection for the key against
  interception, so please ask for the internal CA file and set `INTERNAL_LLM_CA_BUNDLE` instead when you can.
* **Guardrails (monkey patches)**: set `GUARDRAILS_MODULE` in `.env`. It is imported before any
  LangChain / OpenAI import. With verification off, the client is built with a custom `httpx.Client`
  (needed to turn verification off); with it on and no CA bundle, the default client path is untouched.
  Check that your patches still apply on the path you use (`python check_llm.py`).
* **Corporate proxy**: with verification off (or a CA bundle) the client is a plain `httpx.Client`, which honours
  `HTTPS_PROXY` / `NO_PROXY`. If you leave verification on with no bundle, `langchain-openai` builds its own
  transport, which in recent versions ignores proxy environment variables; set `INTERNAL_LLM_CA_BUNDLE` to
  avoid that. An internal LLM usually needs no proxy.
* **Not OpenAI-compatible?** Set `INTERNAL_LLM_MODE=custom` and adapt two small methods in `llm.py`.
  `reconcile` and `plan` work in that mode; the tool-calling `chat` does not.
* **No LLM at all?** Add `--no-llm`: you still get the heuristic plan, every number and the Excel report.
* **Privacy**: `LLM_DATA_MODE=stats_only` (default) sends column names and statistics, never cell values.
  `masked_samples` also sends a few masked examples. Raw data never leaves the machine in either mode.
  Nothing is written outside `work\` and `output\`.

## Performance and memory

Measured on generated data with known differences, on a 2-core / 8 GB cloud machine (see "Tests").
Your machine will differ, but the shape of the numbers will not.

| Input (per side) | Load into DuckDB | Plan | Compare + exports | Peak RAM |
|---|---|---|---|---|
| **1,000,000 rows x 100 cols, CSV** | ~27 s per file | ~40 s | **15 s** (every count exact) | 1.9 GB |
| 250,000 x 100, Excel (25M cells), two sheets of one workbook | ~39 s per sheet | ~14 s | 5 s (every count exact) | 2.5 GB |
| 1,000,000 x 100, Excel (100M cells) | *~2.7 min per sheet (extrapolated)* | | | *~6-7 GB (extrapolated)* |

The 1M x 100 Excel row is extrapolated from the 250k run (cost is linear in cells); it was not run end to end.

* **Excel is the slow part, not the comparison.** The fast reader (calamine) does ~600k cells/s but holds the
  whole sheet in memory (~60 bytes per cell). `EXCEL_READER=auto` (default) checks the sheet size and your free RAM
  and switches to the low-memory reader (openpyxl: ~130k cells/s, flat ~350 MB, so ~13 min per 1M x 100 sheet)
  when the fast one would not fit, and tells you so. Both readers produce identical data (cross-checked).
  **Saving the sheet as CSV first is the fastest option by far** (~26 s for 1M x 100).
* Loaded data is cached in `work\` (keyed on file path, size, modified time and sheet), so re-runs, plan edits
  and `run --plan` skip the load.
* DuckDB uses ~80% of RAM by default and spills to `work\tmp` beyond that. Set `DUCKDB_MEMORY_LIMIT`
  (for example `8GB`) to cap it, and lower `BATCH_COLUMNS` to reduce peak memory further.
* `work\` can grow to a few GB. `python main.py clean` drops intermediate tables;
  `python main.py clean --all` removes everything cached.

## Tests

```
python test_engine.py                  # 20k x 100: CSV and Excel (two sheets), exact counts vs an independent oracle
python test_engine.py --rows 1000000 --quick     # full scale
python test_flow.py                    # LLM-assisted planning (scripted model), privacy modes, approval gate, CLI
python test_llm_http.py                # Bearer header + self-signed cert with verification off (needs openssl)
```

`gen_test_data.py` builds a left/right pair with known injected differences (renamed columns,
shuffled rows, missing rows, extra rows, duplicate keys, rounding differences, case differences,
blanked cells, shifted dates, trailing spaces in the key) and an `expected.json` computed independently.

## Limits you should know about

* **Matching is key-based.** A wrong key gives a wrong reconciliation, which is why the plan needs
  your approval. Without a key, rows are matched by position (a warning is shown).
* Duplicate keys are paired in file order (1st with 1st, 2nd with 2nd); extra occurrences show up as unmatched.
* All values are compared as text/number/date after normalisation: `1`, `1.0` and `"1"` are equal
  under the `numeric` rule but not under `exact`. Blank and whitespace-only cells count as empty.
* Formulas are read as their cached values. Merged cells, multiple header rows and pivot layouts
  need `--header-row` or a cleaned sheet.
* Old `.xls` files work through calamine; password-protected workbooks must be unlocked first.
* The numeric rule compares as 64-bit floats (15 significant digits), fine for money at normal sizes.
