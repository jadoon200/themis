# Roadmap

## Built

- **Stage 0 — Acquire.** Git worktree diffing against the merge base; dbt runner with a
  fail-closed target allowlist; manifest loader; three grounding backends, with the
  compiled manifest as the primary target.
- **Stage 1 — Analyze.** Trino parsing, semantic AST diff (reformatting yields nothing),
  the grain lattice, transitive macro impact, blast radius, and **column-level
  lineage** — schemas derived in dependency order so `select *` resolves, then every
  output column traced back through CTEs and renames to the relations it reads.
  Join keys and filter predicates are collected alongside, because a column can break
  a model while contributing nothing to its output.
- **Stage 2 — Rules.** 29 rules across eight families: grain and fan-out, filters and
  NULL semantics, money precision, periods, incremental and materialization, contracts
  and lineage, governance, and Trino engine behaviour. Plus `X0001`, the safety net
  that reports a measured change no rule accounts for. Skipped checks are reported
  rather than hidden.
- **Stage 3 — Execute.** Both revisions built and diffed on real data, with
  `--defer-state` to resolve unchanged upstreams to an existing build instead of
  rebuilding the ancestor closure twice.
- **Warehouse clients.** DuckDB and **Trino**, both tested against a live engine.
- **Report.** Ranked Markdown, macro attribution, measured deltas where present.
- **Demo project.** A financial dbt project on DuckDB — general ledger, FX conversion,
  revenue recognition, regulatory mart. Macro-using and, deliberately, test-free.

## Next

**M2 — grounding depth.** Built. Column-level lineage, the grain lattice, macro and
YAML routing, and rule families F2 through F8. Still open: the dual-manifest backend
is loadable but not exercised, missing-test suggestions derived from the grain lattice
are not emitted, and the dbt-bouncer ingest is untuned.

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

**M6 — cost.** Triage rubric, feature extraction, SARIF output. Token accounting is
done; the rest is not.

**Next.** Missing-test suggestions from the derived grain — on a project with no test
coverage, emitting the assertions THEMIS already proved is arguably worth as much as
the review. Corpus coverage for F4 and F8, which still have more rules than cases.
Deferral measured against a closure large enough for the saving to matter.

## Deferred

CI-platform wrappers, warehouse key profiling, and value-level data diffing are gated
on a measurement from M3 or M4 rather than an assumption about what will help.
