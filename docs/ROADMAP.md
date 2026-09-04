# Roadmap

## Built

- **Stage 0 — Acquire.** Git worktree diffing against the merge base; dbt runner with a
  fail-closed target allowlist; manifest loader; three grounding backends, with the
  compiled manifest as the primary target and `--prod-manifest` reading the base from a
  production build instead of recompiling it. A backend weaker than the one asked for is
  named in the report rather than substituted quietly.
- **Stage 1 — Analyze.** Trino parsing, semantic AST diff (reformatting yields nothing),
  the grain lattice, transitive macro impact, blast radius, and **column-level
  lineage** — schemas derived in dependency order so `select *` resolves, then every
  output column traced back through CTEs and renames to the relations it reads.
  Join keys and filter predicates are collected alongside, because a column can break
  a model while contributing nothing to its output.
- **Suggested tests.** The derived grain emitted as `schema.yml` assertions, with a
  refusal policy strict enough that a suggestion does not fail on first run — and
  measured to be worth accepting: declaring the keys halves the false-positive rate.
- **Tested-vs-testless measurement.** `themis eval --variant tested` merges declared
  keys into the demo project and reruns the corpus, which is how the cost of deriving
  grain rather than reading it is finally a number rather than an expectation.
- **Stage 2 — Rules.** 29 rules across eight families: grain and fan-out, filters and
  NULL semantics, money precision, periods, incremental and materialization, contracts
  and lineage, governance, and Trino engine behaviour. Plus `X0001`, the safety net
  that reports a measured change no rule accounts for. Skipped checks are reported
  rather than hidden.
- **Stage 3 — Execute.** Both revisions built and diffed on real data, with
  `--defer-state` to resolve unchanged upstreams to an existing build instead of
  rebuilding the ancestor closure twice.
- **Manifest cache.** Compiled manifests are content-addressed by git revision, so the
  base compile a review repeats every time is paid once — and refused outright for
  projects whose SQL is built from query results, where a revision does not determine
  the output.
- **Capability-scoped workers.** Each worker declares what it may do. `execute` is off
  by default and is the only capability that reaches a warehouse with write access, so
  the analysis fleet holds no credentials to misuse. Enforced when claiming work and
  again at execution, because a guard living only in the scheduler is one a scheduling
  bug removes.
- **Warehouse clients.** DuckDB and **Trino**, both tested against a live engine.
- **Report.** Ranked Markdown, macro attribution, measured deltas where present; SARIF
  for inline annotations, carrying the same triage; and JSON for anything that is not a
  person — the measured deltas, the derived grain, and the checks that could not run.
- **Demo project.** A financial dbt project on DuckDB — general ledger, FX conversion,
  revenue recognition, regulatory mart. Macro-using and, deliberately, test-free.

## Next

**M2 — grounding depth.** Built. Column-level lineage, the grain lattice, macro and
YAML routing, missing-test suggestions derived from the grain, and rule families F2
through F8. Still open: the dual-manifest backend is loadable but not exercised, and
the dbt-bouncer ingest is untuned.

**M3 — execution.** Built. Base and head built side by side and diffed on real data:
row counts, monetary sums, column sets, null rates. It turned inference into
measurement and settled the grain question [EVAL](EVAL.md) shows inference alone
cannot. The mutation harness and the precision and recall figures run on the same
machinery.

**M4 — review.** Built. Four specialists, an intent pass, self-check, and an explain
pass for measured changes no rule accounts for. The deliverable was a number and
[EVAL](EVAL.md) has it.

**M5 — follow-up.** Built. Persisted runs and grounded Q&A, including "why was this not
flagged?", answered from persisted absence. An unanswerable question gets a refusal.

**M6 — cost.** Built, minus the classifier. Triage ranks and demotes with an
explicit, printed rubric; SARIF carries the same triage so the annotation view and the
report agree; token accounting was already done. The machine-learning lane is
**closed** rather than pending — see below.

**Next.** Deferral and the dual-manifest backend measured against a project large
enough for the saving to show as time rather than as object counts — that number has to
come from a real warehouse. A tuned dbt-bouncer ingest for the governance family.

## Measured and left alone

**Model choice and sampling.** Three models (`qwen3:8b`, `qwen3:14b`,
`qwen2.5-coder:7b`) across three parameter settings and four cases: 36 runs, **zero**
refutations of a benign finding and zero wrong refutations of a real defect. A larger
model was 36% slower and no better; a coding-specialised one was marginally faster and
no better. There is no configuration to reach for here, which is why the direction is
to make inferences sound rather than to ask a model to second-guess them.

**Agent frameworks — LangChain and LangGraph — not adopted.** The batch pipeline is a
static DAG with no cycles and no dynamic planning, and every model call in it is a
single-shot, JSON-schema'd completion: 10 to 11 of them per corpus run, none using
tools, none multi-turn. LangGraph's value is cycles, checkpointing and human-in-the-loop
interrupts; durability here is Postgres and the persisted run artifact, which already
exists. LangChain's provider abstraction duplicates a 129-line file, and its structured
output is weaker than Ollama's native constrained decoding, which the provider already
uses. Against that, the two pull tens of transitive dependencies into a project that
has 17 — every one of them a supply-chain review in a bank — and their observability
story is a hosted service, which would send the SQL under review off the machine and
break the constraint that made local inference the default in the first place.

The one genuine gap either would have filled is retry and fallback on a failed
completion, which the provider does not do. That is worth about twenty lines, not a
framework. If the follow-up lane ever grows real multi-step tool use, revisit — and
even then a plain tool-runner loop is around a hundred lines.

## Closed, with the reason

**The classifier lane.** The plan kept a logistic-regression router as a side lane. It
should not be built. There are 38 labelled mutations and roughly 30 candidate features:
any model fitted on that reports its own training set back. SQLMesh already does this
classification deterministically from AST diff and lineage with no learning at all, and
the rubric that shipped is transparent, explainable and needs no labels. A number that
looks like evidence and is not is worse here than no number.

If it is ever revisited, the precondition is real labels — approve, revert and hotfix
history from an actual repository — not more synthetic mutations.

## Deferred

CI-platform wrappers, warehouse key profiling, and value-level data diffing are gated
on a measurement from M3 or M4 rather than an assumption about what will help.
