# THEMIS

Automated review of dbt model changes, for SQL that transforms financial data.

A PR that touches a dbt model currently needs a human to read the whole diff and work
out what it does to the numbers. That review is slow, inconsistent, and its failure
mode is silent: a join that fans out, a `DECIMAL` quietly cast to `DOUBLE`, an
`is_incremental()` guard dropped. Nothing errors. The numbers are simply wrong, and
often nobody notices until a reconciliation break.

THEMIS reads the diff between two revisions and produces a ranked, evidence-cited
review — every finding naming the line, stating the consequence in money terms, and
tracing back to an AST node, a lineage edge, or a measured row count.

## How it works

A funnel, not an agent loop. Most of the value is deterministic and free; the language
model is reserved for judgement, and never produces facts of its own.

```
0  ACQUIRE   git diff + compiled manifests   →  ProjectSnapshot before/after
1  ANALYZE   AST, semantic diff, lineage,    →  Facts
             derived grain, macro impact
2  RULES     rule families, recall-first     →  Findings
3  EXECUTE   build both revisions, diff      →  measured evidence
             the actual results
4  TRIAGE    what is worth a model call
5  REVIEW    supervisor + specialists        →  adjudicated findings
6  REPORT    Markdown / SARIF / JSON
7  ASK       grounded follow-up questions
```

Stages 0–4 and 6 need no model at all, and `--no-llm` is a fully useful mode —
detection is entirely the rules' work. The model is kept for the three jobs no rule can
do: reading the author's description against what the SQL actually does (it catches 6
of 6 descriptions that misstate the change), naming a cause for a measured movement no
rule anticipated, and writing the corrected SQL. It has never suppressed a finding, and
the report says so.

### Worth calling out

**Grain is derived, not read.** Fan-out detection normally rests on declared
uniqueness tests. Real projects frequently have none, so THEMIS derives each model's
grain from the SQL itself — `GROUP BY`, `SELECT DISTINCT`, and `ROW_NUMBER()` dedup
patterns are proof, not inference — then propagates it through the DAG and, with
`--execute`, measures it. Anything it cannot establish is marked unknown and escalated
rather than assumed safe.

**Macro edits are analysed as the N-model change they are.** A PR touching one
`macros/*.sql` file can change the behaviour of forty models. THEMIS resolves the call
sites and diffs the compiled SQL of every affected model, so the review reflects the
real blast radius rather than the one-file diff.

**Over-flagging is paid for in ranking, never in silence.** The rules are written for
recall, so a triage stage demotes a finding that a more specific rule already covers —
"a predicate changed" beneath "the `is_incremental()` guard was removed" — with the
relationship named and nothing deleted. The score behind the ranking prints its own
components, because an opaque number gating a merge is not a reviewable statement.

**Totals are not enough, so rows are paired.** A change can move money between accounts,
entities or treatments while every row count and every total stays exactly where it was —
flipping how uncontracted revenue is recognised did, and an aggregate-only comparison called
it clean. Stage 3 pairs the base and head rows on the derived grain and counts what changed,
column by column. The projects this is for declare no keys, so it pairs only on a key it has
*counted* unique in both builds; an inferred key is never trusted to pair rows.

**What reviewers decide changes the next review — visibly, and only in the order.** Mark a
finding dismissed and the next run that raises it says so on the finding, ranks it lower,
and shows the specialist how the same rule was ruled on before. Nothing is deleted, no
weights move, and two guards stop the obvious failure of a tool that learns to go quiet:
one dismissal moves nothing, and a finding execution *measured* is exempt. Every model call
is kept with the exact context it was shown — `themis dataset` exports it, joined to the
human judgement, which is the only honest basis a tuned model could ever have.

**Deriving grain costs precision, not recall — measured.** Running the same corpus
against a variant of the demo project that declares its keys: recall is 100% either
way, and one of the four safe-but-flagged changes stops being flagged — a join onto a
dimension whose key the declared test proves. Every defect is caught without declared
tests; what they buy is fewer flags on safe changes, and only the kind a key can settle.

**The derived grain is handed back as tests.** Because THEMIS works out each model's
key without being told, it can emit the assertions the project never wrote — and it
refuses to emit any it cannot stand behind, so a suggested test does not turn red on
first run. On the demo project it offers seven and all seven pass.

```bash
themis suggest-tests --project demo_project --yaml
```

**Impact is answered per column, not per model.** "Fourteen models are downstream"
over-states almost every change, because thirteen of them never touch the column that
moved. THEMIS derives each model's real column list — expanding `select *` against the
schema it built from the models above — then traces every column back through CTEs and
renames to what it actually reads. So `revenue_usd` in the regulatory mart is known to
be `fct_revenue.amount_usd` under another name, four hops from where it started. A
model whose lineage cannot be resolved is reported as unknown, never as clean.

```bash
themis lineage --project demo_project --model stg_fx_rates --column rate
```

## Quick start

```bash
make env && conda activate themis
make install
make demo-build          # seeds and builds the demo project on DuckDB
make review              # review the working tree against main
```

On a real project, start with `themis init` and `themis doctor` — they write the
configuration and check everything a review needs, printing the fix for whatever is
missing. [docs/WORK_SETUP.md](docs/WORK_SETUP.md) is the step-by-step guide.

### Ask the agent

