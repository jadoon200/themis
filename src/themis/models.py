"""Core typed structures shared by every stage.

The pipeline is a sequence of pure-ish transforms over these: ACQUIRE builds a
``ProjectSnapshot`` pair, ANALYZE turns them into ``Facts``, RULES turns those into
``Finding`` objects, EXECUTE attaches measured ``ExecutionDelta`` evidence, and REPORT
renders the lot.  Keeping them in one module means a rule can be read without chasing
imports across packages.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """How much a finding should worry a reviewer of financial data."""

    CRITICAL = "critical"  # silently wrong numbers reaching a mart or report
    HIGH = "high"  # wrong numbers plausible, or a downstream break
    MEDIUM = "medium"  # behaviour change that needs a deliberate decision
    LOW = "low"  # cost, style, or hygiene
    INFO = "info"  # context a reviewer wants, not a problem


class Confidence(StrEnum):
    """How sure we are the finding is real — distinct from how bad it would be.

    ``MEASURED`` is reserved for findings a Stage 3 run actually demonstrated. Those
    bypass the LLM entirely: there is nothing left to adjudicate once the row count
    and the SUM have both moved.
    """

    MEASURED = "measured"  # execution proved it
    PROVEN = "proven"  # derivable from the AST with no inference
    LIKELY = "likely"  # strong static signal, some context dependence
    POSSIBLE = "possible"  # worth a look; the LLM's main input


class Verdict(StrEnum):
    """Three-way, never binary.

    ``UNDECIDABLE`` escalates to a human rather than defaulting to safe — the
    conservative stance SQLMesh takes, and it matters more here because the target
    project declares no tests to fall back on.
    """

    SAFE = "safe"
    BREAKING = "breaking"
    UNDECIDABLE = "undecidable"


class Backend(StrEnum):
    """How a snapshot's grounding was obtained.

    Only two, because only two are built. A raw-files backend was designed and never
    implemented: without a compiled manifest the Jinja is unexpanded, so the AST is not
    the SQL that runs, and every analysis downstream would be reasoning about the wrong
    thing. Naming a backend that does not exist made the tool look more capable than it
    is.
    """

    DUAL_MANIFEST = "dual_manifest"  # prod manifest + CI manifest
    MANIFEST = "manifest"  # CI-built compiled manifest


class GrainSource(StrEnum):
    """Where a model's grain came from, in descending confidence.

    Where a project declares no tests, ``DECLARED_TEST`` never fires and the
    structural sources carry the whole load.
    """

    MEASURED = "measured"  # counted it: count(*) vs count(distinct k)
    STRUCTURAL = "structural"  # GROUP BY / DISTINCT / ROW_NUMBER dedup in the AST
    DECLARED_TEST = "declared_test"  # schema.yml unique / unique_combination
    CONFIG = "config"  # incremental unique_key, contract PK
    PROPAGATED = "propagated"  # inherited through the DAG
    HEURISTIC = "heuristic"  # naming only — raises a question, never asserts
    UNKNOWN = "unknown"  # escalates to the human


class Evidence(BaseModel):
    """Where a finding physically lives, so a reviewer can go and look.

    Every finding must carry one. The self-check pass drops any LLM output whose
    rationale cannot point at something here — the fix for the hallucinated-lineage
    failure mode Recce documented.
    """

    model_config = {"frozen": True}

    model_name: str
    file_path: str | None = None
    line: int | None = None
    sql_after: str | None = None
    note: str | None = None
    # Another model the finding turns on — the joined table for a fan-out, say. Its
    # SQL is what actually decides the question, so the context packer needs to know
    # which model to include rather than guessing from prose.
    related_model: str | None = None
    # The column the finding is about, where it is about one. Named structurally
    # rather than left in the prose note, because column lineage has to be asked a
    # column and parsing one back out of a sentence would be a guess.
    column_name: str | None = None
    # What makes this the same issue on the next run, when the note is not it. A measured
    # finding's note carries row counts and totals, which move whenever the data does, so
    # fingerprinting the note gave the same issue a new identity every run and a
    # dismissal never accumulated against it. None means the note is the identity.
    identity: str | None = None


class Grain(BaseModel):
    """A model's inferred unique key, and how much to trust it."""

    model_config = {"frozen": True}

    model_name: str
    columns: tuple[str, ...]
    source: GrainSource
    # Populated once Stage 3 has counted. >1.0 means the "key" does not identify a row.
    rows_per_key: float | None = None
    note: str | None = None

    @property
    def is_proven(self) -> bool:
        """True only where the grain is derived or counted, never guessed.

        ``PROPAGATED`` belongs here, which it did not always. Inheritance only happens
        from a parent that was itself proven, across a single upstream, with no join,
        no set operation, and only when the key survives the projection — so a
        propagated grain is a derivation, not a guess. Excluding it meant a dimension
        selecting straight from a tested staging model counted as having no known key at
        all, and every join onto it was reported as a possible fan-out.

        Those conditions are what makes the chain sound, not just one hop of it. A
        propagated grain becomes a parent on the next pass of the fixpoint in
        ``infer_grains``, so a staging → intermediate → mart run of pass-throughs
        inherits the whole way down — and each link re-proves every condition rather
        than trusting the link above it. The set-operation condition was missing at
        first, and a ``UNION ALL`` of one upstream satisfies every other one while
        doubling the rows.
        """
        return self.source in (
            GrainSource.MEASURED,
            GrainSource.STRUCTURAL,
            GrainSource.DECLARED_TEST,
            GrainSource.CONFIG,
            GrainSource.PROPAGATED,
        )


