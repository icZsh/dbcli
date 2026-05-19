# dbcli

`dbcli` is a recipe-first CLI for loading CSV/XLSX files into MySQL tables. It reads one spreadsheet-like source, applies declared cleanup edits, validates the final dataframe against a MySQL schema, then either appends rows or atomically replaces the target table.

The full v1 contract lives in [SPEC.md](SPEC.md). The milestone build plan lives in [BUILD.md](BUILD.md).

## Install

For local development:

```bash
python -m pip install -e ".[dev]"
dbcli --version
```

For an isolated CLI install from a checkout or built wheel:

```bash
pipx install .
```

## Project Setup

Initialize a project from the directory that should own recipes and run history:

```bash
dbcli init
```

This creates:

```text
.dbcli/
├── config.toml
├── profiles.toml
├── profiles.example.toml
├── recipes/
├── rejects/
└── runs.jsonl
```

Recommended `.gitignore` entries:

```gitignore
.dbcli/profiles.toml
.dbcli/rejects/
.dbcli/runs.jsonl
```

## Profiles

Profiles live in `.dbcli/profiles.toml`. Passwords are always stored as environment-variable references, never literal secrets.

```bash
dbcli profile add dev \
  --host localhost \
  --user dbcli \
  --database ecom_dev \
  --password-env DBCLI_DEV_PW

export DBCLI_DEV_PW='...'
dbcli profile test dev
```

Missing password environment variables exit `30`. Literal passwords in the profile file are rejected.

## Recipe Workflow

Inspect a source file:

```bash
dbcli inspect data/sellers.csv --encoding utf-8 --delimiter ","
```

Generate a starter recipe:

```bash
dbcli scan data/sellers.csv --table dim_sellers --profile dev --encoding utf-8 --delimiter ","
```

Edit `.dbcli/recipes/dim_sellers.yaml` until the source, target, schema, edits, and options match the intended load.

Validate without touching MySQL:

```bash
dbcli validate dim_sellers --ci --json
```

Load into MySQL:

```bash
dbcli load dim_sellers --ci --json
```

## Load Modes

`append` creates the target table when it is missing, checks drift when it exists, and inserts rows in batches. Drift exits `2` with a `mysql.schema_drift` diagnostic.

`replace` creates a staging table, loads rows into staging, then uses MySQL `RENAME TABLE` to swap staging into the target name. If the target exists, it is first renamed to a run-specific backup and the backup is dropped after a successful swap. Pre-swap failures clean up staging and leave the original target untouched.

Both modes validate first and append one final run record to `.dbcli/runs.jsonl` on success or expected failure.

## Recipes

Minimal CSV recipe:

```yaml
name: sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
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
edits:
  - cast: {seller_id: {type: int}}
options:
  reject_threshold: 0.01
  batch_size: 5000
```

CSV recipes must declare `source.encoding` and `source.delimiter`. XLSX recipes use `source.sheet` when a workbook contains multiple sheets.

## Run History

Only `load` writes run history. Inspect it with:

```bash
dbcli history --limit 10
dbcli show <run-id>
```

Row-level edit and validation failures are written to `.dbcli/rejects/<run-id>.csv`. MySQL load failures are diagnostics, not reject rows.

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | Clean success |
| `2` | Usage error or schema drift |
| `10` | Validation, recipe, source, or config error |
| `20` | Reject threshold exceeded |
| `30` | Profile, connection, or MySQL load error |
| `1` | Unexpected internal error |

## CI

Suggested pull-request gate:

```bash
dbcli validate .dbcli/recipes/sellers.yaml --ci --json
```

Suggested merge/deploy gate:

```bash
dbcli load .dbcli/recipes/sellers.yaml --ci --json
```

The repository workflow in `.github/workflows/ci.yml` installs `.[dev]`, runs `python -m pytest`, and builds source/wheel distributions on Python 3.11 and 3.12.
