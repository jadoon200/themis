# Evaluation

Numbers here come from running THEMIS, not from reasoning about it. Where it does
worse than it looks like it should, that is recorded rather than dropped — a reviewer
tool whose limits are undocumented is one whose clean results cannot be trusted.

The sections below are in the order the work happened, and a number in an earlier section
describes the project as it was then. Where later work changed a number, the section says
so. **The current figures are in [Where it stands](#where-it-stands), immediately below.**

## Where it stands

Measured 2026-09-17 on the 46-case corpus, after the fixes described in
[A review of the review](#a-review-of-the-review) and the paired-row comparison described in
[What the oracle could not see](#what-the-oracle-could-not-see). Every figure below is from a run whose
gate passed: no case unscorable, no defect missed or caught only by the wrong family, no
control flagged, every rule fired. **CI's own corpus job now reproduces the testless column
exactly** — the first time it has matched anything in this file.

| | testless (the default) | `--variant tested` |
|---|---|---|
| recall | **100%** (18/18) | **100%** (18/18) |
| false negatives | 0 | 0 |
| benign cases flagged | **4 / 4** | **3 / 4** |
| controls flagged | 0 / 7 | 0 / 7 |
| precision | 82% | 86% |
| false-positive rate | 36% (4/11) | 27% (3/11) |
| latent defects reported | 14 / 14 | 14 / 14 |
| unruled defects reported | **3 / 3** | 3 / 3 |
| rules firing | 29 / 29 | 29 / 29 |
| findings per flagged change | median 1, worst 3 | median 1, worst 4 |

**The median fell because two cases were added, not because findings were lost.** Both new
unruled cases carry a single finding; without them the median is 2 in both columns, as before.
Every other figure is unchanged from the 44-case run.

**Declaring keys costs no recall and buys one benign case.** With the tested variant's
keys merged in, `dim_accounts` inherits a proven key from `stg_accounts`, and a join onto it
stops being reported — the only one of the four benign cases a declared test can settle.
The other three are a filter, a join-type flip and a widened window, and no uniqueness
test says anything about those. The earlier claim that declaring keys *halves* the
false-positive rate was measured on a benign case whose query did not build.

**Read the benign row, not precision.** Precision and the false-positive rate are
properties of the corpus as much as of the tool: every benign case added lowers them, and
every control added raises them. The rate reads 36% rather than the 25% the README once
quoted because benign cases were added after that figure was taken and it was never
re-measured. Nothing regressed; the denominator moved.

The model layer, on the testless run with `--llm` (`qwen3:8b`): 52 calls, 37,058 tokens,
279 seconds. It suppressed nothing, proposed a cause for the one unruled defect, and had
one answer rejected as ungrounded. Intent caught **6 of 6** misleading descriptions — the
new UNION ALL case among them — with one false alarm on three honest ones; fixes came back
for **18 of 27** findings, none malformed.

Median findings per flagged change is 2. That is `X0002` — a head that no longer builds —
reported beside the rule that predicted the breakage, and a new join reported on the
corrected PII case, which genuinely adds one.

**Until 2026-09-13 CI never reproduced any of these numbers.** Its corpus job ran against
a seeded, unbuilt project and measured 9/29 rules and 76% recall on every run for ten days
while passing. Figures in this file before that date came from local runs where the
project happened to be built.

## Grain derivation coverage

*First measured on 9 models. The project is now 16 SQL models and 4 seeds, of which 7 are
proven, 3 heuristic and 6 unknown — `themis grain --project demo_project` prints it, and
`themis profile` the rest of the project's shape.*

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

*The first version of the corpus. It has since grown to 42 cases and 29 rules; see
[Where it stands](#where-it-stands).*

The corpus then: **15 defects, 10 latent, 1 unruled, 6 controls** — at least one case for
every rule family, and **every one of the 28 rules exercised by at least one case**.

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

### The specialists, measured

A no-execute corpus run is the only way to see the specialists at all: with execution
enabled they are never called, because measurement settles every finding first.

| | before | after |
|---|---|---|
| calls | 15 | 15 |
| answers rejected as ungrounded | **5** | **0** |
| findings removed | 0 | 0 |

**None of the five rejections were fabrications.** Three had joined lines of context
into one sentence with commas; two had additionally skipped a line while doing so. Every
phrase used was genuinely present. The check compared characters, so a third of the
model layer's output was being discarded for re-punctuation — and the log did not record
what had been rejected, so it was undiagnosable.

Comparison is now on ordered word tokens, with commas and elision markers treated as
join points and each piece verified contiguously. A reordered quote, an invented one, or
one fabricated clause among real ones is still rejected.

**They still change no decision.** That is worth stating plainly rather than presenting
the fix as a win: the specialists now agree with the rules where before a third of their
agreement was thrown away. Their value would show where a rule is *wrong*, and on this
corpus the rules are not wrong. A separate gap closed along the way — **F2 had no
specialist at all**, so filter and NULL-semantics findings were returned unadjudicated,
which is indistinguishable from a specialist declining to change them. A test now
asserts every family has a reviewer.

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

## Mutations nobody chose

Every hand-written case above is a defect class somebody wrote a rule for, so the rules
always win on them. `themis eval --mutations generated` breaks that: it walks each
model's own source and applies mechanical edits wherever they fit — tightening a join,
flipping a boundary, swapping an aggregate, dropping a `COALESCE`. What gets produced is
determined by what is *in the SQL*, not by what anyone thought to check, and execution
decides which of them are defects.

Twelve requested, eleven producible, seed 1:

| | |
|---|---|
| moved the numbers | **7** |
| of those, reported | **7** |
| **missed** | **0** |
| reported but moved nothing | 3 |
| inert and silent | 1 |

Seven, up from four, after the seed data was regenerated to be awkward rather than
tidy. Dropping a `DISTINCT`, loosening a join and removing a `COALESCE` were all inert
against data where every row matched and no key repeated; against data with duplicate
keys, unmatched rows and NULLs in join columns, they bite. The corpus can now judge
seven of eleven cases instead of four, on the same mutations.

**Nothing that changed the numbers went unreported.** That is the result worth having,
because these cases were not selected with any knowledge of the rules.

The three reported-but-inert cases are the interesting half. Two loosened an inner join
to a left join and one made a range boundary exclusive — all real semantic changes that
happen not to bite on this data, because every row has a match and nothing sits on the
boundary. They are the same category as the hand-written `latent` cases, except the
generator cannot know that in advance. Counting them as false positives would be wrong;
counting them as clean would be wrong too.

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

### Closing six rules that had no case

Six rules had no corpus case at all: `F6003`, `F6004`, `F6005`, `F8001`, `F8003`,
`F8004`. That is precisely where every dead-rule bug had hidden — `F1003`, `F4001` and
`F3003` all passed their unit tests while never firing on anything real.

Two demo additions made the untestable cases testable: **a second attached catalog**, so
a cross-catalog join is built and measured rather than reasoned about, and **a model
under an enforced contract**, so there is a promise to break.

All six fire. At the time this read as weak evidence that dead rules were a phase
rather than a pattern. That reading was wrong: hunting them by hand found six and
missed two more, and only [per-rule coverage reporting](#every-rule-now-fires-on-a-real-case)
found those. The lesson is that the check has to be mechanical, not that the rules
turned out fine.

### What harder data exposed

Regenerating the seed data to be awkward rather than tidy immediately found three
defects, none of which the previous data could have surfaced:

- **The oracle compared totals for exact equality.** Summing a floating-point column in
  a different order changes its last bits, so a comment-only control measured as having
  moved the money. Comparison now uses a relative tolerance far below anything a
  reviewer would notice.
- **The demo project's own money was floating point.** DuckDB's division always returns
  `DOUBLE` whatever the operands, while Trino keeps decimals decimal — so every
  downstream amount was a float. That is the exact defect `F3001` exists to catch,
  sitting in the project used to test it, invisible until the data contained cents that
  binary floating point cannot represent.
- **`F1004` described a state rather than a change.** A model already failing its
  derived key goes on failing it, so the finding attached itself to four
  behaviour-preserving refactors. Stage 3 now measures the base revision too, and it
  fires only where a change made duplication worse.

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
- **F7 had three rules and no coverage at all.** Every previous dead-rule bug had
  hidden in an unmeasured family, so this was the obvious place to look next. Adding
  three governance mutations found all three rules working — the first family probed
  that turned out to be healthy.
- **Grain derivation stopped at inline subqueries.** Wrapping a select — a routine
  refactor — made a model's grain unprovable and fired `F7002` on a control.
- **A measured change with no finding was reported as "No findings".** Inverting an FX
  conversion moved revenue by 1.8 million across six models, and because no rule
  covered it the review came back clean. This is the worst failure a merge gate can
  have, and only an unruled mutation could have found it. `X0001` now reports any
  measured change nothing accounts for.

## What is verified today

End-to-end against the demo project, from the local `fixture/*` branches — rebuilt on the
current project on 2026-09-15, which is how the alias-rename false positive in F2001 was
found. They never reached the remote; the same cases are in the corpus as
`fanout_drop_join_predicate`, `money_cast_to_double` and the controls, and
`themis eval --mutations <id>` reproduces each from any checkout:

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

## The harness itself

Worth recording, because it was the most damaging bug in the project and it recurred:

The harness created a branch in the caller's checkout and restored with
`git checkout --force`. That **destroyed uncommitted work twice during development** —
once swept into a scratch commit that cleanup then deleted (recovered from the reflog),
once discarded outright. The dirty-tree guard could not prevent the second: it checks at
the start, and edits made *during* a run are invisible to it.

Each mutation now runs in a throwaway git worktree on a detached HEAD, so the harness
cannot reach the caller's tree at all. The failure is impossible rather than guarded
against, which is the only version of this that survives someone editing during a run.

## Calibration against a real project

A dbt project in production was reviewed by eye (not committed here, and no code from
it is in this repository). Four things it exposed:

**Schema YAML changes reviewed nothing at all.** Where materialization, partitioning
and hooks are declared in YAML rather than in the model file — which is common — a
config change altering real write behaviour touched no `.sql` and produced an empty
review. `is_schema_yml` existed and was never used. Models are now linked to their YAML
through the manifest's `patch_path`; a four-line YAML edit reviews the nine models it
configures.

**Table properties and hooks were being discarded.** Hive- and Iceberg-backed projects
express partitioning and write semantics through `properties` and `pre_hook`, so a
reviewer ignoring them cannot see a repartitioning at all. Now captured, with `F5006`
for a changed partition specification and `F5007` for the removal of
partition-overwrite writes — after which re-processing a period appends a second copy
instead of replacing the first.

**Source tables addressed as `catalog.{{ env_var("SCHEMA") }}.table`** rather than
`ref()` or `source()`. `F6002` would have fired on every model in the project, which is
the same as not shipping the rule. It does not, because Jinja is stripped before the
literal-name match — luck rather than design, so there is now a test holding it.

**Dynamic SQL generated from data.** A macro that reads a table at compile time and
builds a `CASE` expression from its rows means the compiled SQL changes when the *data*
changes, not only the code. THEMIS would report that as a large semantic diff with no
code change behind it. Since handled in part: such models are detected from the manifest,
a change touching one is flagged (`generated_sql_model_touched` covers it), and the
manifest cache refuses the project outright — see the limitation at the end.

## Column lineage, and what it changes

Impact analysis used to be model-granular, and "who reads this column" was a word
search over downstream SQL. Both halves of that are wrong often enough to matter, so
the two methods were compared over **every column of every model** in the demo project
rather than on the cases that motivated the change.

| | |
|---|---|
| Columns compared | 101 |
| Same answer | 97 |
| Different answer | 4 |

Every disagreement is the search being wrong, in one of two ways:

- **Three name matches that were never dependencies.** `stg_fx_rates.currency_code`
  appears in four downstream models. None of them read it: their `currency_code` comes
  from the ledger, not from the rates table. Same for `stg_accounts.account_id` and
  `stg_contracts.contract_id` — the join keys are named identically on both sides, and
  the mart takes the other one.
- **One dependency the search could not see at all.** `stg_fx_rates.rate` feeds five
  models, under the name `amount_usd` in four of them and `total_amount_usd` in the
  fifth. The word `rate` appears in none of their SQL.

**Projection lineage alone was not enough, and measuring caught it before shipping.**
A join key contributes no column to the output, so tracing projections found no
consumers for one — and a removed join key would have gone from correctly flagged to
silent, which is the worst place in this family to go quiet. References are now
collected separately, from every column a model names anywhere, with star-derived ones
excluded: a column pulled in by `select *` and never mentioned is not a dependency,
because deleting it upstream just produces one column fewer.

The corpus gained `join_key_column_removed` to hold that: 16 true positives, 0 false
negatives, 0 false positives, 6 true negatives — unchanged rates on a larger corpus.

A model whose lineage cannot be resolved is recorded as unresolved and reported as
unknown, and the rule falls back to the name search for exactly those models at lower
confidence. Silence from a lineage tool is how a breaking change gets approved.

## Suggested tests, and how many of them hold

THEMIS derives grain because nothing declares it. The same derivation, emitted as
`schema.yml`, is the project's missing test coverage — but only if the tests pass. A
suggestion that fails on first run is worse than silence, so the refusal policy is the
part worth measuring.

On the demo project (18 nodes, 14 SQL models, 4 seeds, zero declared tests):

| | first measured | 2026-09-15 |
|---|---|---|
| Tests suggested | 5 | 7 |
| Suggested tests that pass when run | **5** | **7** |
| SQL models offered nothing | 9 | 9 |

The sixth arrived when a propagated grain became proof: `dim_entity_contract` passes
`stg_entity_reference` through, and inherits its key. The seventh is the new
`fct_account_period_summary`, a GROUP BY. Both hold on the built tables. The UNION ALL
model beside it is offered nothing, which is the point of it.

The five were checked by counting `count(*)` against `count(distinct key)` on the
built tables, which is what the test would assert. Two of the nine refusals are worth
naming, because they are the cases that would have produced a red test:

- **A heuristic grain is never offered.** `stg_gl_entries` looks unique on `entry_id`
  by naming alone. Naming raises a question; asserting an answer to it would be a
  guess with a `unique` test's authority behind it.
- **A measured multiplier above 1.0 disqualifies outright.** Once counting has settled
  that a key does not identify a row, emitting the assertion would be asserting
  something already known to be false.

Nine models getting nothing is the honest result, not a shortfall to be engineered
away. Those keys need either a human to state them or an `--execute` run to measure
them, and both answers are better than a confident guess.

## Deferral, measured

Execution built each measured model's full ancestor closure, once per revision. With
`--defer-state` pointing at a manifest from an existing build, unselected models
resolve to the relations that manifest names instead.

Same change (`inner` → `left` on the FX join), same demo project, both ways:

| | Objects built per revision | Models measured | Deltas |
|---|---|---|---|
| Without `--defer-state` | 14 | 6 | — |
| With `--defer-state` | 6 | 6 | identical |

Every row count and every `SUM` matched exactly, so the saving costs no evidence. Wall
clock did not move (17s either way) because on a project this small the closure is
cheap and dbt's own startup dominates; the number that matters is the eight objects
per revision that were not rebuilt, which is what scales.

Both revisions defer to the *same* state, which is what keeps the comparison honest:
identical upstream data on either side, code the only difference left.

The same production artifacts also stand in for the base. `--prod-manifest` reads it
from the manifest instead of recompiling it from git, and takes the same directory:

| Review of one fan-out change | Wall clock | Objects built | Findings |
|---|---|---|---|
| Plain (`--execute`) | 17.1s | 28 | 3, all measured |
| `--prod-manifest` + `--defer-state` | 15.2s | 12 | 3, all measured |

Analysis-only, where the base compile is the whole cost rather than a share of it,
the same change goes from 5.9s to 2.8s. Neither figure is the interesting one — the
demo project is fourteen models and dbt's own startup dominates both. What scales is
the base compile that did not happen and the sixteen objects that were not built.

A production manifest that is missing or unreadable does not quietly fall back. It is
named in the report and the base is rebuilt from git, because a reviewer reading a
base-versus-head report while believing it is production-versus-head is answering a
different question than the one they asked.

## Every rule now fires on a real case

The corpus reports per-rule coverage, not just per-family. A family can look well
served while three of its rules have never fired on anything — which has happened here
three times, to rules whose unit tests were all green.

Adding the report immediately found two more, and neither was the kind of gap a
family-level count would have shown:

- **`F4002` was masked by a neighbour.** The `current_date` case was scored as caught
  while the rule that exists for it never ran. The mutation had inserted a filter above
  an `is_incremental()` block, so the model compiled to two `WHERE` clauses; the review
  reported unparseable SQL and the corpus counted a detection. A bad case, scoring as a
  pass, for months.
- **`F5007` could not fire at all.** It asks whether a model overwrites whole partitions
  on write and answered by matching the hook text — but dbt records hooks *unrendered*,
  so a project that keeps write semantics in a macro stores
  `{{ partition_overwrite_hook() }}` and nothing else. In a macro-heavy project that is
  every hook, which is precisely the environment this tool is for. Hook text now
  resolves through the macro table before being matched.

With five cases added (`not_in_nullable_subquery`, `current_date_introduced`,
`materialization_incremental_to_table`, `partition_spec_changed`,
`partition_overwrite_hook_removed`) and the demo project grown a partitioned
incremental model whose write behaviour comes from a macro:

| | |
|---|---|
| Rules firing on at least one case | **29 / 29** |
| True positives | 17 |
| False negatives | 0 |
| False positives | 0 |
| True negatives | 6 |
| Latent defects detected | 14 / 14 |
| Unruled defects detected | 1 / 1 |

Coverage is not correctness — a rule that fires once has been shown to be reachable,
not to be right. But a rule that never fires has been shown to be nothing at all, and
until this report existed there was no way to tell the two apart.

## The manifest cache, and what it must refuse

dbt writes its manifest into `target/`, which every dbt project gitignores. A manifest
is therefore never something a review *finds* — it is something THEMIS compiles, and it
recompiles the same base revision on every review of a branch.

A compiled manifest is a pure function of the code at a git SHA, so it is content
addressable. Measured on an ordinary dbt project (17 models, no compile-time queries),
reviewing the same change twice:

| | Wall clock | Findings |
|---|---|---|
| Cold cache | 6.16s | 1 high |
| Warm cache | **0.47s** | 1 high, identical |

Both revisions hit; the whole review becomes the analysis, which is milliseconds.

**The demo project gets none of this, and that is the correct answer.** It contains a
macro that calls `run_query`, so its compiled SQL is built from whatever the warehouse
held at compile time — the same revision compiles differently as data moves. A cached
manifest would then describe last week's data, and the semantic diff would report
changes nobody made or stay silent about ones they did. Such projects are detected from
the manifest and refused outright, and the refusal is logged, because a project that
silently never caches looks exactly like a cache that is broken.

A working tree is never cached either: one with uncommitted edits is described by no
SHA, so there is no honest key for it. Only a clean checkout gets one.

## Capability-scoped workers

A single worker running every stage needs git, dbt, warehouse credentials with write
access, a model endpoint and the database simultaneously. Stage 3 is the only stage
that runs code against a warehouse, so it is the only one that needs credentials at
all — and it is now the only one that can get them.

| Capability | Needs | Default |
|---|---|---|
| `analyse` | nothing but CPU | on |
| `compile` | the project, dbt, and warehouse credentials — **not** read-only, see below | on |
| `execute` | warehouse credentials with write access | **off** |
| `review` | a model endpoint | on |

Enforced twice, deliberately. A worker without `execute` does not *claim* a run that
asked for it — leaving it queued for one that can, rather than returning a review
quietly missing its strongest evidence. And execution itself refuses, so a bug in the
scheduler cannot route around the first gate. Both paths have a test.

The capabilities are part of the worker identity recorded against every run, so "the
machine that produced this review could not reach the warehouse" is legible from the
record rather than reconstructed from deployment configuration.

**A correction, because the first version of this table was wrong.** `compile` was
described here as warehouse read-only. It is not. `run_query` executes during
compilation, and [dbt-core #12447](https://github.com/dbt-labs/dbt-core/issues/12447)
records pre-hooks running their SQL under `dbt compile` even when guarded by
`{% if execute %}` — a hook containing a `DELETE` will run. What keeps that away from
production is the target allowlist, which applies to compile as it does to execution.
The capability does not, and saying it did was a security claim this design cannot
support.

## What counting findings per change found

The score could not see either of the two worst bugs in this project, and both were
found within an hour of printing how many findings each change produced.

Precision read 100% and both mutations scored *caught*, because in each case the family
meant to fire did fire. What was wrong was everything else the report said.

**`select_star_introduced` produced seven findings, six of them false.** The mutation
adds a star to a projection; it removes nothing. `F6001` subtracted the after column
list from the before one, and a star projection returned an *empty set* — so every
column read as removed. The guard against exactly this was written in the function's
own docstring and applied to one side of the subtraction only. An empty set is a claim
that a model emits nothing; not knowing is a different fact, and the two were sharing a
representation.

**`period_boundary_shifted` produced seven findings, six of them redundant.**
Truncating a date to the year moves no row count and no total in the staging model —
the column is not monetary — while every figure beneath it shifts. `X0001` treated a
model as an origin only if it had moved materially itself, so six descendants were
ownerless and each got its own critical. The finding that named the cause ranked
seventh.

| | before | after |
|---|---|---|
| worst case, findings for one change | 7 | **3** |
| median findings per flagged change | 1 | 1 |
| recall / precision / FP rate | 100 / 100 / 0 | 100 / 100 / 0 |

The scores are identical either side, which is the point. A headline rate computed
over "did the right family fire" was measuring something narrower than it sounded.

## Stage 4, and why the noise existed

The rules are written for recall on the argument that a triage step costs less than a
missed fan-out. That bargain was only half-built: nothing was doing the triage, so the
reviewer got the over-flagging and none of the suppression. Both bugs above are
symptoms of the missing stage rather than of the rules that produced them.

Triage demotes, never deletes. `F2001` says a predicate changed, which is equally true
of a removed `is_incremental()` guard, a narrowed lookback, a `current_date` filter and
a partition column wrapped in a function — and in each case a specific rule already
says *which* predicate and why it matters. The general finding is not wrong; it is the
same fact with less information. It moves beneath the finding that subsumes it, with
the relationship named, and stays in the report because *"why didn't you flag X"* is a
question the follow-up lane has to be able to answer.

On the corpus, five of the eleven changes reported by more than one family are exactly
this shape.

Scoring is a transparent weighted sum whose components are printed beside it. The
number is not the point — being able to see which part produced it is. There is
deliberately no model in it: an opaque score gating a merge is not a reviewable
statement, and "the ranker put it seventh" is not an answer to an auditor.

Writing the test for diminishing returns on reach caught the weights doing the
opposite of what the comment above them claimed.

## The honest headline

*Superseded. One of the two false positives here, `benign_join_to_a_unique_dimension`, did
not build — see [A review of the review](#a-review-of-the-review). The corrected case is
still a false positive, for the right reason, and the current figures are at the top.*

Adding two benign mutations — safe changes that trip a rule — changed the numbers the
project had been reporting since the corpus existed:

| | before | after |
|---|---|---|
| recall | 100% | 100% |
| **precision** | 100% | **89%** |
| **false-positive rate** | 0% | **25%** |
| true positives | 17 | 17 |
| **false positives** | **0** | **2** |

Nothing regressed. The corpus simply contained no case in which a rule could be wrong,
so the zero was a property of the questions being asked rather than of the answers. A
static-analysis study at Tencent found 328 of 433 real production alarms were false
positives; a corpus with none of them is not measuring what practitioners measure.

## What the benign cases showed about the model layer

They were built to give the adjudicator the only job it can actually do — telling a
correct flag from a consequential one — and it suppressed neither. Three separate
causes, and only the first is a straightforward bug:

**A specialist was unreachable.** `F2001` emitted `PROVEN`, and the model layer routes
on confidence, so a filter finding never once reached the filters reviewer written for
it. The label conflated two questions: that a predicate changed is provable, that the
population changed with it is not.

**The evidence to refute did not exist.** `dim_accounts` has no `GROUP BY`, no
`DISTINCT` and no dedup, so nothing in its SQL makes it one row per `account_id` — that
is a property of the data. The correct verdict was never *refute*; it was *uncertain*,
which escalates to a human. The prompt had been tuned toward confirming and had
over-corrected into treating unproven grain as evidence of a fan-out. Its own rationale
was uncertain prose carrying a confirm verdict.

**And that is structural, not incidental.** Where execution runs, measurement settles a
finding before the model is asked. Where it does not, the evidence usually is not there
for anyone to decide — which is this project's own premise, no declared tests, arriving
back where it started. The model layer's demonstrated value sits either side of that
gap: explaining a measured change no rule accounts for, which works, and the intent
pass, which needs a pull-request description the corpus does not supply.

The test that would settle it is the `variants/tested` project, where declared tests
make grain `proven` and refutation becomes possible at all. That is also the compounding
claim `themis suggest-tests` makes: accept the suggested tests and the adjudicator gains
something to reason from. It was run next, in the section that follows.

## Tested against testless, measured at last

*Superseded in its numbers. The one benign case that flipped from false positive to true
negative here was the one whose query did not build, so the flip was measured on SQL that
never ran; and the tested variant could not measure a fan-out at all while `dbt build` ran
the declared tests. Both are fixed, the flip reproduces on the corrected case, and the
current comparison is at the top. The reasoning below stands.*

The plan asked how much **recall** derivation costs against having declared tests. The
variant that would answer it sat in the repository as a file nothing read, and the flag
described for it was never built. Both now exist, and the answer is that the plan asked
the wrong question.

Same corpus, same code, the only difference being whether the project declares its keys:

| | testless (the default) | `--variant tested` |
|---|---|---|
| recall | 100% | 100% |
| **precision** | 89% | **94%** |
| **false-positive rate** | 25% | **12%** |
| true positives | 17 | 17 |
| false positives | 2 | **1** |
| true negatives | 6 | **7** |
| rules firing | 29/29 | 29/29 |
| findings raised | 52 | 48 |

**Derivation costs no recall here at all. It costs precision.** Every defect is caught
either way; what declared tests buy is not flagging the safe changes. That is a more
useful thing to know than the number the plan expected, and it points somewhere
different: the argument for adding tests to a project like this is a quieter review, not
a more thorough one.

### The finding underneath it

Chasing why one benign case flipped from false positive to true negative found a bug
that had been suppressing the whole effect. `dim_accounts` — `select … from
stg_accounts`, no join, no aggregation — carried grain `propagated(account_id)` while
`is_proven` returned False, because `PROPAGATED` was grouped with the guesses.

It is not a guess. Propagation only happens from a parent already proven, across a
single upstream, with no join. What was missing was a fourth condition: **the key has to
survive the projection.** A pass-through selecting a subset can drop part of the
parent's key, and rows unique on `(a, b)` are not unique on `(a)`. The structural path
had always checked this and propagation had not — the difference between a derivation
and something that merely usually holds.

With the guard added, a proven key survives one hop, and a dimension selecting from a
tested staging model stops reading as having no key at all. Every join onto such a
dimension had been reported as a possible fan-out, which is most joins in a warehouse.

This is also the compounding claim `themis suggest-tests` makes, demonstrated rather
than asserted for the first time: accept the suggested `unique` test on
`stg_accounts.account_id`, and a false positive in a different model downstream stops
being raised.

### And what it says about the model layer

The benign case was built to give the adjudicator its only real job, and the layer
suppressed nothing. The reason turned out not to be the adjudicator: the grounding was
discarding a fact it had already derived. Fix the grounding and the rule answers
correctly on its own, with no model call.

That is the honest shape of the result. Where an inference can be made sound, making it
sound beats asking a model to second-guess it — and the cases left over, where the
evidence genuinely cannot settle the question, are the ones a specialist should mark
`uncertain` and escalate rather than decide.

## Exercising every component, and what that alone found

The unit suite had 300 tests and all of them passed. Running the components — the real
CLI, a real Trino, a real Postgres, the real HTTP app — found four defects in an
afternoon, and every one of them was invisible to a test by construction.

| defect | why no test could see it |
|---|---|
| Trino rejects the `partitioned_by` property | needs a real engine; DuckDB ignores unknown properties |
| `themis lineage --model` answered for models that do not exist | needs the CLI, not the library underneath it |
| the Trino build only works once | needs a *second* run against the same warehouse |
| `F1002` never reached a specialist | needs timing that does not add up |

The third and fourth are worth spelling out.

**The Trino build is a cold-warehouse claim.** All eighteen models build from cold,
which is what CI does — a fresh service container every run. The second incremental run
fails, because the memory connector cannot `DELETE` and `delete+insert` needs to. The
claim was true and narrower than it sounded.

**`F1002` emitted `PROVEN`**, and the model layer routes on confidence, so a
`LEFT`-to-`INNER` flip was never once put in front of a reviewer. That a join type
changed is provable; that rows are lost depends on whether the sides match, which is a
fact about the data. This is the **third** rule with that confusion after `F2001` and
the propagated-grain case, and the pattern is now named: a rule knows an edit happened
and reaches for `PROVEN`, when confidence has to answer whether the edit is a *problem*.

Component coverage after the fixes, all exercised for real with expected exit codes
declared so that "correctly refused" counts as a pass rather than an accident:

**29 of 29.** Analysis, every review path, Stage 3 with and without deferral, the
production-target guard, the manifest cache and its refusal, SQLite and Postgres
persistence, the queue and a worker claiming from it, the HTTP API, capability
enforcement, the follow-up lane answering and refusing, a live Trino, and SARIF.

## The model sweep: no configuration changes the answer

Three models, three parameter settings, four cases — the three benign changes where a
refutation would be the layer earning its place, and one real defect where a refutation
would be active harm.

| model | mean per case | refutations | wrongly refuted the real defect |
|---|---|---|---|
| `qwen3:8b` | 12.5s | 0 | 0 |
| `qwen3:14b` | 17.0s | 0 | 0 |
| `qwen2.5-coder:7b` | 11.5s | 0 | 0 |

**36 runs, 0 refutations, 0 self-check rejections.** Temperature 0 against 0.3 changed
nothing; a 400-token output cap against 1200 changed nothing; a model nearly twice the
size changed nothing except being 36% slower, and a coding-specialised model changed
nothing except being marginally faster.

The safety property holds — nothing refuted the genuine fan-out — but the value
property does not appear at any setting. Taken with the grain-propagation result, where
the one benign case that *could* be settled was settled by fixing an inference rather
than by asking a model, the conclusion is not that a bigger model is needed. It is that
the adjudication seat sits in a gap: execution settles what it can before the model is
asked, and what is left usually cannot be settled from the code by anyone.

The first version of this sweep produced the same headline and was worthless: it ran
while the demo project was being rebuilt for Trino, so some runs measured a
partially-compiled manifest. The tell was arithmetic — six runs finishing in seven
seconds cannot contain two model calls. It was re-run against a verified-quiescent
project, which is also how `F1002` was found.

## The three jobs a rule cannot do

Adjudication has never changed a decision — 36 sweep runs across three models settled
that. But adjudication was never the only seat, and the other three had gone unmeasured
for different reasons. All three now have numbers.

### Intent — 5 of 5

The only reviewer with no rule behind it. It compares the author's description against
what the SQL actually does, and it had **never once run**, because it needs a pull
request description and the corpus had none. Eight mutations now carry the description
their author would plausibly have written: five that understate or misstate the change,
three that are honest.

| | |
|---|---|
| Misleading descriptions caught | **5 / 5** |
| False alarms on honest descriptions | 1 / 3 |

What it produced, against what the author claimed:

| the description said | intent said |
|---|---|
| "simplify the money() macro, drop the redundant cast wrapper" | removes the cast around monetary values **in models tagged regulatory or recon** |
| "remove a redundant predicate, already covered by the account type check" | *added* a filter `NOT is_reversal`, not mentioned by the author |
| "fix the debit/credit branch" | alters the **sign convention** of a monetary expression |
| "tidy up leftover scaffolding" | removed the **`is_incremental()` guard**, affecting incremental behaviour |
| "downstream consumers are updated in a follow-up PR" | the column is **still selected downstream**, contradicting that claim |

No rule can produce any of those sentences. `F3001` reports the cast; nothing else in
the system compares it to what the author said they were doing.

The single false alarm is worth naming rather than tuning away. On
`period_boundary_shifted` the author honestly described a granularity change, and
intent restated it with a risk assessment attached — drifting from *"what was not
mentioned"* to *"what is concerning"*. On a denominator of three that is one observation,
not a rate, and the last attempt to tune this pass cost more than it bought.

### The tuning that made it worse

The first measured run caught every misleading description and produced one cosmetic
false alarm: the model wrote *"nothing was omitted"* as an **item** in a list whose only
meaning is omissions. The fix — telling it to return an empty list instead — removed
the false alarms and **cost two real catches**. Pushed toward silence, it went quiet on
changes it had previously described correctly. 5 of 5 became 3 of 5.

That was a bad trade. The artefact was a *formatting* problem and it was solved by
making the reviewer timid. There is now a separate boolean for "the description covers
the change", so the prose can stay direct and the nothing-here case is unambiguous. The
catches came back.

### Fixes — 14 of 24

A rule can name a defect and give general advice; it cannot write the corrected `ON`
clause, because which columns the key needs is not something the rule holds as SQL.

| | |
|---|---|
| Findings with SQL to correct | 24 |
| Proposals returned | **14** |
| Discarded as unparseable | **0** |
| Discarded as identical to the original | 0 |

It proposes for roughly three findings in five and stays quiet on the rest, which is
the shape you want. Two guards make it printable: a proposal that does not parse is
dropped, and one that echoes the original is dropped. Writing the test found the first
guard was wrong — a finding's evidence is almost always a *fragment*, a join clause or a
predicate, and none of those parse standalone, so it would have discarded every correct
answer. Proposals are now checked in whatever context makes the original parse.

### Explain — 1 cause proposed

Fires only with `--execute`, which is why it had gone unmeasured here for so long. It
diagnoses a measured movement no rule accounts for, and on this corpus it did.

### What the model layer costs, and what it is for

45 calls, 31,848 tokens, 285 seconds for a 42-mutation corpus. The run's own summary
line puts it as well as anything:

> It suppressed nothing, so detection is entirely the rules' work. What it added is
> explanation of measured changes no rule accounts for — the one contribution rules
> cannot make.

Detection belongs to the rules. Where the model earns its place is where no rule can
exist: reading a claim against a change, naming a cause for a movement nobody
anticipated, and writing the correction.

## A review of the review

A full review of the project, reading CI logs rather than badges and re-running each
suspicion before believing it, found that several of the numbers above rested on
measurements that were not measuring what they said. Each item was reproduced first.

**The corpus job measured nothing, and passed.** CI seeded the demo project without
building it. `dim_entities` builds a `CASE` expression from a query against `stg_accounts`
at compile time; that table did not exist, every compile aborted, and twenty of twenty-nine
rules skipped on every mutation. The job's own log said `rule coverage: 9/29` and
`recall 76%`. It exited 0 because the exit code depended on stale mutations alone, and an
outcome never recorded that its checks had not run — so X0001 firing on a review with no
compiled SQL counted as a detection. The job now builds the project, and the corpus fails
on a gate: an unscorable case, a missed defect, a flagged control, a mislabelled mutation,
or a rule that never fires.

**Stage 3 measured tables it had not built.** Builds went into fixed schemas that were
never cleared, and a model was measured if its relation existed. Reproduced: a fan-out
review, then a GROUP BY change whose head failed to build. The second review attached the
first one's numbers — `revenue_usd` 334.6M → 2,009.8M — to the GROUP BY change at MEASURED
confidence. Each run now builds into schemas carrying its own token and drops them
afterwards, and dbt's `run_results.json` decides what was built. A head that no longer
builds is its own finding, `X0002`; before it, such a change with no rule firing came back
as "No findings", because the net for unexplained movement ignores build errors by design.

**Four mutations were invalid SQL scoring as caught defects.** An oracle asking whether
anything moved sees a build failure as movement. `grain_drop_group_by_key` left
`currency_code` selected but not grouped, so currencies were never mixed;
`pii_column_exposed` selected a column the mart cannot see; `not_in_nullable_subquery`
used a construct DuckDB cannot execute and compared contract ids to customer ids, which
never match; and `benign_join_to_a_unique_dimension` made `account_id` ambiguous. The last
was scored as one of the two false positives behind the old 25% rate and the single case
behind "declaring keys halves it" — while its query never ran. The mislabel check skipped
benign cases entirely, which is how it went unseen. Each case now does what its
description says, and a case whose breakage is the point declares it (`build_fails`).

**Four shapes were proven unique when they are not.** A top-level `UNION ALL`, a `UNION
ALL` in a CTE, a `ROW_NUMBER` dedup in a CTE followed by a join that fans out, and a GROUP
BY with `ROLLUP` all derived a STRUCTURAL grain, and F1001 writes nothing at all for a join
whose key covers a proven grain. The dedup case is the commonest staging shape there is. So
did GROUP BY and DISTINCT over an expression they could not name: the expression was
dropped, and `group by a, date_trunc(...)` was proven unique on `(a)`. The UNION ALL guard
added the week before had covered propagation only. The demo project derives exactly what
it did before; the difference is entirely in shapes the demo does not contain.

**`--head` was ignored.** The head was compiled from the working tree whatever it named and
labelled with the requested SHA. From a checkout of `main`, a review with `--head` naming a
real fan-out branch reported "No findings". The API's `head_ref` invited exactly this. Any
head other than the working tree is now compiled and built from a worktree at that commit.

**The tested variant could not measure a fan-out.** Found by the new gate on its first run:
with declared tests merged in, `dbt build` ran them, the `unique` test failed on the
fanned-out model, and dbt skipped everything below it. The two fan-out cases became
unscorable and F8002 fell out of coverage. Stage 3 now builds without data tests;
measurement must not depend on what the tests conclude.

**Smaller, all reproduced.** A queued review never ran the model layer (`llm_requested` was
stored and not read). Only the EXECUTE capability was enforced. A reclaimed run could be
written by two workers. `ask` showed an answer that quoted nothing as grounded.
`THEMIS_FAIL_ON_SEVERITY=HIGH` never blocked anything. A seed data change reviewed zero
models. A partial compile was reported only when *no* model compiled.

What the list has in common is the lesson this file keeps relearning: **a check that
cannot fail is not a check.** Coverage that could not drop, a build that could not fail,
a proof that could not be wrong, a flag that could not be set.

### The second pass

Closing the first list turned up four more of the same kind.

**The review's own gate passed an unfinished review.** Its exit code read findings alone,
so with blocking on, a review that skipped most of its rules — the corpus job's failure,
inside the product — exited 0 when it found nothing. It now exits 3, naming each reason,
and SARIF marks the run unsuccessful.

**Measured findings were new on every run.** A fingerprint hashed the evidence note, and a
measured finding's note carries its row counts and totals. History and dismissal rates are
built on fingerprints, so they never accumulated against the findings that most deserve a
history. The fingerprint's own docstring said measured numbers were excluded.

**A pure refactor raised four filter findings.** Rebuilding the stale local fixture branches
on the current project found F2001 comparing predicates as rendered text: renaming
`accounts` to `coa` read as two filters removed and two added. "Pure reformatting yields no
findings" had been written before F2 existed and never re-checked. Predicates are now keyed
by the model a qualifier reads, and `control_rename_alias_in_filter` holds it.

**Detected is not detected by the right rule.** The UNION ALL false negative now has a
corpus case, on a demo model that keeps postings and reversals as separate rows — and the
case shows why it had to be gated differently. The derivation from before the fix proves
`(account_id, period_month)` for that union; F1001 then says nothing while execution still
catches the fan-out, so the case would have scored as detected. The gate now fails a defect
caught only by a family other than its own.

Also closed: model calls retry transient failures, the names checks match on are settings,
`--redact` and `themis profile` let evidence leave a project whose code cannot, and CI
reviews a fan-out on a live Trino end to end.

### Running it all, repeatably

The afternoon of component runs above was done by hand, once. `scripts/component_check.py`
makes it a command: 47 checks against the real CLI, a real Postgres, the HTTP app and a
worker, Ollama, a live Trino, and corpus subsets, over five scenario commits built on HEAD in
a throwaway worktree. Its first full run found three defects that 448 passing tests did not.

| defect | why no test could see it |
|---|---|
| every log line went to stdout, into `--yaml`, `--json` and piped reports | no test parsed a real command's stdout |
| `dim_entities` read as changed on every review | its SQL comes from a query with no ORDER BY; two compiles of one commit differed |
| `ask` about a model the review never saw exited 0 | the reply was true and quoted the absence notice, so grounding passed; only the exit code was wrong |

The third is the subtle one. The model did nothing wrong — it said nothing was found and
cited the line that says so. But a script reads exit 0 as "the review covered this", and
whether it got that exit code depended on how the model chose to phrase a known answer. The
refusal is now made by code, before any model call.

Current result, on `fix/poc-base`: **47 of 47**, none skipped, 459 s; CI green on all four
jobs, with the corpus job reproducing the numbers at the top of this file.

## What the oracle could not see

Execution asked whether rows, totals or the column set moved. So did the corpus oracle —
it is the same measurement — and that shared question had a blind spot shaped exactly like
some of the most expensive defects in finance: **values moving between keys while every
total holds.** A reclassification, an entity remap, a treatment flipped. Because the oracle
could not see it either, the corpus could not even represent the miss.

It was found by reading how other tools compare builds (see [PRIOR_ART](PRIOR_ART.md)), and
then proved before it was fixed:

| case | before pairing | after |
|---|---|---|
| `unruled_period_from_posting_date` — period taken from the posting date, a month-end cutoff error | caught (a grouped model's row count moved) | caught |
| `unruled_recognition_default_flipped` — uncontracted revenue recognised over time instead of at a point in time | **missed — a clean review** | caught: "fct_revenue: paired on (entry_id): 21 row(s) changed value in recognition_method (21)" |

The second case moves no row count and no total anywhere in the project. Stage 3 now pairs
base and head rows on the derived grain — only a grain it has *counted* unique in both
builds — and counts what changed per column, with a relative tolerance so reordered float
arithmetic is not a change. The full corpus still passes its gate with every control
silent: the comparison found nothing on any change that moves nothing.

**The score said caught before the report had been read.** Reading the review found three
defects in X0001 that this was the first case to exercise:

- the model whose SQL changed was described as having *unchanged* SQL — it is a view with no
  countable key, so its own rows could not be paired, and the wording assumed that an origin
  which did not move itself could only be a model nobody edited;
- its evidence showed an unchanged row count and nothing about the table where the values
  moved, so a reviewer could not see what had happened;
- it was **critical**, and named three regulatory marts under "a reported figure moved". None
  of them reads the column that changed. Reachability had been standing in for movement, and
  critical — reserved for a reported figure demonstrated to move — was being awarded on a
  path through the DAG.

All three are fixed and covered, and the component check now asserts on the report itself,
not the score. The same lesson as the one that closed the CI corpus job: **a number that
says "caught" is not evidence of what a reviewer was shown.**

## What a real project would have hit

Asked, before handing the tool over, what else was worth checking, one pass found four
failures that 530 unit tests, the component check and the corpus could not see — because
each depends on a shape a real project has and the demo project does not. Each was
reproduced before it was fixed.

| shape of a real project | what happened | measured |
|---|---|---|
| profile in `~/.dbt`, dbt's default | the first compile failed: "Could not find profile" | review exited 2 with the demo profile moved to a home directory |
| audit columns: `current_timestamp`, `'{{ run_started_at }}'`, `'{{ invocation_id }}'` | a comment-only change was a high "changed and no rule explains why" | 100 of 100 rows "changed" in all three columns |
| a prompt longer than Ollama's default window | the beginning — system prompt and finding — silently dropped | 2,050 of 30,324 prompt tokens evaluated; the answer was `"}"` |
| a paired-row comparison on Trino | `IS NOT DISTINCT FROM` planned as a join filter, not a hash join | 200,000 rows in 66s, against 0.2s with `=` |

The first three would have made THEMIS fail or cry wolf on its first real review. None of them
is a defect class a rule can catch; all of them are about where the code meets the
environment it runs in.

## Scale

`scripts/scale_check.py` generates compiled projects in the shapes a real one has and times
every analysis stage a review runs.

| stage | 250 models | 1,000 | 3,000 |
|---|---|---|---|
| review analysis, total | 0.36s | 1.15s | 3.43s |
| grain | 0.12s | 0.34s | 1.02s |
| volatility detection | 0.20s | 0.78s | 2.37s |
| whole-project lineage, before | 1.93s | 7.97s | 24.27s |

The review path is linear. Whole-project lineage — behind `profile`, `lineage` and the agent's
lineage tool — called sqlglot once per column, and each call re-parsed and re-qualified the
whole model: 18,086 calls at 3,000 models. Traced in one pass per model it takes half the
time, and was checked edge for edge against the per-column trace, in both directions, on the
demo project and a 400-model synthetic one. Stage 3 is not timed here: its cost is the
warehouse's, and at work it is the number to take first.

## The agent

`scripts/agent_eval.py` asks questions with known answers — facts that must appear and facts
that must not — and questions no tool can answer, which must be refused. Four outcomes, and
"grounded but wrong" is the one to watch, because citations cannot catch it: a true quote in
support of a wrong conclusion passes every check.

Every run is recorded, including the ones that went backwards, in order:

| run | set | answerable correct | grounded but wrong | refused wrongly | unanswerable refused |
|---|---|---|---|---|---|
| 1 | 13 single-hop questions | 10 / 10 | 0 | 0 | 3 / 3 |
| 2 | + 4 multi-hop (17) | 11 / 14 | 1 | 2 | 3 / 3 |
| 3 | held out — written before run 3's changes, run once | **5 / 5** | 0 | 0 | **1 / 1** |
| 3 | tuned, after quote-format changes | 10 / 14 | 3 | 1 | 3 / 3 |
| 4 | tuned, after run 3's fixes | **12 / 14** | 1 | 1 | **3 / 3** |

**The multi-hop questions found the tools wrong before they found the model wrong.** Asked
what an FX rate feeds, the lineage tool answered "feeds no downstream column" — downstream
edges live on the consuming models and it had traced only the one asked about. Asked where a
regulatory figure comes from, it gave one hop as the whole chain, for the same reason
upstream. An agent quotes such a result faithfully. Both fixed before run 2 was scored.

**Run 2's failures were about how results read.** A quote reformatted
`tags=regulatory,recon` as `tags: regulatory, recon`; a quote lifted the transcript's call
header; and the grounded-but-wrong answer attached a column to a relation after reading a list
under a header. Every tool line now names its subject and states one fact
(`fct_revenue_incremental — incremental strategy: delete+insert`,
`stg_fx_rates.rate directly feeds int_gl_entries_converted.fx_rate`), and results are marked
off from the calls that produced them. The held-out questions were committed before that
change and run once after it.

**Run 3 went backwards on the tuned set, for four separate reasons.** One was a regression:
upstream lineage, now transitive, gave "which column is this computed from" a four-column
chain, and the model named a grandparent — lines now say *directly* or *indirectly*. One
answer listing a chain ran past the 400-token output limit and was cut off mid-JSON — the
agent now has its own budget. One named only one of two regulatory marts. And one was **a
rubric bug**: a correct "dim_accounts — reads from: stg_accounts" was scored wrong for not
containing "yes"; the rubric was fixed, and that is the only rescoring.

**What remains, after run 4.** The one grounded-but-wrong answer is still the incomplete list:
two regulatory marts downstream, one named. Asking an 8B model to enumerate is the weak point,
and the fix belongs in the tools, not the prompt — a `downstream_models` that filters by tag
returns exactly the answer, with nothing to enumerate. The one wrong refusal was tool choice
on a multi-hop question: it asked for a column `rate` on the mart instead of what the rate
feeds, found nothing, and refused — safe, and a cost.

**Two things found along the way that were not agent problems.** The grounding check shared by
the specialists skipped pieces of a quote shorter than a phrase — so
"materialization: incremental" passed against "materialization: view"; every piece is now
checked, and a short one must sit beside its label. And a quote that was genuinely in a tool
result but filed under the wrong result number is now re-attributed rather than discarded.

**What this does not show.** Twenty-three questions, written by the person who wrote the tools,
on the project the tools were built against. The held-out six are a check on tuning, not on
that. The measurement that matters is reviewers' own questions on the work project.

## Learning from what reviewers decide

The roadmap's first four tuning steps are built (step 5, an adapter, is not — the bar is
unchanged). What follows is what they do and, more usefully, what was deliberately given up
to keep them honest.

**A dismissal ranks a finding down; it never removes one.** Two judgements are required
before anything moves, the penalty scales with the dismissal rate and is capped at 45 points
against a high-severity likely finding's 42-point base, and the score has a floor. The
finding stays on the page, carrying the sentence "raised in 4 earlier run(s); reviewers
ruled 3 dismissed" and the most recent note. A reviewer who disagrees can see exactly what
moved it, which a weight could never offer.

**A measured finding is exempt from its own history.** This is the one that mattered most in
the design. Dismissing a measured finding says someone accepted a change whose rows really
did move; it says nothing about a rule that over-flags. Discounting the next measurement on
that basis is precisely how a tool learns to go quiet on the findings it exists to raise, so
the rubric skips the penalty and says so in the reason line.

**Precedent is shown to specialists and is not evidence.** The pack carries how reviewers
ruled on the same rule — the same model first, with their notes — under a heading that says
these are decisions about other changes. It sits outside `pack.evidence_text`, so the
self-check will not accept a quote taken from it. Without that split, a specialist could
refute a real finding by quoting somebody who once dismissed a different one, and the
grounding check would have called that a well-evidenced answer. Retrieval that teaches a
reviewer to refute is worse than no retrieval.

**Every model call is kept, rejected answers included.** The pack, the instructions, the
parsed answer, and whether the self-check accepted it. An answer the self-check threw out is
the clearest label available for what this lane must not produce, and a training set built
only from accepted answers would omit exactly that class.

**What it is measured on, which is not much.** One synthetic corpus, one real disposition.
The component check exercises the whole loop — a disposition written through one component
reaching the ranking, the pack and the export in another — and 10 of 10 pass. That is
evidence the wiring is real. It is not evidence that the behaviour helps, and it cannot be
until the dispositions come from someone who did not write the rules. `themis dataset`
prints the distance to that bar on every run rather than letting it be estimated.

## Known limitations

Kept current. Several entries here were closed and are gone rather than left standing —
a limitations list that lags the code is worse than none, because it teaches the reader
to discount the rest of it.

- **The set-operation case was written after the bug it tests was found by reading.**
  Grain propagation, and then structural derivation, inherited a proven key across `UNION
  ALL`, and F1 wrote nothing for a join it covered. Forty-two mutations, 29/29 rule coverage
  and 100% recall could not see it, because none of them unioned anything. The corpus now
  has `union_joined_as_one_row_per_period`, on a demo model that keeps postings and
  reversals as separate rows — a real ledger shape — but a case added for a known gap is
  weaker evidence than one that found a gap, and it should be read that way.
- **Stage 3 does not run the project's declared tests.** It builds without them so a
  failing test cannot stop the models below it being measured. A test that passes on the
  base and fails on the head is therefore not yet reported as a finding of its own, though
  the measured duplication it would have caught is.
- **A seed data change is reviewed only with `--execute`.** There is no SQL in it for a rule
  to read. Without execution the report names the seed and what it feeds, and says so.
- **The Trino demo build is only idempotent under `--full-refresh`.** All eighteen
  models build on Trino from cold, which is what CI does — a fresh service container
  every run. A *second* incremental run of the same table fails: the memory connector
  cannot `DELETE`, and `delete+insert` needs to. That is a property of the connector
  chosen to avoid needing an object store, not of dbt-trino or of THEMIS, but it means
  the Trino claim holds for a cold warehouse and has to say so.
- **Trino coverage is single-catalog.** The demo project builds on Trino as well as
  DuckDB, so the rules read Trino-compiled SQL. But Trino's memory connector is one
  catalog, so the federated-join case is exercised on DuckDB's attached catalog rather
  than on Trino itself.
- **DuckDB is not Trino.** The demo project stays inside the dialects' intersection, so
  Trino-specific behaviour (decimal overflow at precision 38, connector MERGE support,
  federated pushdown) is reasoned about and never executed.
- **The corpus is fitted**, though generated mutations offset this in part. The
  generator only applies transformations someone wrote down; it reaches cases nobody
  chose, which is the point, but not cases nobody could imagine.
- **Column lineage stops at the project boundary.** A column read from a `source()`
  whose columns nothing declares leaves that model unresolved, and unresolved models
  fall back to the name search. On the demo project this never happens; on a project
  with undeclared sources it would, which is why unresolved is reported rather than
  quietly treated as clean.
- **A project with any compile-time query gets no manifest caching at all.** The
  refusal is whole-project, because a manifest assembled from mixed sources would put
  compiled SQL and DAG out of step. Projects that generate SQL from data — common in
  the environment this targets — therefore keep paying the base compile.
- **Deferral and the production-manifest backend are measured on a project small
  enough not to need either.** Both savings are real and reproduced above, but 28
  objects to 12 is not evidence about a run whose closure is four hundred models and
  whose state manifest is a nightly production build. That number has to come from a
  real warehouse.
