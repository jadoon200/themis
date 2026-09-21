"""Database tables.

Two things shape this schema beyond simple record-keeping.

*Findings are fingerprinted.* A stable hash over rule, model and normalised evidence
means the same issue is recognisable across runs. That is what turns a pile of reports
into a history, and it is the only false-positive signal available that costs nobody
any labelling effort: a finding raised repeatedly and dismissed every time is telling
you something.

*The queue is this database.* Runs are claimed with ``FOR UPDATE SKIP LOCKED`` rather
than through a broker. Reviews are minutes long and low-volume, so a dedicated queue
would be a service to operate for no benefit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from themis.db.base import Base, JsonType


def utcnow() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunSource(StrEnum):
    CLI = "cli"
    API = "api"
    WEBHOOK = "webhook"


class ReviewRun(Base):
    """One review of one diff. The unit an auditor would ask about."""

    __tablename__ = "review_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Stable public identifier, so a CI job can be handed something opaque.
    run_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    project: Mapped[str] = mapped_column(String(255))
    repo: Mapped[str | None] = mapped_column(String(512), default=None)
    base_ref: Mapped[str] = mapped_column(String(255))
    head_ref: Mapped[str] = mapped_column(String(255))
    base_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    head_sha: Mapped[str | None] = mapped_column(String(64), default=None)

    status: Mapped[str] = mapped_column(String(32), default=RunStatus.QUEUED, index=True)
    source: Mapped[str] = mapped_column(String(32), default=RunSource.CLI)
    pr_number: Mapped[int | None] = mapped_column(Integer, default=None)
    pr_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # What a person reading an overview recognises a pull request by. The branches are
    # base_ref and head_ref; these are the two things a revision cannot say.
    pr_title: Mapped[str | None] = mapped_column(String(512), default=None)
    pr_author: Mapped[str | None] = mapped_column(String(255), default=None)
    # The models this review examined, by name. The count alone was enough for a CLI
    # summary; a page that answers questions about the review needs the names.
    reviewed_models: Mapped[list[str]] = mapped_column(JsonType, default=list)

    # What the run was asked to do, so a result can be interpreted later without
    # guessing which options were in force.
    execute_requested: Mapped[bool] = mapped_column(default=False)
    llm_requested: Mapped[bool] = mapped_column(default=False)
    # What the author says the change does. The intent pass has nothing to compare the
    # SQL against without it, so a queued review could never run the one reviewer that
    # has no rule behind it.
    pr_description: Mapped[str | None] = mapped_column(Text, default=None)

    backend: Mapped[str | None] = mapped_column(String(32), default=None)
    executed: Mapped[bool] = mapped_column(default=False)
    models_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    degraded_reason: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Guards against a worker that died mid-run holding a claim forever.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    worker_id: Mapped[str | None] = mapped_column(String(128), default=None)

    findings: Mapped[list[Finding]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    deltas: Mapped[list[ModelDelta]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    grains: Mapped[list[GrainRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    model_calls: Mapped[list[ModelCallRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[RunSnapshot]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_review_run_claim", "status", "created_at"),
        Index("ix_review_run_project_created", "project", "created_at"),
    )


class Finding(Base):
    """One reviewable issue, as persisted."""

    __tablename__ = "finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("review_run.id", ondelete="CASCADE"))
    run: Mapped[ReviewRun] = relationship(back_populates="findings")

    # Stable across runs — the same issue in the same model hashes identically, which
    # is what makes "raised before and dismissed" answerable.
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)

    rule_id: Mapped[str] = mapped_column(String(32), index=True)
    family: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str] = mapped_column(String(16))

    model_name: Mapped[str] = mapped_column(String(255), index=True)
    file_path: Mapped[str | None] = mapped_column(String(512), default=None)
    line: Mapped[int | None] = mapped_column(Integer, default=None)

    consequence: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str | None] = mapped_column(Text, default=None)
    evidence_note: Mapped[str | None] = mapped_column(Text, default=None)
    sql_after: Mapped[str | None] = mapped_column(Text, default=None)
    llm_rationale: Mapped[str | None] = mapped_column(Text, default=None)
    suppressed_reason: Mapped[str | None] = mapped_column(Text, default=None)

    blast_radius: Mapped[list[str]] = mapped_column(JsonType, default=list)
    execution_delta: Mapped[dict[str, object] | None] = mapped_column(JsonType, default=None)

    # How a human dispositioned it. The signal that makes false-positive rate a
    # measured number rather than an estimate.
    disposition: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    disposition_note: Mapped[str | None] = mapped_column(Text, default=None)
    disposition_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    disposition_by: Mapped[str | None] = mapped_column(String(255), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    disposition_events: Mapped[list[DispositionEvent]] = relationship(
        back_populates="finding",
        cascade="all, delete-orphan",
        order_by="DispositionEvent.at",
    )

    __table_args__ = (Index("ix_finding_fingerprint_created", "fingerprint", "created_at"),)


class DispositionEvent(Base):
    """One human decision about one finding: who, what, when, and why.

    The columns on ``Finding`` hold the latest decision, which is what ranking and the
    report need. They are overwritten, which made them useless as a record: a critical
    finding accepted in March and dismissed in April read as simply dismissed, by nobody.
    Once the decisions are on a page managers read, "who accepted this" is the first
    question anyone asks, and a record that can only answer "someone, at some point" is
    not one. So every decision is appended here and never updated.
    """

    __tablename__ = "disposition_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("finding.id", ondelete="CASCADE"))
    finding: Mapped[Finding] = relationship(back_populates="disposition_events")

    disposition: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    # Whoever the deployment says made the request: a trusted header set by the auth
    # proxy in front of the service, or the name a demo user typed. Never inferred.
    actor: Mapped[str] = mapped_column(String(255))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class RunSnapshot(Base):
    """One revision's compiled project, as the review saw it.

    Stored so a question can be asked about a review after the review has finished. The
    agent's tools read grain, lineage and SQL from a snapshot, and the worker's copy is
    gone the moment the run ends — without this, chat over a stored pull request could
    only ever answer from the handful of facts the report kept. It contains the project's
    compiled SQL, so it lives in the same database as everything else and nowhere else.
    """

    __tablename__ = "run_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("review_run.id", ondelete="CASCADE"))
    run: Mapped[ReviewRun] = relationship(back_populates="snapshots")
    side: Mapped[str] = mapped_column(String(16))  # "before" | "after"
    # zlib-compressed JSON of the ProjectSnapshot. A 3,000-model project is a few MB.
    payload: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("run_id", "side", name="uq_run_snapshot_side"),)


class ModelCallRow(Base):
    """One model call, with the context it was given and the answer it returned.

    The training set, accumulated as a by-product of use. Nothing reads this back into
    a review — it exists so that the question "what was this answer grounded in" has an
    answer later, and so that a tuning set can be assembled from real reviews rather
    than from the corpus that wrote the rules.

    The human judgement is not duplicated here. It lands on the finding days later, and
    the export joins the two on the fingerprint.
    """

    __tablename__ = "model_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("review_run.id", ondelete="CASCADE"))
    run: Mapped[ReviewRun] = relationship(back_populates="model_calls")

    # Which seat made the call: a specialist's name, or intent / fix / explain.
    seat: Mapped[str] = mapped_column(String(64), index=True)
    llm_model: Mapped[str] = mapped_column(String(128))

    # The finding it was about, by the same fingerprint the finding row carries, so a
    # disposition recorded weeks later can be joined to what the model was shown.
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    rule_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    model_name: Mapped[str | None] = mapped_column(String(255), default=None)

    context: Mapped[str] = mapped_column(Text)
    system: Mapped[str] = mapped_column(Text)
    response: Mapped[dict[str, object]] = mapped_column(JsonType, default=dict)
    # Whether the self-check let it through. A rejected answer is a label too.
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelDelta(Base):
    """What Stage 3 measured for one model in one run."""

    __tablename__ = "model_delta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("review_run.id", ondelete="CASCADE"))
    run: Mapped[ReviewRun] = relationship(back_populates="deltas")

    model_name: Mapped[str] = mapped_column(String(255), index=True)
    rows_before: Mapped[int | None] = mapped_column(Integer, default=None)
    rows_after: Mapped[int | None] = mapped_column(Integer, default=None)
    sum_deltas: Mapped[dict[str, object]] = mapped_column(JsonType, default=dict)
    columns_added: Mapped[list[str]] = mapped_column(JsonType, default=list)
    columns_removed: Mapped[list[str]] = mapped_column(JsonType, default=list)
    columns_retyped: Mapped[dict[str, object]] = mapped_column(JsonType, default=dict)
    null_rate_deltas: Mapped[dict[str, object]] = mapped_column(JsonType, default=dict)
    # Rows paired on a key both builds counted unique: added, removed, changed per column.
    keyed_diff: Mapped[dict[str, object] | None] = mapped_column(JsonType, default=None)
    build_error: Mapped[str | None] = mapped_column(Text, default=None)
    material: Mapped[bool] = mapped_column(default=False, index=True)


class GrainRecord(Base):
    """A model's grain as of one run, and how it was established.

    Kept per run rather than per model so the derivation lattice can be evaluated over
    time: how often inference is right, and where measurement contradicts it.
    """

    __tablename__ = "grain_record"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("review_run.id", ondelete="CASCADE"))
    run: Mapped[ReviewRun] = relationship(back_populates="grains")

    model_name: Mapped[str] = mapped_column(String(255), index=True)
    columns: Mapped[list[str]] = mapped_column(JsonType, default=list)
    source: Mapped[str] = mapped_column(String(32), index=True)
    rows_per_key: Mapped[float | None] = mapped_column(Float, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)


def fingerprint_finding(
    *, rule_id: str, model_name: str, project: str, evidence_note: str | None
) -> str:
    """A stable identity for the same issue across runs.

    Deliberately excludes severity, confidence and any measured numbers: those move
    between runs as the code and the data change, and a fingerprint that moves with
    them would make every recurrence look like a new problem.

    The caller passes the evidence's ``identity`` where it has one rather than its note.
    That exclusion used to be claimed here and not delivered: measured findings put row
    counts and totals in the note, so every one of them was new on every run.
    """
    # Normalise whitespace so reformatted evidence does not fork the identity.
    note = " ".join((evidence_note or "").split())
    payload = "\\x1f".join((project, rule_id, model_name, note))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
