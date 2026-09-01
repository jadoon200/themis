# Evaluation

Numbers here come from running THEMIS, not from reasoning about it. Where it does
worse than it looks like it should, that is recorded rather than dropped — a reviewer
tool whose limits are undocumented is one whose clean results cannot be trusted.

Full precision and recall arrive with the mutation harness (M3). What follows is what
is measurable today.

## Grain derivation coverage

The target project declares no uniqueness tests, so grain is derived from the SQL. On
the demo project (9 models, 4 seeds):

| Source | Models | |
|---|---|---|
| `structural` | 1 | proven from the AST — `GROUP BY` inside a CTE |
| `heuristic` | 3 | column naming only; never treated as proof |
| `unknown` | 5 | plus 4 seeds, whose grain cannot be derived from SQL at all |

**This is the most important early result, and it is not a good one.** Structural
derivation alone resolves 1 of 9 models. The reason is structural rather than
incidental: most models in a dbt project are projections over an upstream — no
`GROUP BY`, no `DISTINCT`, no dedup — so there is nothing in their own SQL from which
uniqueness follows. Propagation helps only where the upstream is itself proven, and
the chains here terminate at seeds.

Consequences, taken honestly:

- The fan-out family currently runs mostly at `possible` rather than `likely`
  confidence. It still fires — weak grain never suppresses a finding — but it cannot
  say much about how likely a given fan-out is.
- `stg_fx_rates` is derived as unique on `(currency_code)`. **That is wrong**; the real
  grain is `(currency_code, rate_date)`. This is precisely why `heuristic` is excluded
  from `is_proven` and can never suppress a finding. Had it been trusted, the flagship
  fan-out bug would have been silently dismissed as safe.
- It is a direct argument for Stage 3 measurement. `count(*)` versus
  `count(distinct k)` settles in one cheap query what inference cannot settle at all.

## What is verified today

End-to-end against the demo project, both scenarios reproducible from a clean `main`:

| Scenario | Result |
|---|---|
| FX join loses its period predicate | Flagged `high`, names `stg_fx_rates`, blast radius includes the regulatory mart |
| `money()` macro switched to `DOUBLE` | **2 critical findings across 2 models with zero model files changed** — the macro edit is expanded to its real reach and attributed back to the macro |
| Pure reformatting | No findings — semantic AST diff, not text diff |

The macro case is the one a text diff cannot do at all: the PR touches a single file,
and the review correctly covers every model whose compiled SQL changed.

## Known limitations

- **Backend C (raw files) is close to blind** against macro-heavy projects. Jinja is
  unexpanded, so the parsed AST is not the SQL that runs. A compiled manifest is
  required in practice, and `dbt parse` is not enough — only `dbt compile` populates
  `compiled_code`.
- **No execution evidence yet.** Every finding is inferred. Stage 3 is what turns
  "may fan out" into a measured row-count delta.
- **DuckDB is not Trino.** The demo project stays inside the dialects' intersection.
  Analysis always parses as Trino regardless of what executes, and nothing is executed
  during analysis.
- **No false-positive rate yet.** The refactor control set arrives with M3. Until then
  the FP rate is unmeasured, which means unknown rather than low.
