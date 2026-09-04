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

Stages 0–3 and 6 need no model at all. `--no-llm` is a fully useful mode.

### Two things worth calling out

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

**Deriving grain costs precision, not recall — measured.** Running the same corpus
against a variant of the demo project that declares its keys: recall is 100% either
way, while the false-positive rate halves, 25% to 12%. Every defect is caught without
declared tests; what they buy is not flagging the safe changes.

**The derived grain is handed back as tests.** Because THEMIS works out each model's
key without being told, it can emit the assertions the project never wrote — and it
refuses to emit any it cannot stand behind, so a suggested test does not turn red on
first run. On the demo project it offers five and all five pass.

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

Findings land on the diff, not only in the log — and in a form a gate or a dashboard
can read, including the measured deltas, the derived grain, and the checks that could
not run:

```bash
themis review --base main --head HEAD --sarif themis.sarif --json themis.json
```

Stage 3 builds both revisions to measure what actually moved. On a project whose
ancestor closure is large, point it at a manifest from an existing build and the
unchanged upstreams are read where they already are instead of being rebuilt twice:

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

## Dialect

SQL is parsed as **Trino** (Starburst), independently of what executes it. The demo
project runs on DuckDB purely so results can be compared cheaply — THEMIS itself never
executes SQL during analysis.

## Status

Early. See `docs/ROADMAP.md` for what is built and what is next, and `docs/EVAL.md`
for measured precision and recall — 100% recall, **89% precision, 25% false-positive
rate** — including the cases where THEMIS does worse than it looks like it should. The
false-positive rate read 0% until the corpus gained cases in which a rule could be
wrong; it was a property of the questions, not the answers.

## Licence

MIT.