class KeyedDiff(BaseModel):
    """Rows paired across the two builds on a key both agree is unique.

    Totals are the cheapest evidence and they have a blind spot shaped exactly like the
    most expensive defects: money moving *between* keys. Revenue reclassified from one
    treatment to another, entries shifted between entities, a boundary moved between
    two buckets that both still exist — every row survives, every total holds, and an
    aggregate-only diff reports that nothing moved.

    Pairing needs a key, and the projects this is for declare none. The key here is the
    derived grain, used only once Stage 3 has *counted* it unique in both builds, so a
    pairing is never built on an inference.
    """

    model_config = {"frozen": True}

    key: tuple[str, ...]
    # Present only in head, present only in base, present in both with a value changed.
    rows_added: int = 0
    rows_removed: int = 0
    rows_changed: int = 0
    # column -> how many paired rows changed value in it
    columns_changed: dict[str, int] = Field(default_factory=dict)
    # Columns deliberately not compared, named so a reviewer can see what was skipped.
    ignored_columns: tuple[str, ...] = ()
    # Columns computed from current_timestamp, random(), run_started_at and the like —
    # different in any two builds by construction, found from the SQL rather than names.
    volatile_columns: tuple[str, ...] = ()
    # A few keys whose rows changed, stringified. Evidence a reviewer can go and look up;
    # never included in a redacted report, because key values can identify a customer.
    sample_keys: tuple[str, ...] = ()

    # Set when the key carries an accounting period. A change that moves a figure in a
    # period earlier than the latest one is a restatement of something already reported,
    # whatever else it is — which is a different conversation from a change to the period
    # still open, and the one an auditor has.
    period_column: str | None = None
    latest_period: str | None = None
    prior_period_rows: int = 0
    earliest_changed_period: str | None = None

    @property
    def restates_a_closed_period(self) -> bool:
        return self.prior_period_rows > 0 and self.period_column is not None

    @property
    def moved(self) -> bool:
        return bool(self.rows_added or self.rows_removed or self.rows_changed)


class ExecutionDelta(BaseModel):
    """What actually changed when the model was built both ways.

    This is the strongest evidence the system produces: not "this join may fan out"
    but "row count 1.2M to 1.68M, sum(amount) 44.1M to 61.7M".
    """

    model_name: str
    rows_before: int | None = None
    rows_after: int | None = None
    # column -> (sum before, sum after), for columns detected as monetary
    sum_deltas: dict[str, tuple[float, float]] = Field(default_factory=dict)
    columns_added: tuple[str, ...] = ()
    columns_removed: tuple[str, ...] = ()
    columns_retyped: dict[str, tuple[str, str]] = Field(default_factory=dict)
    null_rate_deltas: dict[str, tuple[float, float]] = Field(default_factory=dict)
    build_error: str | None = None
    # Which revision did not build: "head", "base", or "both". Kept apart from the
    # message because they mean different things to a reviewer — a head that no longer
    # builds is this change's doing, a base that never built is not.
    failed_revision: str | None = None
    # Not built because something it depends on failed, rather than failing itself. The
    # model that actually broke is where a reviewer should look.
    build_skipped: bool = False
    # Rows paired on a key both builds proved unique. None when no such key exists, which
    # is reported as a limit of the evidence rather than read as "nothing moved".
    keyed: KeyedDiff | None = None
    # Why the keyed comparison did not run, when it did not.
    keyed_skipped_reason: str | None = None
    # Set on a copy made for a reader who must not see real values (themis/boundary.py):
    # every number and key value is gone, and `withheld` says in words what moved.
    concealed: bool = False
    withheld: tuple[str, ...] = ()
    concealed_material: bool = False

    @property
    def row_delta(self) -> int | None:
        if self.rows_before is None or self.rows_after is None:
            return None
        return self.rows_after - self.rows_before

    @property
    def is_material(self) -> bool:
        """Did anything a reviewer would care about actually move?"""
        if self.concealed:
            return self.concealed_material
        if self.build_error is not None:
            return True
        if self.row_delta not in (0, None):
            return True
        if self.columns_added or self.columns_removed or self.columns_retyped:
            return True
        if any(sum_moved(before, after) for before, after in self.sum_deltas.values()):
            return True
        # Last, because it is the only test that can see values move while every row
        # count and every total holds.
        return self.keyed is not None and self.keyed.moved


