# Roadmap

## Built

- **Stage 0 — Acquire.** Git worktree diffing against the merge base; dbt runner with a
  fail-closed target allowlist; manifest loader; three grounding backends, with the
  compiled manifest as the primary target.
- **Stage 1 — Analyze.** Trino parsing, semantic AST diff (reformatting yields nothing),
  the grain lattice, transitive macro impact, blast radius.
- **Stage 2 — Rules.** 26 rules across eight families: grain and fan-out, filters and
  NULL semantics, money precision, periods, incremental and materialization, contracts
  and lineage, governance, and Trino engine behaviour. Plus `X0001`, the safety net
  that reports a measured change no rule accounts for. Skipped checks are reported
  rather than hidden.
- **Warehouse clients.** DuckDB and **Trino**, both tested against a live engine.
- **Report.** Ranked Markdown, macro attribution, measured deltas where present.
- **Demo project.** A financial dbt project on DuckDB — general ledger, FX conversion,
  revenue recognition, regulatory mart. Macro-using and, deliberately, test-free.

## Next

**M2 — grounding depth.** Column-level lineage; dual-manifest backend; missing-test
suggestions derived from the grain lattice; tuned dbt-bouncer ingest. Rule families F2
(NULL semantics), F4 (periods and point-in-time), F5 (incremental), F6 (contracts),
F8 (Trino cost).

**M3 — execution.** Build base and head, then diff the real results: row counts,
monetary sums, column sets, null rates. The largest remaining jump in usefulness — it
turns inference into measurement, and settles the grain question that
[EVAL](EVAL.md) shows inference alone cannot. The mutation harness and the first
precision and recall figures build on the same machinery.

**M4 — review.** Built. Four specialists, an intent pass, self-check, and an explain
pass for measured changes no rule accounts for. The deliverable was a number and
[EVAL](EVAL.md) has it.

**M5 — follow-up.** Built. Persisted runs and grounded Q&A, including "why was this not
flagged?", answered from persisted absence. An unanswerable question gets a refusal.

**M6 — cost.** Triage rubric, feature extraction, SARIF output. Token accounting is
done; the rest is not.

**Next.** Recorded cassettes so the model path is covered in CI; Postgres in CI so the
`SKIP LOCKED` queue is guarded; corpus coverage for F4, F6 and F8, which have more
rules than cases; `themis review` persisting to the store.

## Deferred

CI-platform wrappers, warehouse key profiling, and value-level data diffing are gated
on a measurement from M3 or M4 rather than an assumption about what will help.
