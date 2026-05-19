# dbcli — v1 Implementation-Ready Specification

> Project name is a placeholder; rename before publishing.

**Status:** trimmed v1 spec
**Last updated:** 2026-05-19
**Owner:** Isaac / Hermes
**Goal:** A project-based CLI for turning CSV / XLSX files into MySQL tables with replayable edits, safe validation, CI-friendly behavior, and auditable run history.

---

## 1. Product Summary

`dbcli` is a small, boring data-loading CLI.

It does one job well:

1. Read one spreadsheet-like source file.
2. Apply a declared edit pipeline.
3. Validate the result against a declared schema.
4. Create or append to a MySQL table (or atomically replace it).
5. Write a reproducible run record.

The design bias is **recipe-first, explicit, replayable, CI-safe**.

This is not a general ETL platform. v1 intentionally avoids joins, aggregations, derived columns, multiple sources, streaming, upserts, and non-MySQL targets.

---

## 2. Goals and Non-Goals

### 2.1 In Scope for v1

- Inspect and profile CSV / XLSX files.
- Generate starter recipes from source files.
- Use declarative YAML recipes as the source of truth.
- Support a small edit vocabulary for ugly-but-common spreadsheet cleanup.
- Validate schema, types, nullability, and string lengths before database writes.
- Create MySQL tables from declared recipes.
- Load data with two modes:
  - `append`
  - `replace` (staging + atomic rename)
- Run safely in CI with no prompts, structured logs, and predictable exit codes.
- Maintain per-project profiles, recipes, rejects, and run history.

### 2.2 Out of Scope for v1

- `upsert` and `truncate-load` modes (deferred to v1.1).
- A separate `plan` command (drift check is folded into `load`).
- File locks / concurrent-load coordination (callers serialize externally).
- Idempotency keys.
- `--continue-on-error` / batch bisection.
- XLS (legacy Excel binary). XLSX only.
- Schema migrations against existing tables beyond create / atomic replace.
- Non-MySQL targets.
- Joins, group-by, aggregations, window functions, derived columns.
- One-to-many or many-to-one source/table mappings.
- Files larger than roughly 100 MB.
- Streaming reads.
- `LOAD DATA LOCAL INFILE`.
- Runtime CSV encoding / delimiter auto-detection during `validate` or `load`.
- Interactive password prompts.

### 2.3 Design Guardrails

- Recipes are checked into git.
- Secrets are never written literally to disk.
- Validation must not touch the database.
- Loading writes run history for both success and failure.
- Row-level failures go to a rejects CSV.
- Schema/recipe/system failures go to structured diagnostics, not fake row rejects.

---

## 3. Architecture

### 3.1 Tech Stack

- Language: Python 3.11+
- CLI: Typer
- Dataframe engine: Polars
- Excel reader: `python-calamine`
- Database access: SQLAlchemy Core + PyMySQL
- Project config: TOML
- Recipes: YAML
- Distribution: pipx-installable Python package
- Entry point: `dbcli`

### 3.2 Data Model

v1 uses an in-memory dataframe model.

- Every source file is fully loaded into memory.
- Polars eager dataframes are acceptable for v1.
- Recommended source-file size limit: ~100 MB.

### 3.3 Lossless-First Ingestion Rule

This is a load-bearing rule.

`dbcli` must avoid losing spreadsheet semantics before the recipe has a chance to cast.

**CSV** — read all columns as strings by default. Preserve leading zeros, date-looking strings, and SKU/account identifiers. Apply `parse_null` and `cast` explicitly from the recipe.

**XLSX** — read native cell values through `python-calamine`. Normalize them into a dataframe without silently coercing identifiers into lossy types. Date cells may be represented as native date/datetime values internally, but recipe `cast` remains the authoritative conversion step for target schema.

Only `cast` is trusted to convert source values into target semantic types before database loading.

---

## 4. Project Layout

A project is any directory containing `.dbcli/`.

