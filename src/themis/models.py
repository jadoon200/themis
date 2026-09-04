"""Core typed structures shared by every stage.

The pipeline is a sequence of pure-ish transforms over these: ACQUIRE builds a
``ProjectSnapshot`` pair, ANALYZE turns them into ``Facts``, RULES turns those into
``Finding`` objects, EXECUTE attaches measured ``ExecutionDelta`` evidence, and REPORT
renders the lot.  Keeping them in one module means a rule can be read without chasing
imports across packages.
"""

from __future__ import annotations

from enum import StrEnum

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
        """True only where the grain is derived or counted, never guessed."""
        return self.source in (
            GrainSource.MEASURED,
            GrainSource.STRUCTURAL,
            GrainSource.DECLARED_TEST,
            GrainSource.CONFIG,
        )


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

    @property
    def row_delta(self) -> int | None:
        if self.rows_before is None or self.rows_after is None:
            return None
        return self.rows_after - self.rows_before

    @property
    def is_material(self) -> bool:
        """Did anything a reviewer would care about actually move?"""
        if self.build_error is not None:
            return True
        if self.row_delta not in (0, None):
            return True
        if self.columns_added or self.columns_removed or self.columns_retyped:
            return True
        return any(sum_moved(before, after) for before, after in self.sum_deltas.values())


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
    suppressed_reason: str | None = None

    @property
    def is_settled(self) -> bool:
        """Settled findings skip the LLM: execution already proved the point."""
        return self.confidence is Confidence.MEASURED or (
            self.execution_delta is not None and self.execution_delta.is_material
        )