```bash
themis agent "Which regulatory models read fct_revenue.amount_usd?"
themis agent --base main --head HEAD "What did the review find, and what changed in the SQL?"
```

A local model investigates by calling THEMIS's own tools — grain, lineage, what is
downstream, the findings, the SQL diff, what execution measured — and every claim in its
answer must quote a tool result verbatim, or the answer is refused. It chooses which fact
to fetch; it never produces one. On held-out questions it answered 5 of 5 and refused the one
no tool could answer; on the questions it was tuned against, 12 of 14 with one incomplete
answer — every run, including the ones that went backwards, is in docs/EVAL.md. The same tools are served to any MCP client by
`themis mcp` (optional extra; tool results contain SQL, so connect only local-model clients
to proprietary projects).

Findings land on the diff, not only in the log — and in a form a gate or a dashboard
can read, including the measured deltas, the derived grain, and the checks that could
not run:

```bash
themis review --base main --head HEAD --sarif themis.sarif --json themis.json
```

`--head HEAD` reviews the working tree, uncommitted files included. Any other revision is
compiled and built from that commit rather than from whatever happens to be checked out,
so a CI job can name the pull request's SHA from any checkout.

Advisory by default. Set `THEMIS_FAIL_ON_SEVERITY=high` and the exit code gates a merge:
`1` for a finding at or above it, and `3` when the review itself is incomplete — checks
skipped, grounding degraded, or execution asked for and not run — because a gate that
passes a review nobody finished is not a gate.

Stage 3 builds both revisions to measure what actually moved — each into schemas of its
own, dropped afterwards, and measuring only the models dbt reports as built, so a failed
build is reported as one rather than read as a result. On a project whose ancestor
closure is large, point it at a manifest from an existing build and the unchanged
upstreams are read where they already are instead of being rebuilt twice:

```bash
themis execute --base main --head HEAD --defer-state path/to/prod/target
```

A full review can take the same production artifacts twice over — the base read from
the prod manifest rather than recompiled from git, and Stage 3 deferring to the same
relations:

```bash
themis review --prod-manifest path/to/prod/target --defer-state path/to/prod/target --execute
```

If the manifest turns out to be missing or unreadable, the review says so and rebuilds
the base from git. It does not quietly answer a different question than the one asked.

Everything runs locally and costs nothing: DuckDB as the warehouse, Ollama for the
model. No warehouse credentials, no API keys, no paid dependency.

Calibrating on a project whose code cannot be shared: `themis profile` describes it in
counts — how much of its SQL parses, how much grain and lineage resolve, how often the
configured names match — and `--redact` writes SARIF and JSON with no SQL, no measured
values and hashed names. The names checks match on (money columns, personal-data columns,
reporting tags, published folders) are settings, e.g.
`THEMIS_MONEY_COLUMN_HINTS='["amount","ntnl","mtm"]'`.

The service (`make api`, `make worker`) binds to the loopback interface. A review request
runs dbt on the project it names, so before exposing it set `THEMIS_API_TOKEN` and
`THEMIS_PROJECT_ROOTS`, and run `make migrate` after upgrading.

What people decide about findings is the one input a review cannot derive for itself.
Dispositions are recorded through the API (`POST /findings/{id}/disposition`), and from
then on they rank, they are shown to specialists as precedent, and they label the captured
model calls:

```bash
themis dataset --judged-only
```

That prints how many captured calls carry a human judgement — the number that decides
whether tuning a model is worth doing yet, which today it is not. Add `--out calls.jsonl`
to export them; the file contains the SQL the model was shown, so treat it like the repo.

A team can also write down what it already knows about its project — FX rates are one row
per currency per month, amounts are in minor units — in `themis_conventions.yml`, versioned
with the models. Specialists read the conventions that apply to a finding as context, never
as evidence:

```bash
themis conventions --project path/to/project
```

Several of these ideas came from reading how other tools work — Recce, dbt-audit-helper,
SQLMesh, Semgrep, Alibaba's Open Code Review. `docs/PRIOR_ART.md` records what was adapted,
what was deliberately not, and why.

To check that every part actually runs — not a stand-in for it — with Postgres (`make up`),
Ollama and a Trino on port 8085 available:

```bash
python scripts/component_check.py
```

47 checks from a throwaway worktree: the CLI, five scenario reviews, exit codes, reports,
execution, persistence, `ask`, the API and a worker, Trino, and the corpus. `--quick` skips
the model, Trino and the corpus.

## Dialect

SQL is parsed as **Trino** (Starburst), independently of what executes it. The demo
project runs on DuckDB purely so results can be compared cheaply — THEMIS itself never
executes SQL during analysis.

## Status

A proof of concept. See `docs/ROADMAP.md` for what is built and what is next, and
`docs/EVAL.md` for the measurements, including where THEMIS does worse than it looks
like it should. On the 46-case corpus: **100% recall, all 29 rules firing, every
behaviour-preserving control silent** — and CI fails if any of that stops being true.
Four of four deliberately safe changes are still flagged (recall-first, by design),
which puts precision at 82% and the false-positive rate at 36%; those two figures move
with how many safe cases the corpus holds, so the four-of-four is the one to read.

That CI gate is recent. Until September 2026 the corpus job ran against a project it had
not built, measured 9 of 29 rules, and passed — `docs/EVAL.md` records what else a review
of the review found.

## Licence

MIT.
