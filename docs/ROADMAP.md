# Roadmap

## Built

- **Stage 0 — Acquire.** Git worktree diffing against the merge base; dbt runner with a
  fail-closed target allowlist; manifest loader; three grounding backends, with the
  compiled manifest as the primary target.
- **Stage 1 — Analyze.** Trino parsing, semantic AST diff (reformatting yields nothing),
  the grain lattice, transitive macro impact, blast radius.
- **Stage 2 — Rules.** F1 fan-out, join-type flips, grain changes; F3 money precision.
  Skipped checks are reported rather than hidden.
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

**M4 — review.** Context packer, specialists, supervisor, self-check. The deliverable
is a number: what the language model adds over the deterministic baseline. A negative
result is a real result.

**M5 — follow-up.** Persisted run artifacts and grounded Q&A, including "why was this
not flagged?". Answers come only from the artifact; an unanswerable question gets a
refusal rather than an inference.

**M6 — cost.** Triage rubric, feature extraction, token accounting, SARIF output.

## Deferred

CI-platform wrappers, warehouse key profiling, and value-level data diffing are gated
on a measurement from M3 or M4 rather than an assumption about what will help.
