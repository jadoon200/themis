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


# --- sign convention ----------------------------------------------------------
#
# A reversed figure of the right magnitude is far harder to spot than a missing one,
# and the diff that causes it is a single changed string literal.

CREDIT_POSITIVE = "select case when t = 'credit' then amt else -1 * amt end as amount_usd from x"
DEBIT_POSITIVE = "select case when t = 'debit' then amt else -1 * amt end as amount_usd from x"


def test_flipped_sign_condition_is_critical() -> None:
    from themis.rules.families.f3_money import SignConventionChangedRule

    findings = SignConventionChangedRule().check(_ctx(CREDIT_POSITIVE, DEBIT_POSITIVE))
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL
    assert findings[0].confidence is Confidence.PROVEN


def test_unchanged_sign_convention_is_silent() -> None:
    from themis.rules.families.f3_money import SignConventionChangedRule

    assert SignConventionChangedRule().check(_ctx(CREDIT_POSITIVE, CREDIT_POSITIVE)) == []


def test_reformatted_case_is_not_a_sign_change() -> None:
    from themis.rules.families.f3_money import SignConventionChangedRule

    reformatted = (
        "select\n  case\n    when t = 'credit' then amt\n"
        "    else -1 * amt\n  end as amount_usd\nfrom x"
    )
    assert SignConventionChangedRule().check(_ctx(CREDIT_POSITIVE, reformatted)) == []


def test_case_without_opposing_signs_is_ignored() -> None:
    """A CASE that does not assign a sign is just a CASE."""
    from themis.rules.families.f3_money import SignConventionChangedRule

    before = "select case when t = 'a' then amt else amt end as amount_usd from x"
    after = "select case when t = 'b' then amt else amt end as amount_usd from x"
    assert SignConventionChangedRule().check(_ctx(before, after)) == []


def test_non_monetary_case_is_ignored() -> None:
    """Sign flips matter because the column is money; a flag column is not."""
    from themis.rules.families.f3_money import SignConventionChangedRule

    before = "select case when t = 'a' then offset else -1 * offset end as delta from x"
    after = "select case when t = 'b' then offset else -1 * offset end as delta from x"
    assert SignConventionChangedRule().check(_ctx(before, after)) == []


# --- F3004: an amount in its own currency, summed across currencies ------------------------

_GROUPED = """
select period_month, currency_code, sum(amount_txn_ccy) as revenue_txn_ccy
from entries
group by period_month, currency_code
"""

_MIXED = """
select period_month, sum(amount_txn_ccy) as revenue_txn_ccy
from entries
group by period_month
"""


def test_a_transaction_currency_amount_summed_without_the_currency_is_flagged() -> None:
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    (finding,) = MixedCurrencyTotalRule().check(_ctx(_GROUPED, _MIXED))
    assert finding.rule_id == "F3004"
    assert finding.severity is Severity.HIGH
    assert "revenue_txn_ccy = sum(amount_txn_ccy)" in (finding.evidence.note or "")
    assert "no unit" in finding.consequence


def test_the_same_sum_with_the_currency_in_the_grain_is_silent() -> None:
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    assert MixedCurrencyTotalRule().check(_ctx(_MIXED, _GROUPED)) == []


def test_an_already_converted_amount_sums_correctly_and_is_never_flagged() -> None:
    """`revenue_usd` is monetary and denominated and adds up perfectly.

    Flagging it would mean firing on the correct way to write the thing this rule asks
    for, in every model that converts — which is how a family gets ignored.
    """
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    converted = "select period_month, sum(amount_usd) as revenue_usd from e group by period_month"
    assert MixedCurrencyTotalRule().check(_ctx(None, converted)) == []


def test_a_model_restricted_to_one_currency_is_not_flagged() -> None:
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    pinned = (
        "select period_month, sum(amount_txn_ccy) as t from e "
        "where currency_code = 'USD' group by period_month"
    )
    assert MixedCurrencyTotalRule().check(_ctx(None, pinned)) == []


def test_a_positional_group_by_still_counts_as_grouping_by_the_currency() -> None:
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    positional = "select period_month, currency_code, sum(amount_txn_ccy) as t from e group by 1, 2"
    assert MixedCurrencyTotalRule().check(_ctx(None, positional)) == []


def test_a_model_that_has_always_summed_this_way_is_not_reported_on_an_unrelated_edit() -> None:
    """Diff-aware, like the rest of the family. THEMIS reviews a change, not a project."""
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    edited = _MIXED.replace("from entries", "from entries -- a comment")
    assert MixedCurrencyTotalRule().check(_ctx(_MIXED, edited)) == []


def test_an_inner_grouping_does_not_excuse_an_outer_sum() -> None:
    """A subtree search would read the CTE's GROUP BY as proof and stay silent."""
    from themis.rules.families.f3_money import MixedCurrencyTotalRule

    nested = """
    with per_currency as (
        select period_month, currency_code, sum(amount_txn_ccy) as amount_txn_ccy
        from entries group by period_month, currency_code
    )
    select period_month, sum(amount_txn_ccy) as revenue_txn_ccy
    from per_currency group by period_month
    """
    (finding,) = MixedCurrencyTotalRule().check(_ctx(None, nested))
    assert "revenue_txn_ccy" in (finding.evidence.note or "")
