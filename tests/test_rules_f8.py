"""F8 — Trino engine behaviour, where the SQL is valid and the engine does something else.

The integer-division case is the one with money in it: a ledger keeps amounts in minor
units precisely so they stay whole, and `amount_minor / 100` is the natural way to convert
them back. Trino divides whole numbers as whole numbers, so every fraction of a unit is
discarded, on every row, and the result is a plausible figure that is quietly short.

These tests care as much about staying silent as about firing. Dividing money is routine,
and a rule that flags the project's own correct macro is one people turn off.
"""

from __future__ import annotations

from themis.models import Backend
from themis.rules.base import RuleContext
from themis.snapshot import ColumnSchema, ModelNode, ProjectSnapshot

# --- F8005: whole-number division, which Trino truncates ----------------------------------


def _money_ctx(before_sql: str | None, after_sql: str, columns: tuple = ()) -> RuleContext:
    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)

    def model(sql: str) -> ModelNode:
        return ModelNode(
            name="m",
            unique_id="model.t.m",
            file_path="models/m.sql",
            raw_sql=sql,
            compiled_sql=sql,
            columns=columns,
        )

    return RuleContext(
        model_name="m",
        before=model(before_sql) if before_sql else None,
        after=model(after_sql),
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains={},
    )


def test_minor_units_divided_without_a_cast_are_flagged() -> None:
    from themis.rules.families.f8_engine import IntegerDivisionRule

    (finding,) = IntegerDivisionRule().check(
        _money_ctx(None, "select amount_minor / 100 as amount_usd from t")
    )
    assert finding.rule_id == "F8005"
    assert "amount_minor / 100" in (finding.evidence.note or "")
    assert "5 / 2 is 2" in finding.consequence


def test_the_projects_own_safe_macro_is_silent() -> None:
    """The correct pattern, which the demo's minor_to_major macro already uses.

    A rule that fires on the fix it recommends is a rule people turn off.
    """
    from themis.rules.families.f8_engine import IntegerDivisionRule

    safe = "select cast(cast(amount_minor as decimal(38, 6)) / 100 as decimal(38, 6)) as a from t"
    assert IntegerDivisionRule().check(_money_ctx(None, safe)) == []


def test_dividing_an_ordinary_decimal_amount_is_not_flagged() -> None:
    """Dividing money is routine. Only a whole-number numerator truncates."""
    from themis.rules.families.f8_engine import IntegerDivisionRule

    assert (
        IntegerDivisionRule().check(_money_ctx(None, "select amount_usd / 100 as x from t")) == []
    )


def test_a_declared_integer_column_counts_even_without_a_telling_name() -> None:
    from themis.rules.families.f8_engine import IntegerDivisionRule

    columns = (
        ColumnSchema(name="qty", data_type="bigint"),
        ColumnSchema(name="d", data_type="int"),
    )
    found = IntegerDivisionRule().check(_money_ctx(None, "select qty / d as r from t", columns))
    assert found and "qty / d" in (found[0].evidence.note or "")


def test_an_explicit_integer_cast_on_both_sides_is_flagged() -> None:
    from themis.rules.families.f8_engine import IntegerDivisionRule

    sql = "select cast(x as bigint) / cast(y as bigint) as r from t"
    assert IntegerDivisionRule().check(_money_ctx(None, sql))


def test_a_division_that_was_already_there_is_not_reported_again() -> None:
    from themis.rules.families.f8_engine import IntegerDivisionRule

    sql = "select amount_minor / 100 as a from t"
    assert IntegerDivisionRule().check(_money_ctx(sql, sql + " where x > 0")) == []


# --- F8006: an incremental strategy that deletes rows, on a table that cannot -------------------


def _incremental(
    strategy: str,
    *,
    catalog: str = "hive",
    properties: dict[str, str] | None = None,
) -> ModelNode:
    return ModelNode(
        name="m",
        unique_id="model.t.m",
        file_path="models/m.sql",
        materialization="incremental",
        incremental_strategy=strategy,
        relation_name=f'"{catalog}"."main"."m"',
        properties=properties if properties is not None else {"partitioned_by": "ARRAY['p']"},
    )


def _hive_ctx(before: ModelNode | None, after: ModelNode, vocabulary: object = None) -> RuleContext:
    from themis.vocabulary import DEFAULT

    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)
    return RuleContext(
        model_name="m",
        before=before,
        after=after,
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains={},
        vocabulary=vocabulary or DEFAULT,  # type: ignore[arg-type]
    )


def test_a_new_hive_model_that_deletes_rows_is_flagged_before_it_ever_runs_twice() -> None:
    """No revision before it, so F5002 has nothing to compare, and one build succeeds."""
    from themis.rules.families.f8_engine import RowLevelIncrementalOnHiveRule

    (finding,) = RowLevelIncrementalOnHiveRule().check(
        _hive_ctx(None, _incremental("delete+insert"))
    )
    assert finding.rule_id == "F8006"
    assert "second run" in finding.title
    assert "transactional" in finding.consequence


def test_switching_a_hive_model_to_merge_is_flagged() -> None:
    from themis.rules.families.f8_engine import RowLevelIncrementalOnHiveRule

    (finding,) = RowLevelIncrementalOnHiveRule().check(
        _hive_ctx(_incremental("append"), _incremental("merge"))
    )
    assert "merge" in finding.title


def test_the_working_hive_shape_and_iceberg_are_silent() -> None:
    """Append with partition overwrite is how Hive works; Iceberg can delete and merge."""
    from themis.rules.families.f8_engine import RowLevelIncrementalOnHiveRule

    rule = RowLevelIncrementalOnHiveRule()
    assert rule.check(_hive_ctx(None, _incremental("append"))) == []
    iceberg = _incremental("merge", catalog="iceberg", properties={"partitioning": "ARRAY['p']"})
    assert rule.check(_hive_ctx(None, iceberg)) == []


def test_a_model_already_broken_this_way_is_not_blamed_on_the_change() -> None:
    from themis.rules.families.f8_engine import RowLevelIncrementalOnHiveRule

    broken = _incremental("delete+insert")
    assert RowLevelIncrementalOnHiveRule().check(_hive_ctx(broken, broken)) == []


def test_a_hive_catalog_with_another_name_is_recognised_when_configured() -> None:
    """At work the Hive catalog may be called anything; THEMIS_HIVE_CATALOGS names it."""
    from dataclasses import replace

    from themis.rules.families.f8_engine import RowLevelIncrementalOnHiveRule
    from themis.vocabulary import DEFAULT

    unpartitioned = _incremental("delete+insert", catalog="datalake", properties={})
    rule = RowLevelIncrementalOnHiveRule()
    assert rule.check(_hive_ctx(None, unpartitioned)) == []
    configured = replace(DEFAULT, hive_catalogs=("datalake",))
    (finding,) = rule.check(_hive_ctx(None, unpartitioned, configured))
    assert "datalake" in (finding.evidence.note or "")
