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

## What was fixed because a real project would have hit it

| on a real project | what happened | now |
|---|---|---|
| profile in `~/.dbt` | first compile failed: "Could not find profile" | resolved in dbt's own order |
| `current_timestamp`, `{{ run_started_at }}`, `{{ invocation_id }}` audit columns | a comment-only change reported as an unexplained change | masked at compile, detected from the SQL, left out of row comparison and named |
| a prompt over ~2,048 tokens | Ollama silently dropped its beginning; the model answered half a question | the context window is always requested; an overflow is refused, not answered |
| a key column with NULLs, or a large table | the paired-row join was unhashable on Trino — 66s for 200k rows | plain equality; a NULL key is refused with the reason |
| 3,000 models | whole-project lineage took 24s | one pass per model, identical graph, half the time |