```text
my-project/
├── .dbcli/
│   ├── config.toml
│   ├── profiles.toml
│   ├── profiles.example.toml
│   ├── recipes/
│   │   ├── sellers.yaml
│   │   └── orders.yaml
│   ├── rejects/
│   └── runs.jsonl
└── data/
```

### 4.1 Git Policy

Recommended `.gitignore`:

```gitignore
.dbcli/profiles.toml
.dbcli/rejects/
.dbcli/runs.jsonl
```

Recommended committed files:

```text
.dbcli/config.toml
.dbcli/profiles.example.toml
.dbcli/recipes/*.yaml
```

`profiles.toml` is ignored by default. Teams that want to commit profile files may do so only if every secret uses environment-variable interpolation. Literal secrets are always a lint error.

---

## 5. Project Config

`.dbcli/config.toml` defines project defaults.

```toml
[defaults]
profile = "dev"
batch_size = 5000
reject_threshold = 0.0
charset = "utf8mb4"
collation = "utf8mb4_unicode_ci"
engine = "InnoDB"

[ci]
no_progress = true
```

Rules:

- Recipe values override project defaults.
- CLI flags override recipe values.
- Missing values fall back to documented defaults.

---

## 6. Connection Profiles

`.dbcli/profiles.toml` stores local connection profiles.

```toml
[dev]
host = "localhost"
port = 3306
user = "dbcli"
password = "${DBCLI_DEV_PW}"
database = "ecom_dev"
```

### 6.1 Profile Rules

- Passwords must use `${ENV_VAR}` interpolation.
- Literal passwords are a lint error.
- The CLI never writes literal passwords to disk.
- No keyring fallback in v1.
- No interactive password prompt.
- If an interpolated env var is missing, profile resolution fails with exit code `30`.

### 6.2 Profile Test

`dbcli profile test <name>`:

- Resolves environment variables.
- Connects to MySQL and runs `SELECT 1`.
- Exits `0` on success, `30` on failure.

---

## 7. Recipes

A recipe is the single declarative artifact tying together source, target, schema, and edits.

One recipe maps exactly:

```text
one source file -> one target table
```

### 7.1 Example Recipe

```yaml
name: sellers

source:
  path: data/sellers_2024Q4.xlsx
  sheet: "EU Sellers"
  header_row: 1
  skip_rows: 0

target:
  profile: dev
  table: dim_sellers
  mode: append
  charset: utf8mb4
  collation: utf8mb4_unicode_ci
  engine: InnoDB

schema:
  - {name: seller_id, type: BIGINT, nullable: false}
  - {name: tier, type: VARCHAR(16), nullable: false}
  - {name: first_active_date, type: DATE, nullable: true}
  - {name: am_email, type: VARCHAR(255), nullable: true}

edits:
  - rename: {SellerID: seller_id, Tier: tier, "AM Email": am_email}
  - drop: [internal_notes, scratch]
  - trim: [tier, am_email]
  - parse_null: {columns: [am_email], values: ["", "N/A", "-"]}
  - cast: {first_active_date: {type: date, format: "%m/%d/%Y"}}
  - fill_null: {tier: "UNKNOWN"}

options:
  reject_threshold: 0.01
  batch_size: 5000
```

### 7.2 Source Rules

- `source.path` is required. Relative paths resolve from the project root. Absolute paths produce a portability warning.
- `source.sheet` is required for XLSX with more than one sheet; ignored for CSV.
- `source.encoding` and `source.delimiter` apply only to CSV.
- CSV recipes must declare both `source.encoding` and `source.delimiter`.
- `inspect` and `scan` may detect CSV encoding / delimiter, but `validate` and `load` never auto-detect them at runtime.
- `source.header_row` is 1-indexed and defaults to `1`.
- `source.skip_rows` defaults to `0` (rows skipped before header handling).

### 7.2.1 Source Edge Cases

