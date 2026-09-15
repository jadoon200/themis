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
- **Model layer, in the three seats a rule cannot fill.** Intent reads the author's
  description against the semantic diff; explain proposes a cause for a measured
  movement no rule accounts for; fixes propose corrected SQL, discarded unless they
  parse in the shape the original did. Adjudication remains on and unproven — it has
  never changed a decision.
- **Suggested tests.** The derived grain emitted as `schema.yml` assertions, with a
  refusal policy strict enough that a suggestion does not fail on first run — seven on
  the demo project, all seven holding — and measured to be worth accepting: declaring the keys
  settles the one safe change on the corpus that a key can settle.
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
  rebuilding the ancestor closure twice. Each run builds into schemas of its own and
  drops them afterwards, and dbt's own record of which models built decides what is
  measured — a relation merely existing is never taken as this run's result. A head that
  no longer builds is a finding in its own right (`X0002`).
- **The revision asked for.** The head is compiled and built from the commit `--head`
  names; the working tree stands in only when it is that commit. A seed data change, a
  `dbt_project.yml` edit, and a model outside `models/` all reach a review.
- **A corpus that fails.** `themis eval` exits non-zero when a case could not be scored,
  a defect or latent case goes unreported or is caught only by a family other than the one
  meant for it, a control is flagged, or a rule never fires — and CI runs it against a
  built project. The corpus includes a UNION ALL case and an alias-rename control.
- **A merge gate that fails closed.** With blocking on, a review that skipped checks, ran
  on degraded grounding, or asked for execution that never ran exits 3; SARIF marks the run
  unsuccessful. Finding fingerprints stay stable across runs, measured findings included.
- **Trino, end to end, in CI.** A job builds the demo project with dbt-trino, reviews a
  fan-out with execution against Trino, and checks the run's schemas are gone.
- **Configurable vocabulary, and evidence that can leave the building.** The names checks
  match on — money columns, personal data, reporting tags, published folders — are
  settings. `--redact` writes SARIF and JSON with no SQL, values or names, and
  `themis profile` describes a project in counts.
- **Manifest cache.** Compiled manifests are content-addressed by git revision, so the
  base compile a review repeats every time is paid once — and refused outright for
  projects whose SQL is built from query results, where a revision does not determine
  the output.
- **Capability-scoped workers.** Each worker declares what it may do, and every
  capability is enforced: `compile` (which needs warehouse credentials and is not
  read-only) and `analyse` to take work at all, `review` for a model review, and
  `execute` — off by default, the only one that builds anything — for Stage 3. Checked
  when claiming work and again in the pipeline, because a guard living only in the
  scheduler is one a scheduling bug removes. A worker whose claim was taken over writes
  nothing.
- **Service.** Queued reviews carry the pull-request description and run the model
  layer when asked. Revisions that git would read as options are rejected, project paths
  are bounded by `THEMIS_PROJECT_ROOTS`, and `THEMIS_API_TOKEN` adds a bearer token.
- **Warehouse clients.** DuckDB and **Trino**, both tested against a live engine.
- **Report.** Ranked Markdown, macro attribution, measured deltas where present; SARIF
  for inline annotations, carrying the same triage; and JSON for anything that is not a
  person — the measured deltas, the derived grain, and the checks that could not run.
- **Demo project.** A financial dbt project on DuckDB — general ledger, FX conversion,
  revenue recognition, regulatory mart. Macro-using and, deliberately, test-free.

## Next

**M2 — grounding depth.** Built. Column-level lineage, the grain lattice, macro and
YAML routing, missing-test suggestions derived from the grain, rule families F2 through
F8, and the dual-manifest backend (`--prod-manifest`), measured on the demo project. The
dbt-bouncer ingest is not built; see below.

**M3 — execution.** Built. Base and head built side by side and diffed on real data:
row counts, monetary sums, column sets, null rates. It turned inference into
measurement and settled the grain question [EVAL](EVAL.md) shows inference alone
cannot. The mutation harness and the precision and recall figures run on the same
machinery.

