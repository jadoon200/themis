# Prior art: what similar tools do, and what THEMIS took from them

A survey of open-source and documented tools that review data changes, review code with a
model, or learn from reviewer feedback — read at the source where the source was
available, not from their marketing. The question for each was narrow: *is there a
mechanism here that closes a gap THEMIS actually has, without breaking a constraint it
actually holds* (local and free, the model never producing facts, nothing leaving the
machine, a regulated reader).

Three things were adapted. Nothing was copied: each is an idea reimplemented against
THEMIS's own constraints, and every project named below is Apache-2.0 or MIT.

## Adapted

### 1. Rows paired on the derived key — from Recce, dbt-audit-helper, SQLMesh and datacompy

**What they do.** [Recce](https://github.com/DataRecce/recce)'s impact analysis builds a
`FULL OUTER JOIN` of the base and current relation on the model's primary key and counts
rows added, removed and changed, per column, with `IS DISTINCT FROM`.
[dbt-audit-helper](https://github.com/dbt-labs/dbt-audit-helper)'s `compare_all_columns`
reports, per column, perfect matches, nulls on either side, rows missing from either
side and conflicting values. [SQLMesh](https://github.com/SQLMesh/sqlmesh)'s `table_diff`
joins on the model's declared `grain` and refuses when that grain is not unique and
non-null. Capital One's [datacompy](https://github.com/capitalone/datacompy) makes the
point that float columns need a tolerance, per column.

**The gap it closed.** THEMIS measured aggregates only — row counts, monetary sums, null
rates — and the corpus oracle asked the same question. Money moving *between* keys leaves
every row and every total where it was. A new corpus case proved it: flipping the default
revenue-recognition treatment for uncontracted entries came back as a **clean review**.
No rule fires, and the safety net only fired on movements an aggregate could see.

**How THEMIS differs.** Every one of those tools pairs on a key someone *declared*. The
projects THEMIS is for declare none, so it pairs on the **derived grain** — but only once
Stage 3 has counted it unique in *both* builds. The derivation proposes, the count
decides, and pairing never rests on an inference. The join is plain equality, as Recce's
is, and a key containing NULLs is refused. The first version used `IS NOT DISTINCT FROM` so
NULL keys would pair; both engines accepted it and every test passed, but Trino plans a
null-safe comparison as a join *filter* rather than hash criteria — 200,000 rows took 66
seconds against 0.2 with `=`, growing with the square of the table. Numeric columns compare
with a relative tolerance so reordered arithmetic is not a change; load-metadata columns are
skipped by name and the report says which. Key values are kept as examples for the reviewer
and never enter a redacted report.

**Result.** The recognition case is now caught by X0001, with the evidence
"fct_revenue: paired on (entry_id): 21 row(s) changed value in recognition_method (21)" —
while every row count and every total in the project holds. One portable statement, tested
on DuckDB and on a live Trino, and the full corpus still passes its gate with every control
silent.

**What checking it against a real review found.** The corpus scored the case as caught
before the report had been read. Reading it found three defects in X0001 that no earlier
case had exercised, all fixed: the model whose SQL changed was described as having
*unchanged* SQL (it is a view with no countable key, so its own rows could not be paired);
its evidence showed an unchanged row count and not the table where the values moved; and it
was reported **critical**, naming three regulatory marts under "a reported figure moved"
when none of them reads the column that changed — reachability had been standing in for
movement. Critical now requires a governed model measured to move.

### 2. Findings placed on their line — from Alibaba's Open Code Review

**What it does.** [Open Code Review](https://github.com/alibaba/open-code-review) names a
failure it calls *position drift* — a model's reported line not matching the code it is
describing — and moves positioning out of the model into a deterministic module that
finds the quoted code.

**The gap it closed.** No THEMIS rule set a line number. Every SARIF annotation — the thing
a pull request shows inline — landed on line 1 of the file.

**How THEMIS differs.** The quote is not a model's; it is the fragment a rule recorded,
in compiled SQL, which has to be found in a raw Jinja file. Positioning matches the
fragment's distinctive identifiers against windows of the file, ignoring comments,
stripping database and schema qualifiers that `ref()` hides, and stepping up to a clause's
opening keyword. It **declines** below 75% coverage or on a fragment too generic to place,
because an annotation on the wrong line points a reviewer confidently at code that is fine.
Configuration findings go to the `config()` block.

### 3. Conventions written down — from Semgrep Assistant Memories

**What it does.** [Semgrep's memories](https://semgrep.dev/docs/semgrep-assistant/overview)
are statements a team writes — a condition, guidance, and what follows from it — scoped to
where they apply and used to inform triage. Their guidance is that general statements beat
notes about single findings.

**The gap it closed.** THEMIS learned from dispositions one finding at a time, needing two
judgements before anything moved. A team that already knows how its project works had no
way to say so.

**How THEMIS differs.** Semgrep lets memories drive false-positive triage. THEMIS does not:
a convention reaches the specialist as context, and like past judgements it sits outside
the text a quote may be grounded in — a stale convention cannot refute a live finding on its
own say-so. Conventions live in `themis_conventions.yml` in the dbt project, so changing one
is reviewed like any other change, and a review of a commit reads *that commit's*
conventions rather than whatever is on disk. `themis conventions` refuses entries missing
any of the three parts and points claims about keys towards tests, where they can be
measured instead of believed.

## Studied and not adopted

**Change categorisation — SQLMesh and Recce.** SQLMesh's categoriser treats only added
projections as non-breaking; Recce's classifier goes further, scope by scope, into
additive / column-level / model-wide / unknown, and uses column lineage to narrow which
downstream models are really affected. It is the right idea and THEMIS has the lineage to
do it. Not built yet, for one reason: narrowing impact is the one change here whose failure
is a *silent* false negative — a model wrongly marked "not affected" is never built or
measured. Recce's own code carries a loud-fail path for exactly this (an unresolvable
CTE change forces `unknown`). It should come with a corpus of cases proving it never
narrows wrongly, not before one. **Next.**

**Model self-scoring — PR-Agent.** PR-Agent has the model score its own suggestions 0–10
and drops low scores. THEMIS already refuses answers it cannot verify, and a model grading
itself measures agreement with itself; a verified quote measures something outside the model.

**Verification scripts written by the model — CodeRabbit.** CodeRabbit has the model write
shell or `ast-grep` checks to confirm a claim before posting. Letting a model write the
query that settles a question is the model producing the fact by another route. The safe
form — a specialist *choosing* from a fixed menu of deterministic measurements THEMIS
already knows how to run — is plausible later.

**Embedding-similarity feedback — Greptile.** Greptile filters comments by their embedding
distance to comments a team upvoted or ignored, and reports addressed-comment rates rising
from 19% to over 55%. THEMIS retrieves precedent by rule and model instead: explainable,
free, and exact about what was shown. Embeddings through a local model would be free, but
"similar" is not a statement a regulated reviewer can check. Revisit if rule-and-model
retrieval proves too narrow on real dispositions.

**SQL equivalence provers — VeriEQL, SQLSolver.** Proving a refactor equivalent would settle
benign changes outright. VeriEQL's backend is under patent evaluation, SQLSolver is a Java
research prover, and both cover a subset of SQL; building both revisions on real data
already gives THEMIS empirical evidence for the cases that matter.

**An MCP server — dbt-mcp, Recce.** [dbt-mcp](https://github.com/dbt-labs/dbt-mcp) and
Recce both expose their tools to agents, and Recce's tool results carry an explicit
`next_action` telling the agent what to measure next — a good design. Not built: an MCP
server hands THEMIS's evidence, which contains the SQL under review, to whatever client
connects, and for proprietary code that may be a hosted model. Safe only with a local client.

**Static SQL linting — Altimate Code.** Anti-pattern checks over whole files. Not
diff-aware, so on a project with no tests they fire everywhere; the same reason dbt-bouncer
was not adopted.

**Metamorphic query oracles — SQLancer.** Ternary logic partitioning checks a query against
the union of its `p`, `NOT p` and `p IS NULL` partitions to find engine bugs. The partition
is a useful way to *attribute* rows a filter change drops to NULL semantics rather than to
the predicate; the keyed comparison already counts the rows, and attribution would need the
predicate evaluated against each model's upstream. Noted, not built.

## Sources

- DataRecce/recce — `recce/util/change_classifier.py`, `recce/mcp_server.py` (Apache-2.0)
- dbt-labs/dbt-audit-helper (Apache-2.0)
- SQLMesh — table diff guide; Tobiko Data, "Automatically detecting breaking changes in SQL queries"
- capitalone/datacompy (Apache-2.0)
- alibaba/open-code-review — `internal/tool/code_comment.go` (Apache-2.0)
- Semgrep — Assistant Memories documentation and best practices
- qodo-ai/pr-agent — self-reflection
- CodeRabbit — agentic code validation
- Greptile — embedding-based comment filtering
- AltimateAI/altimate-code (MIT)
- dbt-labs/dbt-mcp
- sqlancer/sqlancer; VeriEQL; SJTU-IPADS/SQLSolver
