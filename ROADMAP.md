# dbcli Roadmap

This file tracks post-v1 candidates. `SPEC.md` remains the implementation contract for v1.

## v2 Candidates

Ranked:

1. Schema sync against live MySQL for additive changes only.
2. File-to-table diff: `dbcli diff <recipe>`.
3. Multi-source recipes with union-before-load.
4. Streaming for files larger than 100 MB.
5. Postgres target.
6. `LOAD DATA LOCAL INFILE` path for large MySQL loads.
7. Derived columns with a deliberately tiny expression language.
8. Pre/post SQL hooks with strict safety boundaries.