- `skip_rows` is applied first. `header_row` is then interpreted relative to the remaining file.
- `rows_read` counts data rows after skipped rows and header handling.
- Reject `__row_index` values are 1-indexed data-row positions after skipped rows and header handling.
- Duplicate headers are a source validation error (`source.duplicate_header`, exit `10`).
- Empty headers are a source validation error (`source.empty_header`, exit `10`).
- Empty files and files without a readable header are source validation errors (`source.empty`, exit `10`).
- Header-only files are valid; `rows_read` is `0`.
- Fully blank rows after the header are treated as data rows, not silently dropped.
- If `scan` cannot confidently detect CSV encoding or delimiter, it fails with a structured diagnostic and the user must rerun with explicit flags.

### 7.3 Target Rules

- `target.profile` — profile name from `.dbcli/profiles.toml`. May be overridden by CLI flag.
- `target.table` — required. Must be a safe MySQL identifier or dotted schema/table.
- `target.mode` — required. One of `append`, `replace`.
- `target.charset`, `target.collation`, `target.engine` — optional. Defaults come from project config or built-in defaults.

### 7.4 Schema Rules

The `schema` block is the source of truth.

- Column order in `schema` defines MySQL table column order and INSERT column order.
- Every final dataframe column after edits must appear in `schema`.
- Every schema column must appear in the final dataframe.
- Extra columns or missing columns are validation errors.
- Duplicate column names are a validation error.

### 7.5 Supported MySQL Types

v1 supports a conservative type subset:

- `BOOLEAN`, `TINYINT`, `SMALLINT`, `INT`, `BIGINT`
- `DECIMAL(p,s)`, `FLOAT`, `DOUBLE`
- `VARCHAR(n)`, `TEXT`
- `DATE`, `DATETIME`

Unsupported types fail validation with exit code `10`.

### 7.6 Nullability Defaults

- If `nullable` is omitted, default is `true`.
- If `nullable: false`, rows with null final values become row-level validation rejects.

---

## 8. Edit Operations

Edits are applied in declared order. v1 supports six ops:

| Op | Purpose | Example |
|---|---|---|
| `rename` | Map old column names to new names | `rename: {SellerID: seller_id}` |
| `drop` | Remove columns | `drop: [scratch, notes]` |
| `trim` | Strip leading/trailing whitespace | `trim: [name, email]` |
| `parse_null` | Treat listed strings as NULL | `parse_null: {columns: [x], values: ["", "N/A"]}` |
| `cast` | Convert type | `cast: {dt: {type: date, format: "%Y%m%d"}}` |
| `fill_null` | Replace NULLs with constants | `fill_null: {tier: "UNKNOWN"}` |

### 8.1 `rename`

```yaml
- rename: {SellerID: seller_id, "AM Email": am_email}
```

- Source columns must exist before rename.
- Target names must not collide with existing columns unless replacing the same column.
- Duplicate target names are invalid.

### 8.2 `drop`

```yaml
- drop: [internal_notes, scratch]
```

Listed columns must exist at the time `drop` runs.

### 8.3 `trim`

```yaml
- trim: [name, email]
```

- Operates on string-like values.
- Nulls remain null.

### 8.4 `parse_null`

```yaml
- parse_null: {columns: [am_email], values: ["", "N/A", "-"]}
```

- Values are matched after prior edits.
- Matching is exact literal matching. Empty string `""` is supported.
- `parse_null` does not trim by itself — use `trim` first if needed.

### 8.5 `cast`

```yaml
- cast:
    first_active_date: {type: date, format: "%m/%d/%Y"}
    seller_id: {type: int}
    revenue: {type: decimal, precision: 12, scale: 2}
```

Allowed cast types: `string`, `boolean`, `int`, `float`, `decimal`, `date`, `datetime`.

- Cast failures are row-level edit rejects.
- Date/datetime formats use Python `strptime` format strings.
- Decimal casts may include `precision` and `scale`.
- Decimal parsing accepts `.` as the decimal separator. Thousands separators are not accepted in v1.
- Boolean parsing accepts `true`, `false`, `1`, `0`, `yes`, `no`, `y`, `n` case-insensitively.
- Datetime values are treated as timezone-naive. v1 does not perform timezone conversion.
- Casts must map cleanly to declared schema types:

