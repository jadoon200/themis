# Setting THEMIS up on a real project

Written for the first run on a project nobody here has seen — a work dbt project on Trino
(Starburst), with its profile in `~/.dbt`, no declared tests, heavy macros, and audit
columns on most models. Every step below exists because the naive version of it failed.

Nothing here sends anything off the machine. The model runs locally through Ollama; the
warehouse is only ever reached through a target you allow. For whoever approves software,
[APPROVAL.md](APPROVAL.md) is the one-page answer to what it installs, reads, writes and
sends.

## 0. Before the first day

Seven answers decide how the first week goes. Ask a colleague before arriving:

| question | why it matters |
|---|---|
| How does Trino log in — LDAP (password over HTTPS), JWT, Kerberos, certificate, OAuth? | THEMIS logs in with dbt-trino's own credentials, so every method dbt supports works, except OAuth, which needs a browser; a review runs unattended, so it needs a service account |
| Is there a non-production schema THEMIS may create tables in? | `--execute` builds both revisions of a change into schemas of its own and drops them after. Without one it reviews rules-only, which is still the bulk of it |
| How does the dagster-dbt repository provide its dbt profile, and which dbt version? | Dagster setups often generate the profile at run time; THEMIS needs a profile with a dev target on disk, or `DBT_PROFILES_DIR` pointing at one |
| Which snapshots set `target_schema` rather than `schema`, and is each one built in dev? | a legacy `target_schema` fixes where a snapshot is written whatever the target says, so `--execute` never builds it: models reading it are measured against the table already there — which has to exist in the dev target — and a change to the snapshot itself is reviewed by its rules only. `schema` (dbt 1.9+) follows the target and is measured normally |
| How does dbt get the values in `env.config.ini` — `var()`, `env_var()`, or names written out in the SQL? And which section is the non-production one? | `THEMIS_DBT_ENV_CONFIG` gives dbt a section both ways; `themis doctor` says which `var()`/`env_var()` names still have no value, and `themis profile` counts models that read project tables by name rather than `ref()` — a dependency nothing downstream can see |
| Where does production's `target/manifest.json` live? | `--defer-state` reads unchanged upstream models where production already built them instead of rebuilding them |
| What can be installed: a PyPI mirror, Ollama and a model on the GPU host, Postgres? | SQLite is enough to start; the model is optional (`--no-llm`); everything else is on PyPI |

The code comes in through whatever route is approved, and nothing from the office goes
back to the public repository — not a finding, not a profile, not a model name.

## 1. Install

```bash
conda create -y -n themis python=3.12 pip && conda activate themis
pip install uv && uv pip install -r requirements.txt && uv pip install -e .
```

`uv`, not `pip` — pip spends ten minutes backtracking on dbt-core. dbt-trino is part of
the base dependencies. A Kerberos login needs one more package, `requests-kerberos`.

The local model, about 5 GB. Optional — every review runs without it (`--no-llm`):

```bash
ollama pull qwen3:8b
```

## 2. Configure

From the directory you will run THEMIS in:

```bash
themis init --project /path/to/dbt/project
```

It writes `.env` with a generated redaction salt and an allowlist proposed only from
profile targets whose names do not say production, and an empty `themis_conventions.yml`
in the project. It never overwrites a file, and says whether `.env` is ignored by git.
**Read the allowlist it proposes and confirm none of those targets is production.**

If the Dagster project keeps its environments in a file — an `env.config.ini` with the
Trino environment and every schema, per environment — point THEMIS at it and name the
section to use. dbt then gets that section exactly as Dagster would hand it over, as
`--vars` and as environment variables, so whichever the project reads finds its value:

```bash
THEMIS_DBT_ENV_CONFIG=/path/to/env.config.ini
THEMIS_DBT_ENV_SECTION=uat
```

A section whose name says production — `prod`, `preprod`, `prd`, `live` — is refused: its
values can point dbt at a production warehouse whatever the target is called. The values
are never logged or written anywhere.

## 3. Check

