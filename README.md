# dbcli

`dbcli` is a small, recipe-first CLI for loading CSV/XLSX files into MySQL. It keeps spreadsheet ingestion explicit, validates rows before touching the database, and writes an auditable run history for every load.

![dbcli quickstart demo](docs/assets/dbcli-quickstart.svg)

Terminal demo from a local quickstart run: initialize a project, generate a recipe, and validate rows before loading.

## Why dbcli

- Preserve CSV values as strings until a recipe explicitly casts them.
- Keep source cleanup, schema, target table, and load mode in one YAML recipe.
- Validate edits, nullability, string lengths, numeric ranges, and schema shape before writing.
- Load with `append` or atomic `replace` semantics.
- Emit CI-friendly JSON, structured diagnostics, rejects CSVs, and `.dbcli/runs.jsonl` history.

## Install

For development:

```bash
uv sync --dev
uv run dbcli --version
```

For an isolated CLI install from a checkout:

```bash
uv tool install .
```

## Setup

Create a dbcli project in the directory that owns your data recipes:

```bash
dbcli init
```

This creates a project-local `.dbcli/` directory and a default MySQL profile. The profile uses:

- host: `localhost`
- port: `3306`
- user: `dbcli`
- database: a safe name derived from the current directory, for example `Company Data` becomes `company_data`
- password: a generated value stored in macOS Keychain

The generated password is never written to `profiles.toml`.
After writing the profile, `dbcli init` tries to provision local MySQL with admin user `root` and no admin password. When that works, it creates the database, creates or updates the `dbcli` user, grants access, and verifies the default profile.
If MySQL is not running, `dbcli init` leaves setup in place and prints a hint to start it, for example `brew services start mysql`, before retrying provisioning. It does not start background services automatically.
If local admin access needs a password, initialization still succeeds and prints a follow-up provisioning command:

```bash
dbcli mysql provision --admin-user root
```

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

## Quickstart

Given a CSV:

```csv
seller_id,tier
001,VIP
002,STD
003,VIP
```

Generate a starter recipe:

```bash
dbcli scan data/sellers.csv \
  --table dim_sellers \
  --encoding utf-8 \
  --delimiter ","
```

Edit `.dbcli/recipes/dim_sellers.yaml`:

```yaml
name: dim_sellers
source:
  path: data/sellers.csv
  encoding: utf-8
  delimiter: ","
target:
  profile: default
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

Validate without connecting to MySQL:

```bash
dbcli validate dim_sellers
```

Load into MySQL:

```bash
dbcli load dim_sellers
```

If `dbcli load` cannot find a recipe, run `dbcli scan .` first to generate recipes for supported CSV/XLSX files in the project.

For CI, add `--ci --json`:

```bash
dbcli validate .dbcli/recipes/dim_sellers.yaml --ci --json
dbcli load .dbcli/recipes/dim_sellers.yaml --ci --json
```

## Load Modes

`append`

- Creates the target table when it is missing.
- Checks an existing table for schema drift.
- Inserts rows in batches inside a transaction.
- Exits `2` with `mysql.schema_drift` if the live table differs.

`replace`

- Creates a run-specific staging table.
- Loads validated rows into staging.
- Swaps staging into the target name with MySQL `RENAME TABLE`.
- Drops the backup after a successful swap.
- Cleans up staging on pre-swap failure and leaves the original target untouched.

## Inspect Results

Only `load` writes run history:

```bash
dbcli history --limit 10
dbcli show <run-id>
```

Row-level edit and validation failures are written to:

```text
.dbcli/rejects/<run-id>.csv
```

MySQL load failures are reported as structured diagnostics, not reject rows.

## Common Commands

| Command | What it does |
|---|---|
| `dbcli init` | Creates the local `.dbcli/` project structure and a default MySQL profile. |
| `dbcli init --host 127.0.0.1 --user loader --database warehouse` | Creates the default profile with explicit connection values. |
| `dbcli init --no-provision` | Creates local dbcli files and credentials without touching MySQL. |
| `dbcli mysql provision --admin-user root` | Creates the configured MySQL database/user using admin authorization. |
| `dbcli profile test` | Connects to MySQL with the default profile and runs `SELECT 1`. |
| `dbcli inspect data/sellers.csv --encoding utf-8 --delimiter ","` | Reads source metadata, headers, row count, and a preview. |
| `dbcli scan data/sellers.csv --table dim_sellers --encoding utf-8 --delimiter ","` | Generates `.dbcli/recipes/dim_sellers.yaml` from the source file. |
| `dbcli scan data --encoding utf-8 --delimiter ","` | Generates recipes for all CSV/XLSX files directly under `data/`. |
| `dbcli recipes list` | Lists recipes found in `.dbcli/recipes/`. |
| `dbcli recipes show dim_sellers` | Prints the resolved recipe YAML for `dim_sellers`. |
| `dbcli validate dim_sellers` | Applies edits and validates rows without connecting to MySQL. |
| `dbcli load dim_sellers` | Validates rows, then loads them into the configured MySQL table. |
| `dbcli history --limit 10` | Shows the most recent load run records. |
| `dbcli show <run-id>` | Prints the full history record for one load run. |

## Exit Codes

| Code | Meaning |
|---:|---|
| `0` | Clean success |
| `2` | Usage error or schema drift |
| `10` | Validation, recipe, source, or config error |
| `20` | Reject threshold exceeded |
| `30` | Profile, connection, or MySQL load error |
| `1` | Unexpected internal error |

## Development

```bash
uv sync --dev
uv run pytest
uv build
```

The GitHub Actions workflow in `.github/workflows/ci.yml` runs tests and package builds on Python 3.11 and 3.12.
It also runs a live MySQL integration job that exercises append, drift detection, rollback on SQL failure, and replace mode against a MySQL service container.
