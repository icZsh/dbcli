# dbcli v1 — Build Sequence

## Context

`dbcli` is a greenfield project at `/Users/isaaczhu/dbcli/`. Existing planning artifacts are `SPEC.md`, `BUILD.md`, and `ROADMAP.md`. No code, no git, no scaffolding.

The spec describes a Python CLI that loads CSV/XLSX into MySQL via declarative YAML recipes, with two load modes (`append`, `replace`), six edit ops, lossless-first ingestion, structured rejects, run history JSONL, and CI-safe exit codes.

This plan sequences the v1 build into 11 dependency-ordered milestones. Each milestone leaves the codebase in a shippable, testable state. The ordering front-loads three high-risk areas (lossless ingestion, edit pipeline null/cast semantics, MySQL drift comparison) and defers DB-dependent work until a non-DB foundation exists. `validate` is fully usable by M6 with zero MySQL code; `load` arrives in M9 (append) and M10 (replace).

Tech stack is settled: Python 3.11+, Typer, Polars, python-calamine, SQLAlchemy Core + PyMySQL, pipx + PyPI.

---

## Milestones

### M1 — Package skeleton + output / exit-code spine
**Ships:** `pyproject.toml`, `dbcli` Typer entry point, `output.py` (human/JSON/CI mode detection, stderr vs stdout split), `errors.py` with exit-code constants (0/1/2/10/20/30), `--version` and `--help`. CI-mode auto-detected from `isatty`.
**Touches:** §3.1, §12 global flags, §13.
**Acceptance:** `dbcli --version` exits 0; `--json` routes structured output to stdout, logs to stderr; `--ci` and non-TTY both trigger CI mode. Snapshot tests for both render paths.
**Deferred:** Command bodies. Run history. Diagnostics catalog beyond enum.

### M2 — Project + config + profile lint (no DB)
**Ships:** `project.py` (find/init `.dbcli/`), `dbcli init`, TOML config with precedence chain (CLI > recipe > project > builtin), `profiles.py` with `${ENV}` interpolation, literal-password lint, `profile add/list/remove`. No `profile test` yet.
**Touches:** §4, §5, §6 (minus 6.2), §12.1.
**Acceptance:** `dbcli init` produces the exact `.dbcli/` tree; `profile add` writes `${VAR}` form only; missing env var → exit 30 with `profile.missing_env_var`; literal-password file → structured diagnostic + exit 10.
**Deferred:** MySQL connection.

### M3 — Source ingestion + `inspect` (lossless-first) — **HIGH RISK**
**Ships:** `source.py` reading CSV (strings-only, explicit encoding/delimiter for recipe execution; detection only for `inspect`/`scan`) and XLSX (calamine, native cell types), `dbcli inspect`.
**Touches:** §3.3, §7.2, §12.4 (inspect only).
**Acceptance:** CSV with leading zeros, SKU-like ints, and date-shaped strings survives read as strings; XLSX with multiple sheets requires `--sheet`; `inspect` prints encoding/delimiter/sheets/preview; `validate`/`load` reject CSV recipes missing explicit encoding or delimiter.
**Risk:** The lossless-first rule is the spec's load-bearing invariant. Front-load fixture coverage: leading zeros, mixed-type columns, European decimals, BOM, CRLF, quoted commas. If this is wrong, every downstream test is unreliable.

### M4 — Recipe parser + schema types + `scan`
**Ships:** `recipes.py` (YAML parse, structural validation, unknown-key rejection), `schema.py` parsing the §7.5 type subset including `VARCHAR(n)` and `DECIMAL(p,s)`, `dbcli scan` (writes starter recipe with explicit CSV settings and empty edits block), `dbcli recipes list/show`.
**Touches:** §7, §9.1, §12.2.
**Acceptance:** Invalid YAML → exit 10 with `recipe.invalid_yaml`; unsupported MySQL type → exit 10; duplicate schema columns → exit 10; `scan` output round-trips through the parser.
**Deferred:** Edit execution, dataframe validation.

### M5 — Edit pipeline + rejects writer — **HIGH RISK**
**Ships:** `edits.py` with `rename`, `drop`, `trim`, `parse_null`, `cast`, `fill_null` in declared order; `rejects.py` writing `.dbcli/rejects/<run-id>.csv` with §11.1 metadata columns; cast failures routed as edit rejects.
**Touches:** §8, §9.2, §11.1.
**Acceptance:** Per-op unit tests (rename collisions, drop of missing column, trim preserves null, parse_null exact-match post-trim, cast with strptime format, fill_null type compatibility). Cast failures produce reject rows with `__stage=edit`, `__check=cast_failed`, original contents preserved.
**Risk:** Polars semantics around null preservation across trim/cast, decimal precision overflow, and strptime date parsing are easy to get subtly wrong. Build a strong fixture matrix.

### M6 — Final schema validation + `dbcli validate` (FIRST SHIPPABLE CHECKPOINT)
**Ships:** End-to-end `dbcli validate` (no DB): source → edits → final-dataframe-vs-schema checks (extra/missing columns, declared-type vs cast-result compatibility, non-null violations, VARCHAR(n) length, numeric range), column reorder to schema order, reject threshold (exit 20), report renderer.
**Touches:** §9.3–§9.6, §12.3, §15.
**Acceptance:** §18.3 criteria. A recipe with deliberate cast failures + one over-length VARCHAR produces a rejects CSV with both `__stage=edit` and `__stage=validate` rows and the correct exit code based on threshold.
**Note:** First user-shippable checkpoint — `validate` works fully without any DB code.

