"""Report rendering and ranking."""

from __future__ import annotations

from themis.models import Confidence, Evidence, ExecutionDelta, Finding, Severity
from themis.report.markdown import rank_key, render
from themis.rules.base import SkippedRule


def _finding(
    severity: Severity = Severity.HIGH,
    confidence: Confidence = Confidence.LIKELY,
    blast: tuple[str, ...] = (),
    delta: ExecutionDelta | None = None,
) -> Finding:
    return Finding(
        rule_id="F1001",
        family="F1",
        title="New join may fan out",
        severity=severity,
        confidence=confidence,
        evidence=Evidence(model_name="fct_revenue", file_path="models/fct_revenue.sql"),
        consequence="Amounts would be duplicated.",
        blast_radius=blast,
        execution_delta=delta,
    )


def test_severity_outranks_reach() -> None:
    critical = _finding(severity=Severity.CRITICAL)
    wide_high = _finding(severity=Severity.HIGH, blast=tuple(f"m{i}" for i in range(40)))
    assert rank_key(critical) < rank_key(wide_high)


def test_wider_reach_ranks_first_at_equal_severity() -> None:
    """Two equally severe findings are not equally urgent."""
    wide = _finding(blast=("a", "b", "c"))
    narrow = _finding(blast=("a",))
    assert rank_key(wide) < rank_key(narrow)


def test_measured_findings_outrank_inferred_ones() -> None:
    measured = _finding(confidence=Confidence.MEASURED)
    possible = _finding(confidence=Confidence.POSSIBLE)
    assert rank_key(measured) < rank_key(possible)


def test_clean_report_says_static_only_when_not_executed() -> None:
    output = render([], models_reviewed=3, executed=False)
    assert "No findings" in output
    assert "--execute" in output


def test_clean_report_omits_the_hint_when_execution_ran() -> None:
    assert "--execute" not in render([], models_reviewed=3, executed=True)


def test_measured_deltas_are_rendered_as_numbers() -> None:
    """The whole point of Stage 3: a number, not an adjective."""
    delta = ExecutionDelta(
        model_name="fct_revenue",
        rows_before=1_200_000,
        rows_after=1_680_000,
        sum_deltas={"amount_usd": (44_100_000.0, 61_700_000.0)},
    )
    output = render([_finding(confidence=Confidence.MEASURED, delta=delta)], models_reviewed=1)
    assert "1,200,000" in output
    assert "1,680,000" in output
    assert "+480,000" in output
    assert "sum(amount_usd)" in output


def test_skipped_checks_are_surfaced() -> None:
    """A clean report that quietly ran half the rules is worse than no report."""
    output = render(
        [],
        skipped=[SkippedRule(rule_id="F1001", model_name="m", reason="no compiled SQL")],
        models_reviewed=1,
    )
    assert "could not run" in output
    assert "no compiled SQL" in output


def test_findings_are_ordered_hardest_first() -> None:
    output = render(
        [_finding(severity=Severity.LOW), _finding(severity=Severity.CRITICAL)],
        models_reviewed=1,
    )
    assert output.index("Critical") < output.index("Low")


def test_null_rate_shifts_are_shown() -> None:
    """A column that starts or stops being NULL is the signature of a join-semantics
    change. The aggregate was already computed; not showing it was pure waste."""
    delta = ExecutionDelta(
        model_name="fct_revenue",
        rows_before=15,
        rows_after=15,
        null_rate_deltas={"contract_id": (0.0, 0.2)},
    )
    output = render([_finding(confidence=Confidence.MEASURED, delta=delta)], models_reviewed=1)
    assert "null rate" in output
    assert "20.0%" in output


def test_negligible_null_rate_drift_is_not_shown() -> None:
    delta = ExecutionDelta(
        model_name="fct_revenue",
        rows_before=15,
        rows_after=15,
        null_rate_deltas={"x": (0.10001, 0.10002)},
    )
    assert "null rate" not in render(
        [_finding(confidence=Confidence.MEASURED, delta=delta)], models_reviewed=1
    )
