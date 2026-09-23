"""An agent workspace rebuilt from a review that finished some time ago.

The agent was written for a review running in the same process: it reads grain, lineage and
SQL from the snapshots the review acquired, the findings it raised and what execution
measured. A page asking questions about a pull request reviewed yesterday has none of that
in memory — only what was stored. So the snapshots are stored with the run, and this turns
the rows back into the objects the tools already read, instead of teaching every tool a
second way to answer.

What is rebuilt is what was kept. Sample keys were never stored, because a key value can
identify a customer, so a question about which rows moved gets counts and not examples.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from themis.agent.workspace import Workspace
from themis.conventions import Convention
from themis.db.models import Finding as FindingRow
from themis.db.models import GrainRecord, ModelDelta, ReviewRun
from themis.db.store import load_snapshots
from themis.execute.runner import ExecutionResult
from themis.models import (
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Grain,
    GrainSource,
    KeyedDiff,
    Severity,
    Verdict,
)


def _finding(row: FindingRow) -> Finding:
    return Finding(
        rule_id=row.rule_id,
        family=row.family,
        title=row.title,
        severity=Severity(row.severity),
        confidence=Confidence(row.confidence),
        verdict=Verdict(row.verdict),
        evidence=Evidence(
            model_name=row.model_name,
            file_path=row.file_path,
            line=row.line,
            note=row.evidence_note,
            sql_after=row.sql_after,
        ),
        consequence=row.consequence,
        suggestion=row.suggestion,
        blast_radius=tuple(row.blast_radius or ()),
        llm_rationale=row.llm_rationale,
        suppressed_reason=row.suppressed_reason,
    )


def _keyed(payload: dict[str, object] | None) -> KeyedDiff | None:
    if not payload:
        return None
    return KeyedDiff.model_validate(payload)


def _pairs(stored: dict[str, object] | None) -> dict[str, tuple[object, object]]:
    """A JSON column of `name -> [before, after]`, back as tuples. Anything else is skipped."""
    out: dict[str, tuple[object, object]] = {}
    for key, value in (stored or {}).items():
        if isinstance(value, (list, tuple)) and len(value) == 2:
            out[key] = (value[0], value[1])
    return out


def _delta(row: ModelDelta) -> ExecutionDelta:
    # Validated rather than cast by hand: the model's own types are the contract, and a
    # stored value that no longer fits them should fail loudly here, not in a tool.
    return ExecutionDelta.model_validate(
        {
            "model_name": row.model_name,
            "rows_before": row.rows_before,
            "rows_after": row.rows_after,
            "sum_deltas": _pairs(row.sum_deltas),
            "columns_added": tuple(row.columns_added or ()),
            "columns_removed": tuple(row.columns_removed or ()),
            "columns_retyped": _pairs(row.columns_retyped),
            "null_rate_deltas": _pairs(row.null_rate_deltas),
            "build_error": row.build_error,
            "keyed": _keyed(row.keyed_diff),
        }
    )


def _grain(row: GrainRecord) -> Grain:
    return Grain(
        model_name=row.model_name,
        columns=tuple(row.columns or ()),
        source=GrainSource(row.source),
        rows_per_key=row.rows_per_key,
        note=row.note,
    )


def workspace_for_run(
    session: Session,
    run: ReviewRun,
    *,
    dialect: str = "trino",
    conventions: tuple[Convention, ...] = (),
) -> Workspace | None:
    """The workspace a stored review can support, or None if it kept no snapshots.

    A run stored before snapshots were kept — or by a path that never had them — cannot
    support the agent's tools, and saying so is better than answering from a project the
    review never saw.
    """
    before, after = load_snapshots(session, run)
    if after is None:
        return None
    execution = (
        ExecutionResult(deltas={row.model_name: _delta(row) for row in run.deltas})
        if run.executed
        else None
    )
    workspace = Workspace(
        after=after,
        before=before,
        changed_models=tuple(run.reviewed_models or ()),
        findings=tuple(_finding(row) for row in run.findings),
        execution=execution,
        conventions=conventions,
        dialect=dialect,
    )
    stored = {row.model_name: _grain(row) for row in run.grains}
    if stored:
        # The keys the findings were judged against, over anything re-derived: a question
        # about a review should be answered from what that review knew.
        workspace._grains = {**workspace.grains, **stored}
    return workspace
