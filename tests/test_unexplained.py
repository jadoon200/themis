"""X0001 — the safety net, and the discipline that keeps it readable.

It exists because rules only catch defect classes somebody anticipated: inverting an
FX conversion moved revenue by 1.8 million across six models and the review said "no
findings". So a measured movement no rule accounts for must always surface.

The counterweight is that one edit propagates. Reporting it once per affected model
turns a single defect into six criticals, five of which can only say that nothing
changed in their own SQL — and a report that cries wolf on the case it handled
correctly is one nobody reads.
"""

from __future__ import annotations

from themis.execute.runner import ExecutionResult
from themis.models import (
    Backend,
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Severity,
)
from themis.pipeline import unexplained_change_findings
from themis.snapshot import ModelNode, ProjectSnapshot

# stg -> mid -> mart. The chain a single upstream edit propagates down.
_CHAIN = {"stg": ("mid",), "mid": ("mart",)}


def _snapshot(stg_sql: str) -> ProjectSnapshot:
    def node(name: str, sql: str, deps: tuple[str, ...] = ()) -> ModelNode:
        return ModelNode(
            name=name,
            unique_id=f"model.d.{name}",
            file_path=f"models/{name}.sql",
            compiled_sql=sql,
            depends_on_models=tuple(f"model.d.{d}" for d in deps),
        )

    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "stg": node("stg", stg_sql),
            "mid": node("mid", "select * from stg", ("stg",)),
            "mart": node("mart", "select * from mid", ("mid",)),
        },
        child_map=_CHAIN,
    )


def _moved(*names: str) -> ExecutionResult:
    return ExecutionResult(
        deltas={
            name: ExecutionDelta(model_name=name, rows_before=100, rows_after=140) for name in names
        }
    )


def _finding(model: str) -> Finding:
    return Finding(
        rule_id="F4001",
        family="F4",
        title="period granularity changed",
        severity=Severity.HIGH,
        confidence=Confidence.PROVEN,
        evidence=Evidence(model_name=model),
        consequence="figures move between periods",
    )


def test_an_upstream_edit_that_does_not_move_itself_still_owns_what_moved_below() -> None:
    """The case that produced six criticals for one explained change.

    Truncating a date to the year changes no row count and no total in the staging
    model — the column is not monetary — while every figure below it moves. Requiring
    an origin to have moved left those descendants ownerless.
    """
    findings = unexplained_change_findings(
        _moved("mid", "mart"),
        [_finding("stg")],
        _snapshot("select a, month(d) as p from raw"),
        _snapshot("select a, year(d) as p from raw"),
    )
    assert findings == []


def test_an_unexplained_upstream_edit_is_reported_at_the_edit() -> None:
    """The net must not be bought by giving up the thing it is for.

    Suppressing the descendants is only correct if something still names the change.
    The finding points at the model whose SQL moved — where a reviewer can act — not
    at the first model whose totals happened to shift.
    """
    findings = unexplained_change_findings(
        _moved("mid", "mart"),
        [],
        _snapshot("select a, month(d) as p from raw"),
        _snapshot("select a, year(d) as p from raw"),
    )
    assert [f.evidence.model_name for f in findings] == ["stg"]
    assert findings[0].rule_id == "X0001"
    assert "2 downstream model(s)" in (findings[0].evidence.note or "")


def test_a_model_that_moved_with_nothing_changed_anywhere_is_reported() -> None:
    """Moved, own SQL unchanged, downstream of no edit. Genuinely unexplained."""
    same = "select a, month(d) as p from raw"
    findings = unexplained_change_findings(_moved("mart"), [], _snapshot(same), _snapshot(same))
    assert [f.evidence.model_name for f in findings] == ["mart"]


def test_an_origin_that_moved_and_is_unexplained_is_reported_once() -> None:
    """One finding at the origin, not one per model beneath it."""
    findings = unexplained_change_findings(
        _moved("stg", "mid", "mart"),
        [],
        _snapshot("select a, month(d) as p from raw"),
        _snapshot("select a, year(d) as p from raw"),
    )
    assert [f.evidence.model_name for f in findings] == ["stg"]


def test_nothing_moving_reports_nothing() -> None:
    assert (
        unexplained_change_findings(
            ExecutionResult(), [], _snapshot("select 1"), _snapshot("select 1")
        )
        == []
    )


# --- values that move between keys, and what "a reported figure moved" may claim -----


def _governed_snapshot(stg_sql: str) -> ProjectSnapshot:
    """stg -> mid -> mart, with only the mart tagged for regulatory reporting."""
    snapshot = _snapshot(stg_sql)
    mart = snapshot.models["mart"].model_copy(update={"tags": ("regulatory",)})
    return snapshot.model_copy(update={"models": {**snapshot.models, "mart": mart}})


def _paired(name: str, changed: int = 21) -> ExecutionDelta:
    from themis.models import KeyedDiff

    return ExecutionDelta(
        model_name=name,
        rows_before=142,
        rows_after=142,
        keyed=KeyedDiff(
            key=("entry_id",), rows_changed=changed, columns_changed={"recognition": changed}
        ),
    )


def test_an_edit_whose_own_results_hold_says_its_sql_changed() -> None:
    """It used to say "its own SQL did not change" about the model where it did — sending
    a reviewer looking for the cause somewhere else."""
    (finding,) = unexplained_change_findings(
        ExecutionResult(deltas={"mid": _paired("mid")}),
        [],
        _snapshot("select a, 'point_in_time' as recognition from raw"),
        _snapshot("select a, 'over_time' as recognition from raw"),
    )
    assert finding.evidence.model_name == "stg"
    assert "This model's SQL changed" in finding.consequence
    assert "its own SQL did not" not in finding.consequence


def test_the_finding_shows_where_the_movement_was_measured() -> None:
    """The origin measured the same, so its own delta says nothing. The evidence has to
    name the descendant where rows were paired and values moved."""
    (finding,) = unexplained_change_findings(
        ExecutionResult(deltas={"mid": _paired("mid")}),
        [],
        _snapshot("select a, 'point_in_time' as recognition from raw"),
        _snapshot("select a, 'over_time' as recognition from raw"),
    )
    assert finding.evidence.note is not None
    assert "mid: paired on (entry_id): 21 row(s) changed value in recognition (21)" in (
        finding.evidence.note
    )


def test_a_reachable_regulatory_mart_that_did_not_move_is_not_a_reported_figure() -> None:
    """Reachability stood in for movement: a reclassification that moved one untagged
    table was reported critical, naming regulatory marts that never read the column."""
    (finding,) = unexplained_change_findings(
        ExecutionResult(deltas={"mid": _paired("mid")}),
        [],
        _governed_snapshot("select a, 'point_in_time' as recognition from raw"),
        _governed_snapshot("select a, 'over_time' as recognition from raw"),
    )
    assert finding.severity is Severity.HIGH
    assert "A reported figure moved" not in finding.consequence


def test_a_regulatory_mart_measured_to_move_is_critical() -> None:
    (finding,) = unexplained_change_findings(
        ExecutionResult(deltas={"mid": _paired("mid"), "mart": _paired("mart", changed=5)}),
        [],
        _governed_snapshot("select a, 'point_in_time' as recognition from raw"),
        _governed_snapshot("select a, 'over_time' as recognition from raw"),
    )
    assert finding.severity is Severity.CRITICAL
    assert "A reported figure moved: mart" in finding.consequence