| Cast type | Compatible schema types |
|---|---|
| `string` | `VARCHAR(n)`, `TEXT` |
| `boolean` | `BOOLEAN` |
| `int` | `TINYINT`, `SMALLINT`, `INT`, `BIGINT` |
| `float` | `FLOAT`, `DOUBLE` |
| `decimal` | `DECIMAL(p,s)` |
| `date` | `DATE` |
| `datetime` | `DATETIME` |

### 8.6 `fill_null`

```yaml
- fill_null: {tier: "UNKNOWN"}
```

- Column must exist.
- Fill value must be compatible with the final schema type after casting.

### 8.7 Explicitly Excluded Edits

v1 does not support: `select`, `normalize_case`, `replace`, `filter`, `dedupe_on`, joins, group-by, aggregations, window functions, computed columns, multi-source union.

Users who need these should preprocess before `dbcli` or run SQL after load. Several of these are tracked for v1.1.

---

## 9. Validation Model

Validation has three stages:

1. Recipe validation.
2. Edit application + row-level edit validation.
3. Final dataframe vs schema validation.

### 9.1 Recipe Validation

Fails with exit code `10`. Examples:

- Invalid YAML.
- Missing required fields.
- Unknown edit op.
- Invalid MySQL type.
- Duplicate schema columns.
- Literal password in profile when profile lint is triggered.

Recipe validation failures produce structured diagnostics, not rejects.

### 9.2 Edit-Time Row Validation

Failures become row-level rejects. Examples:

- Cast failure.
- Unparseable date.
- Decimal precision/scale overflow during cast.

Good rows continue unless the reject threshold is exceeded.

### 9.3 Final Schema Validation

Schema-level diagnostics (fail with exit code `10`):

- Extra dataframe columns not in schema.
- Schema columns missing from final dataframe.
- Declared type incompatible with cast result.

Row-level validation rejects:

- Non-nullable column has null.
- `VARCHAR(n)` length exceeded.
- Numeric value outside target type range.

### 9.4 Column Order

Before database insert, final dataframe columns are reordered to match `schema` order. If a schema column is missing or an extra column remains, validation fails.

### 9.5 Reject Threshold

`options.reject_threshold`:

- Default: `0.0`.
- Ratio is `unique_rejected_rows / rows_read`.
- If ratio exceeds threshold, command exits `20`.
- If `rows_read` is `0`, the ratio is `0.0`.
- Rejected rows are excluded from DB loading even when the threshold is not exceeded.

### 9.6 Row Accounting

- A source row may produce more than one rejects CSV record.
- Reject counts in reports and run history count unique source rows, not reject records.
- A row with an edit-time reject is removed before final schema validation.
- A row with a final validation reject is not loaded.
- If one row has both edit and validation failures, only the edit-stage reject is recorded because validation does not run for that row.

---

## 10. Load Mode Semantics

This section is the safety contract.

### 10.1 Shared Load Rules

All modes:

- Validate before DB writes.
- Check live schema against declared schema before writing (drift = exit `2`).
- Generate a `run_id` at start.
- Append exactly one final run record before exit, for both success and failure.
- Use `options.batch_size` for insert batches.
- Use SQLAlchemy Core parameterized statements.
- Never string-concatenate user values into SQL.
- Quote identifiers safely.

### 10.2 Drift Check

Before any write, `load` compares the declared schema to the live table:

- If table does not exist: proceed to create (per mode rules).
- If table exists and matches: proceed.
- If table exists and differs: exit `2` with structured diagnostics (`mysql.schema_drift`). The only mode allowed to resolve drift is `replace`, which rebuilds the table.

Drift examples: missing/extra column, type mismatch, nullability mismatch, charset/collation/engine mismatch.

### 10.2.1 DDL Boundaries

v1 creates plain MySQL tables only.

- No primary keys.
- No indexes.
- No unique constraints.
- No foreign keys.
- No default values.
- No auto-increment columns.
- No generated columns.
- No column comments.

If `target.table` is unqualified, it resolves inside `profile.database`.

