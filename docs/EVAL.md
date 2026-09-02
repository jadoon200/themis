# Evaluation

Numbers here come from running THEMIS, not from reasoning about it. Where it does
worse than it looks like it should, that is recorded rather than dropped — a reviewer
tool whose limits are undocumented is one whose clean results cannot be trusted.

Full precision and recall arrive with the mutation harness (M3). What follows is what
is measurable today.

## Grain derivation coverage

THEMIS is built for projects that declare no uniqueness tests, so grain is derived
from the SQL rather than read. On the demo project (9 models, 4 seeds), which is
deliberately test-free for this reason:

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

## What execution measures

Stage 3 builds both revisions into side-by-side schemas against the same source data
and compares the results. On the fan-out fixture — a single dropped join predicate:

| Model | Rows | `sum(amount_usd)` |
|---|---|---|
| `int_gl_entries_converted` | 18 → 54 | 12,836,412.45 → 38,477,683.30 |
| `fct_revenue` | 15 → 45 | 13,112,347.70 → 39,318,036.05 |
| `fct_regulatory_summary` | **9 → 9** | **13,112,347.70 → 39,318,036.05** |

**The regulatory mart's row count does not move.** It is a `GROUP BY`, so its grain is
fixed and an upstream fan-out leaves the row count untouched while tripling the money.
A row-count check alone reports *no change* on precisely the table that reaches a
regulator. Summing the monetary columns is what catches it, and that is why the differ
does both.

### Grain, settled by counting

Measurement does what derivation could not:

| Fixture | `fct_revenue` |
|---|---|
| fan-out | 45 rows, 15 distinct — **3.00 rows per key** |
| control | 15 rows, 15 distinct — 1.00 rows per key |

Derivation could only offer `heuristic (entry_id)`, unproven and — for `stg_fx_rates` —
wrong. One `count(distinct)` replaces that with an exact multiplier. Where the two
disagree, the disagreement is itself reported (`F1004`), because it is the only direct
evidence of whether the lattice can be trusted on a given project.

## Mutation corpus

`themis eval` injects known changes, builds both revisions, and lets the results decide
the truth. A change that moves no number is treated as behaviour-preserving whatever it
was labelled — so the corpus labels itself, and the author's belief about what *should*
be caught never enters the scoring.

Current corpus: **13 defects, 3 latent, 1 unruled, 6 controls** across all eight rule
families.

| | |
|---|---|
| recall | **100%** (13/13) |
| precision | **100%** |
| false-positive rate | **0%** (0/6 controls flagged) |
| caught by the family designed for it | 13/13 |
| latent defects detected | 3/3 |
| unruled defects detected | 1/1 |

### The oracle has a blind spot, and it is named

Execution asks whether the numbers moved. That is the right question for most defects
and the wrong one for three kinds, which are scored separately as **latent**:

- **cost** — dropping an `is_incremental()` guard reprocesses all history and produces
  byte-identical output at many times the price;
- **lineage** — replacing `ref()` with a literal name reads the same table today while
  removing the DAG edge that guarantees build order and keeps development out of
  production;
- **not yet triggered** — narrowing a late-arrival window loses nothing until something
  actually arrives late.

Scoring these against execution counted three correct flags as false positives. Left
that way, it would have pushed the tool towards not reporting them at all.

### Where the model layer earns its place

Measured, which is the only way this was going to be settled:

| Run | Model calls | Findings removed | Causes proposed | Rejected as ungrounded |
|---|---|---|---|---|
| corpus with `--execute` | **0** | 0 | 0 | 0 |
| corpus with `--no-execute` | 4 | 0 | 0 | **1** |
| unruled defect, with execution | 1 | 0 | **1** | 0 |

*An earlier version of this table reported "2 calls" for the no-execute row. That figure
was wrong: the `--no-execute` flag was not being forwarded to the harness, so the run
had in fact built both revisions. The row above is from the fixed code.*

**With execution enabled the model makes no calls at all.** Measurement settles every
finding first, so nothing is left to adjudicate. On the gated path, which model is
configured is currently irrelevant.

**It has never suppressed a finding.** Detection is entirely the rules' work, and on a
corpus calibrated to those rules that is expected rather than surprising.

**The self-check rejected one answer as ungrounded** in the no-execute run — the
fabrication guard firing on real output rather than in a test.

