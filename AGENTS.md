# AGENTS.md

Shared project instructions for coding agents (Codex, Cursor, Claude Code ≥2.1.277, etc.).

## What this is

`dbcli` is a recipe-first Python CLI that loads CSV/XLSX into MySQL. Spreadsheet values stay strings until a recipe casts them. Validation runs before any DB write. Loads support `append` and atomic `replace`, with CI-friendly JSON and `.dbcli/runs.jsonl` history.

## Setup & commands

```bash
uv sync --dev
uv run dbcli --version
uv run pytest
uv build
```

Common CLI flows:

```bash
dbcli init
dbcli scan data/sellers.csv --table dim_sellers --encoding utf-8 --delimiter ","
dbcli validate dim_sellers
dbcli load dim_sellers
dbcli history --limit 10
```

CI-style:

```bash
dbcli validate .dbcli/recipes/dim_sellers.yaml --ci --json
dbcli load .dbcli/recipes/dim_sellers.yaml --ci --json
```

Do not commit secrets: keep `.dbcli/profiles.toml`, `.dbcli/rejects/`, and `.dbcli/runs.jsonl` out of git (see `.gitignore`).

## Exit codes (do not invent new ones without updating README)

| Code | Meaning |
|---:|---|
| 0 | Success |
| 2 | Usage error or schema drift |
| 10 | Validation, recipe, source, or config error |
| 20 | Reject threshold exceeded |
| 30 | Profile, connection, or MySQL load error |
| 1 | Unexpected internal error |

Known open issues include exit-code / help-text / empty-CSV / `scan-dir` overwrite / `None` in load reports — prefer fixing those over new features unless asked.

## Conventions

- Prefer `uv` over raw `pip`.
- Keep recipe YAML as the single place for source cleanup, schema, target table, and load mode.
- Preserve string values until an explicit recipe `cast`.
- `replace` must stay atomic (staging + `RENAME TABLE`); never leave a half-swapped table.
- Structured diagnostics for MySQL failures; row-level rejects go to `.dbcli/rejects/`.
- Tests: `uv run pytest`. CI covers Python 3.11/3.12 plus a live MySQL job — don't break those paths.
- Match existing tone in README and help text; top-level `--help` should describe commands when you touch CLI help.
