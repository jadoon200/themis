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
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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

    # What the run was asked to do, so a result can be interpreted later without
    # guessing which options were in force.
    execute_requested: Mapped[bool] = mapped_column(default=False)
    llm_requested: Mapped[bool] = mapped_column(default=False)

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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_finding_fingerprint_created", "fingerprint", "created_at"),)


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
    """
    # Normalise whitespace so reformatted evidence does not fork the identity.
    note = " ".join((evidence_note or "").split())
    payload = "\\x1f".join((project, rule_id, model_name, note))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]
