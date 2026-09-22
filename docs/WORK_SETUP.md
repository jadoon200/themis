# Setting THEMIS up on a real project

Written for the first run on a project nobody here has seen — a work dbt project on Trino
(Starburst), with its profile in `~/.dbt`, no declared tests, heavy macros, and audit
columns on most models. Every step below exists because the naive version of it failed.

Nothing here sends anything off the machine. The model runs locally through Ollama; the
warehouse is only ever reached through a target you allow.

## 1. Install

```bash
conda create -y -n themis python=3.12 pip && conda activate themis
pip install uv && uv pip install -r requirements.txt && uv pip install -e .
```

`uv`, not `pip` — pip spends ten minutes backtracking on dbt-core. For Starburst/Trino add
the adapter, which is not in the base dependencies:

```bash
uv pip install "dbt-trino>=1.10,<1.11"
```

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

## 3. Check

```bash
themis doctor --project /path/to/dbt/project --target <your-dev-target>
```

Checks Python, dbt, the adapter your target uses, where the profile is (the project,
`DBT_PROFILES_DIR` or `~/.dbt`, in dbt's order), the allowlist, git, the compiled manifest,
the local model and its context window, the database and its migrations, conventions and
the redaction salt — and prints the command that fixes each. Fix every `FAIL`.

## 4. Measure the project before trusting anything on it

```bash
(cd /path/to/dbt/project && dbt compile --target <your-dev-target>)
themis profile --project /path/to/dbt/project --json
```

Counts only — no SQL, no names. How much of the SQL parses as Trino, how deep CTEs go, how
far macros reach, how much grain and lineage resolve, and how often the vocabulary matches.
This is the file that is safe to share, and the one that says whether THEMIS's assumptions
hold here.

## 5. The first review

Pick a merged pull request whose outcome you already know:

```bash
themis review --project /path/to/dbt/project --base <base-sha> --head <head-sha> \
  --target <your-dev-target> --no-llm
```

Then with the model (drop `--no-llm`), then with `--execute` against a non-production
schema you can write to. Compare what it said with what the human review found.

## 6. Ask

```bash
themis agent --project /path/to/dbt/project "Which regulatory models read fct_trades?"
themis agent --project /path/to/dbt/project --base <base-sha> --head <head-sha> \
  "What did the review find in int_positions, and what changed in its SQL?"
```

The agent answers only from THEMIS's tools, and every claim quotes a tool result verbatim
or the answer is refused. `--json` shows every tool it called and every quote it relied on.

## 7. Teach it the project

- **Conventions** — what the team already knows, in `themis_conventions.yml`, versioned with
  the models: `themis conventions --project ...` checks them. Context for the specialists,
  never evidence; a claim about a key belongs in a test instead.
- **Vocabulary** — the column names that mean money or personal data here, in `.env`.
  `themis profile` shows how often the defaults match.
- **Dispositions** — `make migrate`, `make api`, then record `accepted` / `dismissed` on
  findings. They rank repeated findings, are shown to specialists as precedent, and label
  the captured model calls (`themis dataset --judged-only`).

## 8. Serve the tools to an IDE assistant (optional)

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

## 9. The pages: one link for everyone outside Jenkins

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
| a comment written at the reviewer | an AI reviewer quoting it would be quoting honestly, and the self-check would pass it | reported as F7004, and the model that carries it is kept away from every seat that could refute a finding |
