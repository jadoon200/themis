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

## Local model characterisation

Measured on this machine (Apple silicon, Ollama), warm, `temperature=0`, with Ollama's
JSON-schema structured output. Both models return schema-valid JSON, so the specialist
design is viable in principle.

| Model | Warm latency | Throughput | Verdict on the fan-out case |
|---|---|---|---|
| `qwen3:8b` | 5.1 s | 24.7 tok/s | `uncertain` / medium — hedged |
| `qwen3:30b` | 86.8 s | 2.0 tok/s | `confirm` / high — correct, accurate reasoning |

Two things follow, and neither is comfortable.

**The larger model is right and the smaller one is not**, on exactly the defect class
this tool exists to catch. If that holds up, the planned tiering — a small model for
high-volume specialist calls, a large one only for the supervisor — puts the quality
where it is needed least.

**2.0 tok/s makes `qwen3:30b` impractical at volume here.** At roughly 87 s per call, a
supervisor pass over ten findings is a fifteen-minute wait. The throughput suggests the
18 GB model does not sit comfortably in memory on this machine; office hardware may
differ, but the local profile has to assume it does not.

**This is a signal, not a verdict.** It is a single zero-shot prompt with no evidence
pack, no rulebook and no few-shot examples — precisely the grounding the specialist
design supplies. A small model given a tight context pack and one narrow question is a
very different proposition from one asked to reason from scratch. Establishing which of
those holds is the entire point of M4, and it needs the harness rather than one prompt.

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
