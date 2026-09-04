"""SARIF output — and the severity distinction it must not quietly lose.

SARIF has four levels and none of them is "critical". Mapping critical and high both
to `error` and stopping there would erase the difference the severity model exists to
draw, while still producing a file that validates.
"""

from __future__ import annotations

import json

from themis.models import Confidence, Evidence, Finding, Severity
from themis.report import sarif


def _finding(
    rule_id: str = "F3001",
    *,
    severity: Severity = Severity.CRITICAL,
    file_path: str | None = "models/fct.sql",
    suppressed: str | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        family=rule_id[:2],
        title="money typed as DOUBLE",
        severity=severity,
        confidence=Confidence.PROVEN,
        evidence=Evidence(model_name="fct", file_path=file_path),
        consequence="binary floating point drifts cents across large sums",
        suggestion="cast to DECIMAL",
        blast_radius=("mart",),
        suppressed_reason=suppressed,
    )


def _log(*findings: Finding) -> dict:
    return json.loads(sarif.render(list(findings)))


def test_the_log_is_valid_sarif_shape() -> None:
    log = _log(_finding())
    assert log["version"] == "2.1.0"
    assert log["runs"][0]["tool"]["driver"]["name"] == "THEMIS"
    assert len(log["runs"][0]["results"]) == 1


def test_critical_keeps_its_severity_even_though_sarif_has_no_word_for_it() -> None:
    """`level` answers "should this fail a build"; it is not the severity model."""
    result = _log(_finding(severity=Severity.CRITICAL))["runs"][0]["results"][0]
    assert result["level"] == "error"
    assert result["properties"]["severity"] == "critical"

    high = _log(_finding(severity=Severity.HIGH))["runs"][0]["results"][0]
    assert high["level"] == "error"
    assert high["properties"]["severity"] == "high"


def test_confidence_travels_with_the_finding() -> None:
    """A viewer showing an inferred finding as though it were measured would mislead."""
    result = _log(_finding())["runs"][0]["results"][0]
    assert result["properties"]["confidence"] == "proven"


def test_each_rule_is_declared_once_with_its_explanation() -> None:
    """A rule that cannot explain itself where it appears is one people dismiss."""
    log = _log(_finding("F3001"), _finding("F3001"), _finding("F1001"))
    rules = log["runs"][0]["tool"]["driver"]["rules"]
    assert [r["id"] for r in rules] == ["F1001", "F3001"]
    assert "floating point" in rules[0]["fullDescription"]["text"]


def test_a_suppressed_finding_travels_as_suppressed_rather_than_being_dropped() -> None:
    """SARIF models this natively; the viewer decides whether to show it."""
    result = _log(_finding(suppressed="refuted by the money reviewer"))["runs"][0]["results"][0]
    assert result["suppressions"][0]["justification"] == "refuted by the money reviewer"


def test_a_finding_with_no_file_still_renders() -> None:
    """Not every finding has a path — a config change routed via YAML may not."""
    result = _log(_finding(file_path=None))["runs"][0]["results"][0]
    assert "locations" not in result
    assert result["ruleId"] == "F3001"


def test_a_location_always_carries_a_positive_line() -> None:
    """SARIF requires one, and most findings are about a model rather than a line."""
    result = _log(_finding())["runs"][0]["results"][0]
    assert result["locations"][0]["physicalLocation"]["region"]["startLine"] >= 1


def test_an_empty_review_renders_an_empty_run_not_an_error() -> None:
    log = _log()
    assert log["runs"][0]["results"] == []


def test_a_demoted_finding_is_suppressed_in_sarif_too() -> None:
    """The annotation view must not show noise the Markdown report just suppressed.

    They describe the same review, and a reviewer looking at the diff in an IDE is
    entitled to the same triage as one reading the report.
    """
    log = _log(_finding("F2001", severity=Severity.HIGH), _finding("F5001"))
    by_rule = {r["ruleId"]: r for r in log["runs"][0]["results"]}
    assert "suppressions" in by_rule["F2001"]
    assert "F5001" in by_rule["F2001"]["suppressions"][0]["justification"]
    assert "suppressions" not in by_rule["F5001"]


def test_both_reasons_for_suppression_survive_together() -> None:
    """A finding can be refuted by a specialist and demoted by triage."""
    log = _log(
        _finding("F2001", severity=Severity.HIGH, suppressed="refuted by the filters reviewer"),
        _finding("F5001"),
    )
    result = next(r for r in log["runs"][0]["results"] if r["ruleId"] == "F2001")
    reasons = [s["justification"] for s in result["suppressions"]]
    assert any("refuted" in r for r in reasons)
    assert any("F5001" in r for r in reasons)