### M7 — Run history + report parity
**Ships:** `history.py` appending final `.dbcli/runs.jsonl` records, run-id minting, git-SHA capture, `dbcli history`, `dbcli show <run-id>`. `validate` stays history-free per spec — this milestone lands the writer so M9+ can use it.
**Touches:** §14, §12.4.
**Acceptance:** Synthetic run records round-trip through writer/reader; `history` filters by table; missing git → `git_sha: null`.

### M8 — MySQL connectivity + `profile test` + identifier safety
**Ships:** `mysql.py` — SQLAlchemy Core engine factory from resolved profile, pooling config, `SELECT 1`, identifier validator/quoter, `dbcli profile test`. Docker-compose service for local MySQL in CI.
**Touches:** §6.2, §10.1 identifier rules, §16.
**Acceptance:** `profile test` exits 0 against a local MySQL fixture, 30 on bad creds/missing env, never logs the password. Identifier validator rejects backticks, semicolons, non-`schema.table` dotted forms.
**Note:** Settle CI MySQL infra here, not in M9.

### M9 — `append` load: DDL create + drift check + batched insert — **HIGH RISK**
**Ships:** Recipe-type → SQLAlchemy DDL mapping, `CREATE TABLE` with charset/collation/engine, drift comparator (column set, types, nullability, charset/collation/engine), batched parameterized INSERT, transactional rollback on first error, run-record writing on success and failure, `mysql.load_failed` diagnostic, `dbcli load` for append mode.
**Touches:** §10.1–§10.3, §11.2, §14.
**Acceptance:** §18.4 append criteria. New table created and populated; matching live table appends; differing live table → exit 2 with `mysql.schema_drift` listing each diff; mid-batch SQL error → rollback, run record `exit_code=30`, no partial rows.
**Risk:** Drift comparison is where MySQL reality bites (collation defaults, `INT` vs `INT(11)` display width, implicit `NOT NULL` on PK columns). Build a normalization layer on both sides of the comparison before M10 inherits it.

### M10 — `replace` mode (staging + atomic rename)
**Ships:** Staging table creation with `__dbcli_staging__<run_id>` suffix, load into staging, `RENAME TABLE` atomic swap, backup-then-drop on success, staging cleanup on pre-swap failure.
**Touches:** §10.4.
**Acceptance:** Replace against a populated table → target ends with new rows; concurrent reader sees no missing-table window (verified test). Pre-swap failure → original target untouched, staging removed. Replace resolves drift (the one mode that can).

### M11 — Acceptance suite, CI workflow, packaging
**Ships:** Full §18 acceptance suite as automated tests; CI workflow running validate + load against ephemeral MySQL; exit-code matrix tests; `pyproject` finalization for pipx/PyPI; README with §13.3 CI patterns.
**Acceptance:** Every §18 bullet has a green test. `pipx install` from a built wheel produces a working `dbcli`.

---

## Critical files to be created

```
/Users/isaaczhu/dbcli/
├── pyproject.toml                  # M1
├── README.md                        # M11
├── .gitignore                       # M1
├── dbcli/
│   ├── __init__.py                  # M1
│   ├── cli.py                       # M1, grows per milestone
│   ├── output.py                    # M1
│   ├── errors.py                    # M1
│   ├── project.py                   # M2
│   ├── profiles.py                  # M2 (test fn in M8)
│   ├── source.py                    # M3
│   ├── recipes.py                   # M4
│   ├── schema.py                    # M4
│   ├── edits.py                     # M5
│   ├── rejects.py                   # M5
│   ├── history.py                   # M7
│   ├── mysql.py                     # M8
│   └── load.py                      # M9, extended in M10
├── tests/                           # per-milestone, starts M1
└── docker-compose.yml               # M8 (MySQL test service)
```

---

## Dependency surprises / ordering pitfalls

- **`cast` depends on resolved schema types** — M4 must land before M5. `cast` declares `type: decimal, precision, scale` which must align with `DECIMAL(p,s)` parsed from schema. Skipping this order forces double validation of decimal params.
- **`fill_null` schema compatibility check belongs in M6, not M5.** Don't try to validate fill values inside the edit op — the post-cast final-validation pass is where it fits.
- **`parse_null` runs on post-edit, pre-cast values** in the common case. Pipeline is strict declared-order (§8). Document loudly in `scan` output. Do not add "smart" reordering.
- **Drift normalization is the silent killer.** `information_schema` returns charset/collation/engine in forms that don't always match recipe text (`utf8mb4` default collation, engine casing). Build a normalizer in M9 before M10 inherits a broken comparator.
- **Run history before MySQL (M7 before M8) is deliberate.** `load` failures must still write run records (§2.3, §14). Retrofitting error-path writes after `load` lands tends to miss cases.
- **Rejects writer in M5, before `validate` in M6, is deliberate.** Edit-time and validate-time rejects share one CSV per run, same writer, different `__stage`.
- **CI-mode detection in M1, not M11.** Output routing (stdout vs stderr, JSON vs human) shapes every command's logging from day one. Retrofitting is painful.

---

## Riskiest milestones to front-load

1. **M3** — lossless ingestion invariant.
2. **M5** — edit pipeline null/cast semantics in Polars.
3. **M9** — drift comparison against real MySQL.

If any of these surfaces a redesign, pause and fix the foundation before moving on. They are listed in their natural build order, so no sequence change is needed — just slow down for them.

---

## Verification

Each milestone has its own acceptance criteria above. Whole-build verification at M11:

1. `pytest` — all per-milestone unit tests green.
2. `docker-compose up -d mysql && pytest tests/integration/` — full §18 acceptance suite green against a real MySQL.
3. `python -m build && pipx install dist/dbcli-*.whl` — installed CLI executes `dbcli --version` and `dbcli init` against a clean directory.
4. Exit-code matrix test: every code in §13.2 (0/1/2/10/20/30) is provably reachable via a deliberately-shaped fixture.
5. CI workflow runs (1)–(4) on every PR.