If `target.table` is `schema.table`, the schema part is the MySQL database name for DDL/DML. The connection still uses `profile.database` as its default database, but all target, staging, and backup tables are created in the explicitly named schema.

### 10.3 `append`

Purpose: add rows to an existing table or newly created table.

- If table does not exist, create it from schema.
- If table exists, schema must match or `load` exits `2`.
- Data insert is transactional. On first failure, rollback the transaction.

### 10.4 `replace`

Purpose: replace the whole target table with the recipe-declared schema and rows.

Safety contract:

- Create a staging table with the declared schema.
- Load all rows into staging.
- Atomically swap staging with target using MySQL `RENAME TABLE`.
- On success, drop the backup table.
- On failure before swap, drop staging; original target is untouched.
- If the target table does not exist, rename staging directly to target and do not create a backup table.

Suggested internal table names:

```text
<target>__dbcli_staging__<run_id>
<target>__dbcli_backup__<run_id>
```

Atomicity:

- Load into staging is transactional.
- The final swap relies on MySQL atomic-rename semantics.
- If swap succeeds, target is replaced. If swap fails, original target remains.

---

## 11. Rejects and Diagnostics

Two output classes:

1. A single row-level rejects CSV per run.
2. Structured diagnostics JSON for schema/recipe/system failures.

### 11.1 Rejects CSV

One file per validate/load run at `.dbcli/rejects/<run-id>.csv`.

`validate` and `load` both mint a `run_id` for reports and rejects. Only `load` writes run history.

Contains original row contents plus metadata columns:

- `__stage` — `edit`, `validate`.
- `__row_index` — 1-indexed source data-row position after skipped rows and header handling.
- `__column` — offending column, if applicable.
- `__check` — failed check name.
- `__value` — raw or pre-failure value.
- `__reason` — short human-readable explanation.

v1 fails fast on load-time MySQL row errors (no `--continue-on-error`), so load failures appear in diagnostics, not in rejects.

### 11.2 Structured Diagnostics

Examples:

- `schema.extra_source_column`
- `schema.missing_source_column`
- `schema.type_mismatch`
- `source.empty`
- `source.empty_header`
- `source.duplicate_header`
- `source.csv_detection_failed`
- `profile.literal_password`
- `profile.missing_env_var`
- `mysql.schema_drift`
- `mysql.load_failed`

Every diagnostic has this shape:

```json
{
  "code": "schema.type_mismatch",
  "message": "Column seller_id is declared BIGINT but was cast as string.",
  "path": "schema[0].type",
  "details": {
    "column": "seller_id"
  }
}
```

Rules:

- `code` is stable and machine-readable.
- `message` is human-readable and may change between versions.
- `path` points to the recipe/config/source location when available; otherwise it is `null`.
- `details` contains structured context and must not contain secrets.

These diagnostics appear in:

- Human stderr output.
- JSON stdout when `--json` is set.
- Run history when the command creates a run record.

### 11.3 JSON Result Envelope

When `--json` is set, successful and expected-failure command output uses this envelope:

```json
{
  "status": "success",
  "command": "validate",
  "run_id": "r_2026_05_18T14_22_08Z_a91f",
  "recipe": "sellers",
  "profile": "dev",
  "table": "dim_sellers",
  "mode": "append",
  "rows": {
    "read": 42318,
    "edited": 42310,
    "validated": 42310,
    "loaded": null,
    "rejected_edit": 8,
    "rejected_validate": 0
  },
  "rejects": ".dbcli/rejects/r_2026_05_18T14_22_08Z_a91f.csv",
  "diagnostics": [],
  "duration_ms": 4214,
  "exit_code": 0
}
```

Rules:

- `status` is one of `success`, `failed`.
- Commands that do not create a run id set `run_id` to `null`.
- Commands that do not target a database set `profile`, `table`, `mode`, or `rows.loaded` to `null` when not applicable.
- Unexpected internal errors may emit a shorter diagnostic envelope, but must still avoid secrets.

---

## 12. Command Line Interface

All commands support:

