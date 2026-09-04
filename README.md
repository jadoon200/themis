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

Stage 3 builds both revisions to measure what actually moved. On a project whose
ancestor closure is large, point it at a manifest from an existing build and the
unchanged upstreams are read where they already are instead of being rebuilt twice:

```bash
themis execute --base main --head HEAD --defer-state path/to/prod/target
```

Everything runs locally and costs nothing: DuckDB as the warehouse, Ollama for the
model. No warehouse credentials, no API keys, no paid dependency.

## Dialect

SQL is parsed as **Trino** (Starburst), independently of what executes it. The demo
project runs on DuckDB purely so results can be compared cheaply — THEMIS itself never
executes SQL during analysis.

## Status

Early. See `docs/ROADMAP.md` for what is built and what is next, and `docs/EVAL.md`
for measured precision and recall — including the cases where THEMIS does worse than
it looks like it should.

## Licence

MIT.
