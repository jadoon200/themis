# THEMIS, for whoever approves it

One page: what it is, what it installs, what it reads, what it writes, and what leaves
the network. Each claim names where in the code it is enforced, so it can be checked
rather than taken on trust.

## What it is

A command-line tool that reviews a change to a dbt project before it merges: it reads the
SQL of both revisions, checks it against 33 rules for defects that are costly in financial
data (fan-outs, dropped filters, money cast to floating point, periods restated), and —
when asked — builds both revisions in a non-production schema and compares the results.
An optional internal web page shows the reviews. MIT-licensed, open source.

## What it installs

Python 3.12 packages, all from PyPI:

| package | licence | package | licence |
|---|---|---|---|
| dbt-core | Apache-2.0 | sqlglot | MIT |
| dbt-trino | Apache-2.0 | pydantic, pydantic-settings | MIT |
| dbt-duckdb, duckdb | Apache-2.0, MIT | sqlalchemy, alembic | MIT |
| trino (client) | Apache-2.0 | fastapi | MIT |
| psycopg | **LGPL-3.0** | uvicorn, httpx | BSD-3-Clause |
| structlog | MIT or Apache-2.0 | typer, rich, pyyaml | MIT |

`psycopg`, the PostgreSQL driver, is the one copyleft licence. It is used unmodified, as a
library, which LGPL permits; it is only needed when reviews are stored in PostgreSQL
(SQLite needs nothing extra). Optional: Ollama (MIT) with the Qwen3 8B model (Apache-2.0)
on a GPU host, for the parts that use a language model; every review also runs without one.

## What it reads

- The dbt project's git repository. Earlier revisions are checked out into temporary
  worktrees under the system temporary directory and removed afterwards.
- The warehouse, through dbt and through its own Trino client, using the **same dbt
  profile and credentials** (`execute/warehouse.py`, `trino_connect`). Secrets stay in
  environment variables, as dbt expects; THEMIS resolves them in memory and writes none
  to disk.

## What it writes

- **In the warehouse, only with `--execute`**: dbt builds the two revisions into schemas
  named `themis_base_<random>` and `themis_head_<random>`, and THEMIS drops them afterwards —
  the only writes THEMIS issues itself (`drop_run_schemas`); everything else it sends is a
  read. Before building, it asks dbt where every node would be written and never builds
  one that would land outside those schemas: a snapshot with a legacy `target_schema`
  is read where it is, or left out and reported (`execute/runner.py`,
  `plan_locations`). The dbt target must be on
  an allowlist that fails closed; `themis init` proposes only targets whose names do not
  say production (`acquire/dbt_runner.py`, `assert_target_allowed`).
- `dbt compile` is not read-only — a project's macros can run queries while compiling — so
  the same allowlist guards compiling too.
- Its own records: SQLite by default, or a PostgreSQL database it is given.
- Report files (SARIF, JSON) where asked. `--redact` writes them with no SQL, no measured
  values and hashed names.

## What leaves the network

Nothing. Enforced, not assumed:

- No telemetry of its own, no hosted model, no API keys. The language model is Ollama, at an
  address you configure (`THEMIS_LLM_BASE_URL`).
- dbt reports anonymous usage to dbt Labs by default; every dbt THEMIS starts is told not to
  (`DBT_SEND_ANONYMOUS_USAGE_STATS=False`, `DO_NOT_TRACK=1`, `acquire/dbt_runner.py`).
- `themis doctor` reads dbt's version from the installed package; `dbt --version` would ask
  pypi.org for the latest release.
- The optional MCP server speaks over standard input and output only.

`tests/test_no_egress.py` holds the two dbt points in place.

## Access it needs

| for | access |
|---|---|
| reviewing a change, rules only | read access to the git repository; a dbt profile whose target can compile |
| `--execute` | create and drop schemas in each non-production catalog the project writes to — Hive for models and Iceberg for snapshots, at work |
| the web page | behind the organisation's sign-in proxy, which passes the user's name in a header (`THEMIS_UI_TRUSTED_USER_HEADER`). `themis serve` binds to the local machine by default, and with that header configured refuses to bind beyond it unless told only the proxy can reach the port — otherwise anyone reaching it directly could claim any name |
| the queue API | a bearer token (`THEMIS_API_TOKEN`) |

## How to check it

```bash
themis doctor --project <dbt project> --target <dev target>   # every prerequisite, and the fix for each
make check                                                    # lint, strict typing, the test suite
```
