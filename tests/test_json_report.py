"""Machine-readable output — and the parts a findings format has no room for.

SARIF carries what a viewer needs to draw an annotation. A CI job deciding whether to
block, or a dashboard trending false positives, needs the measured deltas, the derived
grain, and above all the checks that could not run.
"""

from __future__ import annotations

import json

from themis.models import (
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Grain,
    GrainSource,
    Severity,
)
from themis.report import json_out
from themis.rules.base import SkippedRule


def _finding(rule_id: str = "F1001", **kw: object) -> Finding:
    return Finding(
        rule_id=rule_id,
        family=rule_id[:2],
        title=f"{rule_id} fired",
        severity=kw.get("severity", Severity.HIGH),  # type: ignore[arg-type]
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name="fct", file_path="models/fct.sql"),
        consequence="rows may multiply",
        blast_radius=("mart",),
    )


def _render(**kw: object) -> dict:
    return json.loads(json_out.render(**kw))  # type: ignore[arg-type]


def test_findings_carry_their_triage_rather_than_being_dropped() -> None:
    """Omitting demoted findings makes "why didn't you flag X" unanswerable."""
    doc = _render(findings=[_finding("F2001"), _finding("F5001")])
    by_rule = {f["rule_id"]: f for f in doc["findings"]}
    assert by_rule["F2001"]["triage"]["subsumed_by"] == "F5001"
    assert by_rule["F5001"]["triage"]["subsumed_by"] is None
    assert len(doc["findings"]) == 2


def test_skipped_checks_are_reported_not_omitted() -> None:
    """A report that hides its blind spots reads exactly like one that had none."""
    doc = _render(
        findings=[],
        skipped=[SkippedRule(rule_id="F3001", model_name="fct", reason="no compiled SQL")],
    )
    assert doc["skipped_checks"] == [
        {"rule_id": "F3001", "model": "fct", "reason": "no compiled SQL"}
    ]


def test_measured_deltas_travel_with_the_run() -> None:
    """The strongest evidence the tool produces, and SARIF has nowhere to put it."""
    delta = ExecutionDelta(
        model_name="fct", rows_before=100, rows_after=140, sum_deltas={"amount_usd": (10.0, 14.0)}
    )
    doc = _render(findings=[], deltas={"fct": delta}, executed=True)
    recorded = doc["execution_deltas"][0]
    assert recorded["row_delta"] == 40
    assert recorded["sum_deltas"]["amount_usd"] == [10.0, 14.0]
    assert recorded["is_material"] is True


def test_grain_records_how_it_was_derived() -> None:
    """A key read from a test and one guessed from a column name are not the same fact."""
    grains = {"fct": Grain(model_name="fct", columns=("id",), source=GrainSource.HEURISTIC)}
    doc = _render(findings=[], grains=grains)
    assert doc["grains"][0] == {
        "model": "fct",
        "columns": ["id"],
        "source": "heuristic",
        "is_proven": False,
        "rows_per_key": None,
        "note": None,
    }


def test_degraded_grounding_is_always_present_even_when_absent() -> None:
    doc = _render(findings=[])
    assert "degraded_reason" in doc and doc["degraded_reason"] is None


def test_an_empty_review_renders_a_valid_document() -> None:
    doc = _render(findings=[])
    assert doc["schema_version"] == 1
    assert doc["findings"] == []