**What it does contribute is explanation.** On `unruled_fx_inverted`, a defect outside
every rule family, the rules found nothing, execution reported revenue moving 11.3% on
a regulatory model, and the model proposed the cause: *"dividing instead of multiplying
the amount_txn_ccy by the exchange rate."*

So: **rules detect, execution verifies, and the model explains what neither can.** If a
cause were anticipable there would be a rule for it.

### Tuning the model layer, and what it was worth

Two changes, both measured on the fan-out fixture across three runs each:

| | Verdict | Rationale |
|---|---|---|
| before | `uncertain` | "the context does not resolve this ambiguity" |
| after | `confirm` | "stg_fx_rates is grained on (currency_code, rate_date), but the join only uses currency_code" |

The first change was **a missing fact, not a better prompt**: the specialist was never
shown the SQL of the model being joined to, so the only honest answer was that the
question was open. The second was **verdict semantics**: it kept answering "uncertain"
while its own rationale stated the problem, because "confirm" read as claiming the
damage was proven rather than that the risk was real.

Separately, attributing unexplained changes to their **root** model rather than
reporting one per affected model took a single FX inversion from six findings and six
model calls down to one of each — cheaper and more accurate at once, since five of the
six could only report that nothing had changed in their own SQL.

### Read the headline number carefully

### Read the headline number carefully

**100% here means the corpus is calibrated to the rules, not that the reviewer is
complete.** Three reasons to discount it:

1. **The rules were fitted to this corpus.** F2, F4 and F3003 were written *because*
   this corpus exposed them as false negatives. Measuring them against the same corpus
   measures how well a patch fits the hole it was cut for.
2. **Twenty-two cases is a small sample**, hand-written by the same person who wrote the
   rules.
3. **The oracle only sees what the data exercises.** Two mutations initially scored as
   false positives purely because the seed data never triggered them: every entity
   booked in one currency, and every revenue entry had a contract. The rules were right
   and the oracle could not tell.

### What the corpus has actually been worth

Not the score — the seven defects it found in the reviewer itself, none of which the
137 unit tests caught:

- **`GroupByGrainChangedRule` had never fired.** It read the outermost `SELECT` for a
  `GROUP BY`; dbt models put theirs in the final CTE.
- **`F4001` matched only `DateTrunc`.** Trino parses `date_trunc` to `TimestampTrunc`,
  so the rule was inert against the dialect it targets.
- **`F3003` passed its unit test and missed the real case.** The test used a bare
  `-1 * amt`; the compiled macro produces `-1 * CAST(...) / CAST(...)`, where the sign
  sits inside a division.
- **Macro edits routed by filename.** `macros/money.sql` defines three macros, so
  editing `signed_amount` reached models using `money` and never the model that changed.
- **A one-directional path comparison** then routed macro changes to no models at all —
  caught on the very next run, with two previously-detected defects going quiet.
- **Incremental models carried state between runs.** Each mutation inherited the
  previous one's table, so three behaviour-preserving refactors measured as defects and
  `F1004` fired on leftover rows. Builds now run `--full-refresh` first, then again
  without it so `is_incremental()` is actually exercised.
- **Grain derivation stopped at inline subqueries.** Wrapping a select — a routine
  refactor — made a model's grain unprovable and fired `F7002` on a control.
- **A measured change with no finding was reported as "No findings".** Inverting an FX
  conversion moved revenue by 1.8 million across six models, and because no rule
  covered it the review came back clean. This is the worst failure a merge gate can
  have, and only an unruled mutation could have found it. `X0001` now reports any
  measured change nothing accounts for.

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
- **Execution rebuilds more than strictly changed.** Redirecting output into a fresh
  schema means every `ref()` resolves there, so the full ancestor closure of each
  measured model must be built. At scale the dbt-native answer is `--defer` against a
  production manifest; that needs the dual-manifest backend and is not built yet.
- **Measurement is DuckDB-only.** The warehouse client protocol is small and a Trino
  implementation is a modest addition, but it does not exist today.
- **DuckDB is not Trino.** The demo project stays inside the dialects' intersection.
  Analysis always parses as Trino regardless of what executes, and nothing is executed
  during analysis.
- **The corpus is fitted and small.** See the caveats above. The next useful work is
  defect classes chosen *without* reference to the rules that exist — the current score
  cannot rise, so it can only be made meaningful by making the corpus harder.
- **Families F5–F8 are unimplemented.** Incremental and materialization logic, contracts
  and lineage, governance, and Trino-specific cost rules have no coverage, and the
  corpus contains no cases for them — so their absence does not show up in the score at
  all.
