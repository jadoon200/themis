"""F2 (filters and NULL semantics) and F4 (periods).

Both families were built to close specific false negatives the mutation corpus
exposed, so the tests mirror those cases directly — and, as ever, assert silence on
the refactors that must stay quiet.
"""

from __future__ import annotations

from themis.models import Backend, Confidence, Severity
from themis.rules.base import RuleContext
from themis.rules.families.f2_filters import FilterChangedRule, NotInNullableRule
from themis.rules.families.f4_periods import (
    NonDeterministicTimeRule,
    PeriodGranularityChangedRule,
)
from themis.snapshot import ModelNode, ProjectSnapshot


def _ctx(before_sql: str | None, after_sql: str, *, tags: tuple[str, ...] = ()) -> RuleContext:
    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)

    def model(sql: str) -> ModelNode:
        return ModelNode(
            name="m",
            unique_id="model.t.m",
            file_path="models/m.sql",
            raw_sql=sql,
            compiled_sql=sql,
            tags=tags,
        )

    return RuleContext(
        model_name="m",
        before=model(before_sql) if before_sql else None,
        after=model(after_sql),
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains={},
    )


# --- F2: filters --------------------------------------------------------------

BASE = "select a from t where x > 0"


def test_added_filter_is_reported() -> None:
    """The corpus case: one extra conjunct removes a population from every total."""
    findings = FilterChangedRule().check(
        _ctx(BASE, "select a from t where x > 0 and not is_reversal")
    )
    assert len(findings) == 1
    assert "added" in findings[0].title
    # Not PROVEN: the predicate change is provable, the population change is not, and
    # confidence answers the second. It also routes the model layer — labelling this
    # proven meant a filter finding never reached the reviewer written to judge it.
    assert findings[0].confidence is Confidence.LIKELY


def test_removed_filter_is_reported() -> None:
    findings = FilterChangedRule().check(_ctx("select a from t where x > 0 and y = 1", BASE))
    assert len(findings) == 1
    assert "removed" in findings[0].title


def test_filter_in_a_cte_is_seen() -> None:
    """dbt models filter inside CTEs far more often than in the final SELECT."""
    before = "with s as (select * from t where x > 0) select * from s"
    after = "with s as (select * from t where x > 0 and y = 1) select * from s"
    assert len(FilterChangedRule().check(_ctx(before, after))) == 1


def test_reformatted_filter_is_not_a_change() -> None:
    after = "select a\nfrom t\nwhere   x  >  0"
    assert FilterChangedRule().check(_ctx(BASE, after)) == []


def test_reordered_conjuncts_are_not_a_change() -> None:
    """AND is commutative; reordering is a refactor, not a scope change."""
    before = "select a from t where x > 0 and y = 1"
    after = "select a from t where y = 1 and x > 0"
    assert FilterChangedRule().check(_ctx(before, after)) == []


def test_unchanged_filter_is_silent() -> None:
    assert FilterChangedRule().check(_ctx(BASE, BASE)) == []


def test_governed_model_escalates_a_filter_change() -> None:
    findings = FilterChangedRule().check(
        _ctx(BASE, "select a from t where x > 0 and z = 2", tags=("regulatory",))
    )
    assert findings[0].severity is Severity.CRITICAL


def test_not_in_subquery_is_flagged() -> None:
    findings = NotInNullableRule().check(
        _ctx(None, "select a from t where id not in (select fk from u)")
    )
    assert len(findings) == 1
    assert "no rows at all" in findings[0].title


def test_not_in_literal_list_is_not_flagged() -> None:
    """A literal list is only dangerous if someone writes a NULL into it; flagging
    every NOT IN would bury the subquery case that actually bites."""
    findings = NotInNullableRule().check(_ctx(None, "select a from t where id not in (1, 2, 3)"))
    assert findings == []


def test_pre_existing_not_in_is_not_reported_as_new() -> None:
    sql = "select a from t where id not in (select fk from u)"
    assert NotInNullableRule().check(_ctx(sql, sql)) == []


# --- F4: periods --------------------------------------------------------------


def test_granularity_change_is_reported() -> None:
    """The corpus case: month becoming year moves every row's period.

    Trino parses date_trunc to TimestampTrunc rather than DateTrunc; matching only one
    node type is how this rule silently never fired.
    """
    before = "select date_trunc('month', posting_date) as p from t"
    after = "select date_trunc('year', posting_date) as p from t"
    findings = PeriodGranularityChangedRule().check(_ctx(before, after))
    assert len(findings) == 1
    assert "month" in findings[0].title and "year" in findings[0].title


def test_unchanged_granularity_is_silent() -> None:
    sql = "select date_trunc('month', posting_date) as p from t"
    assert PeriodGranularityChangedRule().check(_ctx(sql, sql)) == []


def test_truncation_on_a_different_column_is_not_a_granularity_change() -> None:
    """A new truncation elsewhere is an unrelated change, not a period shift."""
    before = "select date_trunc('month', posting_date) as p from t"
    after = (
        "select date_trunc('month', posting_date) as p, date_trunc('day', settled_date) as s from t"
    )
    assert PeriodGranularityChangedRule().check(_ctx(before, after)) == []


def test_current_date_introduction_is_flagged() -> None:
    findings = NonDeterministicTimeRule().check(
        _ctx("select a from t", "select a from t where d <= current_date")
    )
    assert len(findings) == 1
    assert "reproduce" in findings[0].consequence


def test_pre_existing_current_date_is_not_reported_as_new() -> None:
    sql = "select a from t where d <= current_date"
    assert NonDeterministicTimeRule().check(_ctx(sql, sql)) == []


def test_deterministic_model_is_silent() -> None:
    assert NonDeterministicTimeRule().check(_ctx("select a from t", "select a, b from t")) == []


# --- calibration against a real-world dbt house style -------------------------
#
# Some projects address source tables as three-part names built with env_var
# (`catalog.{{ env_var("SCHEMA") }}.table`) rather than ref() or source(). That is a
# deliberate convention, not a hardcoded reference, and a rule that flags it fires on
# every model in the project — which is the same as not shipping the rule at all.


def test_env_var_source_references_are_not_hardcoded_tables() -> None:
    from themis.rules.families.f6_contracts import HardcodedTableReferenceRule

    rule = HardcodedTableReferenceRule()
    for sql in (
        'select a from warehouse_cat.{{ env_var("SCHEMA_A") }}.some_table',
        'select a from other_cat.{{env_var("SCHEMA_B")}}.rate_history',
        "select a from cat.{{ env_var('S') }}.t where d = '{{ var(\"businessdate\") }}'",
    ):
        assert rule._literal_refs(sql) == set(), sql


def test_a_genuinely_hardcoded_three_part_name_still_fires() -> None:
    from themis.rules.families.f6_contracts import HardcodedTableReferenceRule

    refs = HardcodedTableReferenceRule()._literal_refs('select a from "proj"."main"."int_x"')
    assert refs