- `--json` — emit machine-readable result data to stdout; human logs go to stderr.
- `--no-progress` — suppress progress bars/spinners.
- `--ci` — force CI mode.

### 12.1 Workspace Commands

```bash
dbcli init
dbcli profile add <name> --host HOST --user USER --database DB --password-env VAR [--port 3306]
dbcli profile list
dbcli profile remove <name>
dbcli profile test <name>
```

Profile add writes `${VAR}` interpolation, never the secret value.

### 12.2 Recipe Lifecycle Commands

```bash
dbcli scan <file> [--sheet SHEET] [--table TABLE] [--profile PROFILE] [--encoding ENC] [--delimiter DELIM]
dbcli recipes list
dbcli recipes show <name>
```

`scan`:

- Reads the file.
- Infers starter schema.
- For CSV, writes explicit `source.encoding` and `source.delimiter`.
- Writes `.dbcli/recipes/<name>.yaml`.
- Does not write starter edits in v1.

### 12.3 Validation and Loading

```bash
dbcli validate <recipe>
dbcli load <recipe>
```

`validate`:

- Loads source.
- Applies edits in memory.
- Validates final dataframe against schema.
- Writes rejects if row-level failures occur.
- Does not connect to DB.

`load`:

- Runs validation.
- Resolves profile and connects to MySQL.
- Checks live schema for drift.
- Creates / appends / replaces according to mode.
- Writes run history.

### 12.4 Inspection Commands

```bash
dbcli inspect <file> [--sheet SHEET] [--encoding ENC] [--delimiter DELIM]
dbcli history [--table TABLE] [--limit N]
dbcli show <run-id>
```

`inspect` reports file type, encoding/delimiter for CSV when detectable, sheet names for XLSX, header preview, first rows, suggested dtypes, and row/column counts when cheap.

`history` reads `.dbcli/runs.jsonl`. `show` prints one run record.

---

## 13. CI Mode

CI mode is triggered automatically when stdin is not a TTY, or explicitly with `--ci`.

### 13.1 CI Guarantees

- No interactive prompts.
- Progress bars/spinners suppressed.
- Logs are single-line structured stderr messages.
- JSON result data goes to stdout when `--json` is set.

Example log:

```text
level=info command=load recipe=sellers table=dim_sellers rows_read=42318 duration_ms=4214
```

### 13.2 Exit Codes

| Code | Meaning |
|---:|---|
| `0` | Clean success |
| `2` | Usage error or schema drift detected by `load` |
| `10` | Validation failed, recipe invalid, declared type wrong |
| `20` | Rejects exceeded threshold |
| `30` | DB / profile / connection error |
| `1` | Unexpected internal error |

### 13.3 Suggested CI Pattern

On PR:

```bash
dbcli validate .dbcli/recipes/sellers.yaml --ci --json
```

On merge:

```bash
dbcli load .dbcli/recipes/sellers.yaml --ci --json
```

Default production recommendation: use `replace` for atomic full refreshes; use `append` for incremental adds.

---

## 14. Run History

`.dbcli/runs.jsonl` is append-only. Each load run appends exactly one final JSON object.

Rules:

- `load` generates a `run_id` before validation starts.
- `load` appends the run record in a finalization path before the process exits.
- Successful loads and expected failures both write run records.
- `validate` does not write run history.
- Hard process termination may prevent a run record from being written.
- Existing records are never modified in place.

### 14.1 Run Record Shape

```json
{
  "run_id": "r_2026_05_18T14_22_08Z_a91f",
  "recipe": "sellers",
  "profile": "dev",
  "table": "dim_sellers",
  "mode": "append",
  "started_at": "2026-05-18T14:22:08Z",
  "finished_at": "2026-05-18T14:22:12Z",
  "duration_ms": 4214,
  "rows": {
    "read": 42318,
    "edited": 42310,
    "validated": 42310,
    "loaded": 42310,
    "rejected_edit": 8,
    "rejected_validate": 0
  },
  "rejects": ".dbcli/rejects/r_2026_05_18T14_22_08Z_a91f.csv",
  "diagnostics": [],
  "exit_code": 0,
  "git_sha": "abc123"
}
```

