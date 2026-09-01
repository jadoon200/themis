"""F3 — money precision.

The DOUBLE-on-money case is the canonical silent financial bug, so the tests here care
as much about *not* firing on non-monetary columns as about firing on monetary ones. A
rule that flags every float in the project trains reviewers to skip the whole family.
"""

from __future__ import annotations

from themis.models import Backend, Confidence, Severity
from themis.rules.base import RuleContext
from themis.rules.families.f3_money import DecimalScaleReducedRule, MoneyAsFloatRule
from themis.snapshot import ModelNode, ProjectSnapshot


def _ctx(before_sql: str | None, after_sql: str, *, via_macro: str | None = None) -> RuleContext:
    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)

    def model(sql: str) -> ModelNode:
        return ModelNode(
            name="m",
            unique_id="model.t.m",
            file_path="models/m.sql",
            raw_sql=sql,
            compiled_sql=sql,
        )

    return RuleContext(
        model_name="m",
        before=model(before_sql) if before_sql else None,
        after=model(after_sql),
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains={},
        via_macro=via_macro,
    )


def test_money_cast_to_double_is_critical() -> None:
    findings = MoneyAsFloatRule().check(_ctx(None, "select cast(x as double) as amount_usd from t"))
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].confidence is Confidence.PROVEN


def test_trino_real_is_treated_as_inexact() -> None:
    """REAL parses to FLOAT in sqlglot; it is binary floating point either way."""
    findings = MoneyAsFloatRule().check(_ctx(None, "select cast(x as real) as total_fee from t"))
    assert len(findings) == 1


def test_decimal_money_is_not_flagged() -> None:
    findings = MoneyAsFloatRule().check(
        _ctx(None, "select cast(x as decimal(38,6)) as amount_usd from t")
    )
    assert findings == []


def test_non_monetary_double_is_not_flagged() -> None:
    """A latitude or a ratio is legitimately a float. Flagging it burns credibility."""
    findings = MoneyAsFloatRule().check(
        _ctx(None, "select cast(x as double) as latitude, cast(y as double) as ratio from t")
    )
    assert findings == []


def test_pre_existing_float_is_not_reported_as_new() -> None:
    """Only what this change introduced is this review's business."""
    sql = "select cast(x as double) as amount_usd from t"
    assert MoneyAsFloatRule().check(_ctx(sql, sql)) == []


def test_decimal_to_double_regression_is_caught() -> None:
    findings = MoneyAsFloatRule().check(
        _ctx(
            "select cast(x as decimal(38,6)) as amount_usd from t",
            "select cast(x as double) as amount_usd from t",
        )
    )
    assert len(findings) == 1


def test_macro_attribution_is_named_in_the_suggestion() -> None:
    """A reviewer seeing a finding on a file they never edited needs to know why."""
    findings = MoneyAsFloatRule().check(
        _ctx(None, "select cast(x as double) as amount_usd from t", via_macro="money")
    )
    suggestion = findings[0].suggestion or ""
    assert "money" in suggestion
    assert "every model that uses it" in suggestion


def test_one_finding_per_column_not_per_occurrence() -> None:
    findings = MoneyAsFloatRule().check(
        _ctx(
            None,
            "select cast(a as double) as amount_usd, cast(b as double) as amount_usd from t",
        )
    )
    assert len(findings) == 1


def test_reduced_decimal_scale_is_flagged() -> None:
    findings = DecimalScaleReducedRule().check(
        _ctx(
            "select cast(x as decimal(38,6)) as amount_usd from t",
            "select cast(x as decimal(18,2)) as amount_usd from t",
        )
    )
    assert len(findings) == 1
    assert "6" in findings[0].title and "2" in findings[0].title


def test_increased_decimal_scale_is_not_flagged() -> None:
    """Widening precision loses nothing."""
    findings = DecimalScaleReducedRule().check(
        _ctx(
            "select cast(x as decimal(18,2)) as amount_usd from t",
            "select cast(x as decimal(38,6)) as amount_usd from t",
        )
    )
    assert findings == []


def test_unchanged_scale_is_not_flagged() -> None:
    sql = "select cast(x as decimal(38,6)) as amount_usd from t"
    assert DecimalScaleReducedRule().check(_ctx(sql, sql)) == []