**M4 — review.** Built and, at last, measured in all four seats. Adjudication changes
no decision and has not across 36 runs and three models. The other three earn their
place because no rule can occupy them: **intent** catches 6 of 6 descriptions that
misstate what the change does, **explain** names a cause for a measured movement no
rule accounts for, and **fixes** return corrected SQL for 18 of 27 findings with none
malformed. [EVAL](EVAL.md) has the numbers and the tuning mistake that cost two intent
catches before it was undone.

**M5 — follow-up.** Built as a CLI. Persisted runs and grounded Q&A, including "why was
this not flagged?", answered from persisted absence. An unanswerable question — or an
answer that quotes nothing — gets a refusal. The MCP server and counterfactual questions
("what if this key were tested?") in the original plan are not built.

**M6 — cost.** Built, minus the classifier. Triage ranks and demotes with an
explicit, printed rubric; SARIF carries the same triage so the annotation view and the
report agree; token accounting was already done. The machine-learning lane is
**closed** rather than pending — see below.

**Next.** Deferral and the dual-manifest backend measured against a project large
enough for the saving to show as time rather than as object counts — that number has to
come from a real warehouse. After that, in rough order of what it would change:

- **A sample of differing rows** beside the measured totals, joined on the derived key.
- **A declared test that newly fails, as its own finding.** Stage 3 builds without tests
  so a failing one cannot hide the models below it; running them afterwards against both
  revisions would turn their verdicts into evidence.
- **Learning from dispositions.** Stable fingerprints make "raised and dismissed six times"
  answerable; nothing yet acts on it. See the note on model tuning below.
- **The MCP server** exposing the `ask` lane, and **counterfactual questions** that
  re-run the rules against a hypothetical declared test.

## Not built, with the reason

**dbt-bouncer ingest.** Governance checks that are not diff-aware fire on every model of a
project with no tests, which is exactly the noise triage exists to prevent; adopting it
means writing a tuned configuration first, and F7 already covers the diff-aware part.

**A UI.** Deferred at the owner's request. The JSON report carries everything one would
render.

## Tuning the model, and when it would be worth it

The aim is a reviewer that gets better at catching mistakes the longer it is used. Changing
the model's weights is the last step of that, not the first, and it is not yet worth taking.

**What there is to learn from.** Forty-four synthetic cases written by the person who wrote
the rules, about fifty model calls per corpus run, and one human disposition. A model tuned
on the corpus learns the corpus — and leaves nothing independent to evaluate it on, which is
the reason the classifier lane closed.

**What tuning would change.** Adjudication has changed no decision in 36 runs across three
models and two sampling settings; where it cannot decide, the evidence is missing, and
weights do not add evidence. Intent catches 6 of 6 misleading descriptions. The room left is
in proposed fixes (18 of 27), which gate nothing.

**What it would risk.** A model tuned towards agreeing with dismissals learns to refute
findings — the one behaviour the design forbids. Any tuned model has to show, on cases it
never saw, that it refutes no real defect before it replaces the one it was tuned from.
And in a regulated environment a model whose weights keep changing needs revalidating on
every change: "evolving" has to mean discrete, versioned, evaluated releases recorded
against the reviews that used them, never learning online.

**The order it should evolve in.**

1. *Every miss becomes a corpus case*, and a rule where the defect class is anticipable.
   The gate stops it regressing. This loop exists and CI now enforces it.
2. *Dispositions act.* A finding dismissed on many runs is ranked down — visibly, never
   deleted. Stable fingerprints made this possible; nothing uses them yet.
3. *Past judgements as examples.* Retrieve dispositioned findings like the one under review
   into the specialist's context. Behaviour changes at once, reversibly, and the pack records
   exactly what the model was shown.
4. *Capture the dataset.* Persist each model call's context pack, its answer, and the human
   disposition that later settled it. Today the pack is not stored, so no training set can
   be assembled even in principle.
5. *Then an adapter.* A LoRA adapter on the local model, trained and served for free — on
   Apple silicon an 8B model quantised to 4 bits fits the 18 GB development machine,
   slowly. Worth doing once there are hundreds of real dispositions across more than one
   project, a held-out set of real pull requests to judge it on, and evidence that step 3
   has stopped improving on that set.

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

The one genuine gap either would have filled was retry on a failed completion. The
provider now retries transient failures with backoff — about thirty lines, not a
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