Row count meanings:

- `read` — source data rows after skipped rows and header handling.
- `edited` — rows remaining after edit-time rejects are removed.
- `validated` — rows remaining after final validation rejects are removed.
- `loaded` — rows successfully written to MySQL.
- `rejected_edit` — unique source rows rejected during edit application.
- `rejected_validate` — unique source rows rejected during final validation.

### 14.2 Git SHA

If the project root is inside a git repository, capture current `HEAD` SHA. If unavailable, set `git_sha: null`.

---

## 15. Report Output

Every validate/load should produce a concise report.

Example human report:

```text
sellers @ dev.dim_sellers (append)
  read:       42,318
  edited:     42,310   (8 cast failures → .dbcli/rejects/r_...csv)
  validated:  42,310
  loaded:     42,310
  duration:   4.2s
```

Human output goes to stderr when `--json` is set. JSON output goes to stdout.

---

## 16. Security Rules

- Do not log passwords.
- Do not write literal passwords to disk.
- Do not include environment-variable-resolved values in run history.
- Use parameterized SQL for row values.
- Validate/quote identifiers; never interpolate raw identifiers without validation.
- Refuse suspicious table/column identifiers.

---

## 17. Implementation Boundaries

Suggested internal modules:

```text
dbcli/
├── cli.py
├── project.py          # find/init .dbcli project
├── profiles.py         # TOML profiles + env interpolation
├── recipes.py          # YAML parsing + validation
├── source.py           # CSV/XLSX inspection and loading
├── edits.py            # edit pipeline
├── schema.py           # type mapping + schema validation
├── mysql.py            # SQLAlchemy connection + DDL/DML
├── load.py             # load orchestration + modes
├── rejects.py          # reject writer
├── history.py          # runs.jsonl
└── output.py           # human/json/CI output
```

Required separations:

- Recipe parsing must not connect to DB.
- Validation must not connect to DB.
- Loading owns run history.
- Edit ops must be independently unit-testable.

---

## 18. Acceptance Criteria for v1

### 18.1 Init / Profile

- `dbcli init` creates `.dbcli/` structure.
- `dbcli profile add` writes env-interpolated password reference only.
- `dbcli profile test` exits `0` on valid connection, `30` on failure.

### 18.2 Inspect / Scan

- `dbcli inspect` can inspect CSV and XLSX.
- `dbcli scan` writes a starter recipe with source, target, schema, empty edits, options, and explicit CSV encoding/delimiter when applicable.

### 18.3 Validate

- Applies edits in order.
- Extra/missing columns vs. schema fail validation.
- Cast failures write edit rejects.
- Nullability/length failures write validate rejects.
- Never connects to DB.

### 18.4 Load

- `append` creates table if missing and inserts rows; exits `2` on drift.
- `replace` uses staging + atomic rename contract.
- Every load writes a `.dbcli/runs.jsonl` entry.

### 18.5 CI / JSON

- No prompts in CI.
- `--json` emits result JSON to stdout; logs/human output go to stderr.
- Exit codes match the table in this spec.

---

## 19. v1.1 Candidates

Deferred from v1, ranked by expected value:

1. `upsert` mode (requires `primary_key` block, `INSERT ... ON DUPLICATE KEY UPDATE`).
2. `truncate-load` mode (with explicit non-atomic warnings).
3. Separate `plan` command with full drift report.
4. File locks (`.dbcli/locks/`) for concurrent-load safety.
5. Idempotency keys (`--idempotency-key`).
6. `--continue-on-error` with batch bisection and load-time rejects.
7. Additional edit ops: `select`, `normalize_case`, `replace`, `filter`, `dedupe_on`.
8. XLS (legacy Excel binary) support.
9. `dbcli lint` for pre-commit recipe checks.
10. `scan --from-recipe` to refresh schema suggestions against an evolved source.

---

## 20. Final v1 Principle

If a behavior cannot be made explicit, replayable, and safe in CI, it does not belong in v1.

Boring is the feature.