def sum_moved(before: float, after: float) -> bool:
    """Whether two totals differ by more than floating-point noise.

    Exact equality is the wrong test. Summing a floating-point column in a different
    order changes its last bits, so a comment-only change could read as having moved
    the money — which it did, on a control, once the seed data contained cents that
    binary floating point cannot represent.

    The tolerance is relative and far below any difference a reviewer would care
    about: a hundred-million-pound total would have to move by more than a ten-
    thousandth of a penny to register.
    """
    if before == after:
        return False
    scale = max(abs(before), abs(after))
    return abs(after - before) > max(1e-9, scale * 1e-12)


class ModelCall(BaseModel):
    """One completed call to the model: what it was shown, and what it answered.

    Kept so that a training set can exist at all. Today the pack is assembled, sent and
    thrown away, so there is no record of what any answer was grounded in — which makes
    tuning impossible in principle rather than merely premature, and makes "why did it
    say that" unanswerable a week later.

    The human disposition that later settles the finding is not stored here. It arrives
    days after the call and is already on the finding; the export joins the two by
    fingerprint.
    """

    seat: str
    model: str
    # The pack, verbatim — the whole of what the model could see.
    context: str
    system: str
    response: dict[str, Any] = Field(default_factory=dict)
    # Whether the self-check let the answer through. A rejected answer is worth keeping:
    # it is the clearest label there is for what this lane must not produce.
    accepted: bool = True
    rejected_reason: str | None = None
    # The finding it was about, for the fingerprint. None for the intent pass, which
    # judges the change as a whole.
    finding: Finding | None = None


class PriorJudgement(BaseModel):
    """One earlier finding a human ruled on, kept as text a reviewer can read.

    Retrieved for the *kind* of finding under review — same rule, ideally the same
    model — not only for the identical fingerprint, because the useful precedent is
    usually "we decided this about this rule on this model" rather than an exact repeat.
    """

    rule_id: str
    model_name: str
    disposition: str
    title: str
    note: str | None = None
    same_model: bool = False


class FindingHistory(BaseModel):
    """What earlier runs, and the people reading them, did with this same finding.

    Keyed by fingerprint, which is stable across runs by construction. A finding raised
    again and again is either a real problem nobody has fixed or a false positive nobody
    believes, and only the dispositions tell you which.
    """

    occurrences: int = 0
    dismissed: int = 0
    accepted: int = 0
    fixed: int = 0
    deferred: int = 0
    last_note: str | None = None
    # Past judgements on findings like this one, for the specialist's context pack.
    examples: tuple[PriorJudgement, ...] = ()

    @property
    def dispositioned(self) -> int:
        return self.dismissed + self.accepted + self.fixed + self.deferred

    @property
    def dismissal_rate(self) -> float | None:
        """Share of human judgements that dismissed it, or None if nobody has judged."""
        total = self.dispositioned
        return None if total == 0 else self.dismissed / total


class Finding(BaseModel):
    """One reviewable issue. The unit the whole system exists to produce."""

    rule_id: str
    family: str
    title: str
    severity: Severity
    confidence: Confidence
    evidence: Evidence
    # Why it matters in money terms — the part a reviewer actually reads.
    consequence: str
    suggestion: str | None = None
    # Models and exposures downstream of this change; drives ranking.
    blast_radius: tuple[str, ...] = ()
    execution_delta: ExecutionDelta | None = None
    verdict: Verdict = Verdict.UNDECIDABLE
    # Set when a specialist adjudicated it; absent on pure --no-llm runs.
    llm_rationale: str | None = None
    # Corrected SQL a model proposed for this finding. Never applied, and discarded
    # unless it parses in the target dialect and actually differs from the original.
    suggested_fix: str | None = None
    suppressed_reason: str | None = None
    # What earlier runs and their readers did with this same finding. Set only when a
    # store is available; None means "nobody has looked", which is not the same as
    # "nobody dismissed it".
    history: FindingHistory | None = None

    @property
    def is_settled(self) -> bool:
        """Settled findings skip the LLM: execution already proved the point."""
        return self.confidence is Confidence.MEASURED or (
            self.execution_delta is not None and self.execution_delta.is_material
        )