```bash
themis doctor --project /path/to/dbt/project --target <your-dev-target>
```

Checks Python, dbt, the adapter your target uses, where the profile is (the project,
`DBT_PROFILES_DIR` or `~/.dbt`, in dbt's order), the allowlist, that **dbt** can reach the
warehouse, that **THEMIS itself** can log in and read the way measurement will, git, the
compiled manifest, the local model and its context window, the database and its
migrations, conventions and the redaction salt — and prints the command that fixes each.
Fix every `FAIL`.

The two logins are checked separately because they are different code. `{{ env_var(...) }}`
in the profile is resolved the way dbt resolves it, and a variable that is not set in this
shell is named rather than sent to the warehouse as a password. If THEMIS cannot log in, a
review with `--execute` says so and is marked incomplete; it never reports that nothing
moved.

## 4. Measure the project before trusting anything on it

```bash
(cd /path/to/dbt/project && dbt compile --target <your-dev-target>)
themis profile --project /path/to/dbt/project --json
```

Counts only — no SQL, no names. How much of the SQL parses as Trino, how deep CTEs go, how
far macros reach, how much grain and lineage resolve, and how often the vocabulary matches.
This is the file that is safe to share, and the one that says whether THEMIS's assumptions
hold here.

## 5. Replay what already merged

```bash
themis backtest --project /path/to/dbt/project --target <your-dev-target> --last 20 \
  --json backtest.json
```

The last twenty changes to the project on the main branch, each reviewed against its first
parent — rules only, nothing built, nothing written. It prints findings by severity and
which rules fired, per change and in total: how often THEMIS would have spoken up, and how
loudly, on changes whose outcome is already known. Counts and commit hashes only; add
`--subjects` to see commit subjects, which is not something to take home. A change that
could not be reviewed is listed as such, never as clean.

Read the busiest rules against what actually happened to those changes. That, not the
corpus, is the false-positive rate that matters here.

## 6. The first review

Pick a merged pull request whose outcome you already know:

```bash
themis review --project /path/to/dbt/project --base <base-sha> --head <head-sha> \
  --target <your-dev-target> --no-llm
```

Then with the model (drop `--no-llm`), then with `--execute` against a non-production
schema you can write to. Compare what it said with what the human review found.

## 7. Ask

```bash
themis agent --project /path/to/dbt/project "Which regulatory models read fct_trades?"
themis agent --project /path/to/dbt/project --base <base-sha> --head <head-sha> \
  "What did the review find in int_positions, and what changed in its SQL?"
```

The agent answers only from THEMIS's tools, and every claim quotes a tool result verbatim
or the answer is refused. `--json` shows every tool it called and every quote it relied on.

## 8. Teach it the project

- **Conventions** — what the team already knows, in `themis_conventions.yml`, versioned with
  the models: `themis conventions --project ...` checks them. Context for the specialists,
  never evidence; a claim about a key belongs in a test instead.
- **Vocabulary** — the column names that mean money or personal data here, in `.env`.
  `themis profile` shows how often the defaults match.
- **Dispositions** — `make migrate`, `make api`, then record `accepted` / `dismissed` on
  findings. They rank repeated findings, are shown to specialists as precedent, and label
  the captured model calls (`themis dataset --judged-only`).

## 9. Serve the tools to an IDE assistant (optional)

```bash
uv pip install 'themis[mcp]'
themis mcp --project /path/to/dbt/project
```

The same twelve read-only tools the built-in agent uses, over MCP stdio, so an assistant in
the IDE can investigate a change with THEMIS's evidence. **Tool results contain the SQL under
review.** MCP sends nothing anywhere itself — the client decides where results go, and a
client backed by a hosted model sends them to that provider. Connect only a local-model
client to a proprietary project. THEMIS cannot enforce that from the server side, which is
why it is said here, in `themis mcp --help`, and on startup.

### What the optional SDK brings, and what was checked

The extra is optional because a deployment that never serves MCP should not have to review
another dependency tree. Before it went into the dev requirements this is what was checked
(2026-09-18, `mcp` 2.2.0). Re-run it when the pin moves.

| question | answer |
|---|---|
| how much is new | 11 packages, and not one of them an upgrade or downgrade of something THEMIS already had — nothing in the existing tree moved version |
| known vulnerabilities | none, `pip-audit` over all 28 resolved packages |
| who published them | `mcp`, `mcp-types`, `pyjwt`, `cryptography`, `sse-starlette` and `python-multipart` carry PyPI attestations naming their GitHub repository and publishing workflow; `httpx2`/`httpcore2` (pydantic), `truststore` (vendored by pip) and `cffi`/`pycparser` do not |
| does it phone home | no. OpenTelemetry arrives as the API only — no SDK, no exporter package — so its spans are no-ops. A test serves a whole session under an audit hook and asserts the process resolved no host and opened no connection |
| what reaches the network | nothing from the server: stdio only, down the client's own pipe |

```bash
uv pip compile <(echo 'mcp>=2.2,<3') -o resolved.txt   # exactly what would be installed
uvx pip-audit -r resolved.txt                          # against the OSV database
```

## 10. The pages: one link for everyone outside Jenkins

Jenkins runs the review; the pages are where a reviewer, a lead or a manager reads it —
the overview across pull requests, one pull request's findings with what building both
revisions measured, a decision on each finding, and an assistant that answers questions
about the change from THEMIS's own tools.

```bash
make migrate          # the schema, on the Postgres the worker writes to
themis serve          # 127.0.0.1:8040 — put the sign-in proxy in front of it
```

| setting | what it does |
|---|---|
| `THEMIS_DATABASE_URL` | the Postgres the worker writes and the pages read |
| `THEMIS_LLM_BASE_URL` | the GPU host running Ollama, e.g. `http://gpu-host:11434`. The chat and the status light in the top corner both read it |
| `THEMIS_LLM_SUPERVISOR_MODEL` | the model the chat asks; it must be pulled on that host |
| `THEMIS_UI_TRUSTED_USER_HEADER` | the header the sign-in proxy sets to the person's name, e.g. `X-Forwarded-User`. With it, every decision carries the signed-in name and a typed name is refused. Without it the pages are the demo, and say so |
| `THEMIS_UI_BRAND_NAME`, `THEMIS_UI_BRAND_SUBTITLE`, `THEMIS_UI_LOGO_URL` | the organisation's name and logo. Settings only — this repository is public and carries no organisation's branding |
| `THEMIS_FAIL_ON_SEVERITY` | the threshold behind "Blocking". The same one the CLI's exit code uses, so the page and the merge check cannot disagree |
| `THEMIS_API_TOKEN` | bearer token for the JSON API (queueing reviews). The pages do not use it |

**Bind to loopback behind the proxy.** The trusted header names the person, so anything that
reaches the port without passing the proxy could name anyone. `themis serve` refuses to bind
beyond loopback while the header is configured, unless `--trust-network` says the network
already guarantees it.

**What the proxy must allow.**

- The chat streams its answer (server-sent events on `POST /ui/pr/<key>/chat`). The response
  carries `X-Accel-Buffering: no` for nginx; any other proxy needs buffering off on that path
  and a read timeout of at least five minutes — a model on a busy GPU takes its time.
- The pages load nothing from anywhere else — no CDN, no web fonts — and send a
  content-security policy that forbids inline script, inline style and framing. A proxy that
  injects its own script into pages (some sign-in banners do) will find it blocked; the
  browser console says so.

**First time on the server.**

1. `curl http://127.0.0.1:8040/health` — `"database": true`.
2. Open `/ui`. The light in the top-right corner: green, the model is loaded and answering;
   amber, the GPU host answers but the model is not pulled; red, the host is down (the pages
   still work, the chat does not).
3. Record a decision on any finding, then open *Decision record*: it should carry your
   signed-in name, not "Guest".
4. Ask the assistant one of the suggested questions. Reviews stored before this version
   have no project snapshots and the assistant says so; every review stored from now on
   keeps them.

To see the pages with something in them before any real review exists,
`python scripts/seed_demo.py` fills an empty database with eight real reviews of changes
to the demo project, under invented pull-request titles on a host that does not exist.

Press <kbd>?</kbd> on any page for the keyboard shortcuts: <kbd>⌘K</kbd> searches,
<kbd>j</kbd>/<kbd>k</kbd> step through findings, <kbd>g</kbd> then <kbd>o</kbd>/<kbd>p</kbd>/<kbd>d</kbd>
changes page, <kbd>t</kbd> switches light and dark.

## What was fixed because a real project would have hit it

| on a real project | what happened | now |
|---|---|---|
| profile in `~/.dbt` | first compile failed: "Could not find profile" | resolved in dbt's own order |
| `current_timestamp`, `{{ run_started_at }}`, `{{ invocation_id }}` audit columns | a comment-only change reported as an unexplained change | masked at compile, detected from the SQL, left out of row comparison and named |
| a prompt over ~2,048 tokens | Ollama silently dropped its beginning; the model answered half a question | the context window is always requested; an overflow is refused, not answered |
| a key column with NULLs, or a large table | the paired-row join was unhashable on Trino — 66s for 200k rows | plain equality; a NULL key is refused with the reason |
| 3,000 models | whole-project lineage took 24s | one pass per model, identical graph, half the time |
| a dbt version other than this project's | unknown — the answer was a reading of dbt's changelog | verified against real 1.8, 1.9, 1.10 and 1.12 manifests, field for field; an unverified schema version warns rather than failing |
| every staging model reading `{{ source(...) }}` | untested — the demo project has no source at all | a real compiled source-rooted project is in the test suite |
| a refactor touching fifty models | hundreds of findings, each a model call, an hour of reviewing | bounded by `THEMIS_LLM_MAX_FINDINGS_REVIEWED` (60), spent worst-first and counted |
| models with `+schema:`, an alias, or a catalog of their own | measured where a guess put them — absent on both sides, reported as nothing moved | each build reads where dbt put every model from its own manifest |
| a Hive incremental model using `delete+insert` | builds once, fails every run after: Hive refuses row-level deletes | the demo is written for partition overwrite, CI runs it twice, and F5008 flags a filter that overwrites part of a partition |
| `password: "{{ env_var('...') }}"` in the profile | THEMIS's own client sent the template text as the password | rendered as dbt renders it; an unset variable is named |
| a login other than a password (JWT, Kerberos, certificate) | THEMIS could not log in, every query failed quietly, and a review read as "nothing moved" | logs in through dbt-trino's own credentials; a failed login stops measurement and marks the review incomplete |
| a changed dbt snapshot | reported as not analysed; a model reading one had no edge to it | reviewed as history (F9001–F9007, F8007); a reader is keyed per version unless it takes one |
| a snapshot's legacy `target_schema`, or a `generate_schema_name` that ignores the target, with other models `ref()`-ing it | base and head would have written the same table, in a schema never dropped; then, guarded, every change below a snapshot went unmeasured | dbt says where every node would go; a fixed node the change does not reach is read where it is, one it does reach is left out with its readers (X0007), and the rest is measured |
| a snapshot keyed by `a \|\| '\|' \|\| b` | read as text, a key that identifies a row looked like one that does not | read as the columns it is made of |
| an Iceberg timestamp copied into a Hive table | Trino refuses: Iceberg keeps microseconds, this Hive catalog milliseconds | the demo narrows it to `timestamp(3)`; worth knowing when the SCD data lands |
| schema names kept in a scheduler's `env.config.ini`, per environment | THEMIS ran dbt without them: the first compile would fail on a variable only Dagster sets, or compile against a default | `THEMIS_DBT_ENV_CONFIG` + `THEMIS_DBT_ENV_SECTION` hand dbt one non-production section as `--vars` and environment; `doctor` names any value still missing |
| a comment written at the reviewer | an AI reviewer quoting it would be quoting honestly, and the self-check would pass it | reported as F7004, and the model that carries it is kept away from every seat that could refute a finding |
