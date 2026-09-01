"""Request and response shapes for the API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    """Ask for a review. Returns immediately with a run key."""

    project: str = Field(description="Path to the dbt project, relative to the worker.")
    base_ref: str = Field(default="main")
    head_ref: str = Field(default="HEAD")
    repo: str | None = None
    execute: bool = Field(
        default=False,
        description="Build both revisions and measure the difference. Slower, far stronger.",
    )
    use_llm: bool = False
    pr_number: int | None = None
    pr_url: str | None = None


class FindingOut(BaseModel):
    id: int
    fingerprint: str
    rule_id: str
    family: str
    title: str
    severity: str
    confidence: str
    model_name: str
    file_path: str | None
    consequence: str
    suggestion: str | None
    evidence_note: str | None
    sql_after: str | None
    blast_radius: list[str]
    execution_delta: dict[str, object] | None
    disposition: str | None
    # History, which is the point of persisting anything. A finding raised repeatedly
    # and dismissed every time is saying something a single report cannot.
    seen_before: int = 0
    dismissal_rate: float | None = None


class DeltaOut(BaseModel):
    model_name: str
    rows_before: int | None
    rows_after: int | None
    sum_deltas: dict[str, object]
    columns_added: list[str]
    columns_removed: list[str]
    columns_retyped: dict[str, object]
    build_error: str | None
    material: bool


class GrainOut(BaseModel):
    model_name: str
    columns: list[str]
    source: str
    rows_per_key: float | None
    note: str | None


class RunSummary(BaseModel):
    run_key: str
    project: str
    base_ref: str
    head_ref: str
    status: str
    source: str
    executed: bool
    models_reviewed: int
    finding_count: int = 0
    created_at: datetime
    finished_at: datetime | None
    pr_url: str | None
    error: str | None


class RunDetail(RunSummary):
    degraded_reason: str | None
    findings: list[FindingOut]
    deltas: list[DeltaOut]
    grains: list[GrainOut]


class DispositionRequest(BaseModel):
    """How a human judged a finding.

    ``dismissed`` is the valuable one: enough of them against the same fingerprint is
    a measured false-positive signal that nobody had to sit down and label.
    """

    disposition: str = Field(pattern="^(accepted|dismissed|fixed|deferred)$")
    note: str | None = None
