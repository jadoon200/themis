"""Grain derivation — the component the fan-out family rests on.

Where a project declares no uniqueness tests, every one of these patterns is
load-bearing: whatever they fail to derive becomes an `unknown` that escalates to a
human, and whatever they derive *wrongly* becomes a confidently missed fan-out.
"""

from __future__ import annotations

import pytest

from themis.analyze.grain import infer_grains
from themis.models import Backend, GrainSource
from themis.snapshot import ColumnSchema, ModelNode, ProjectSnapshot


def _snapshot(**sql_by_name: str) -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="test",
        backend=Backend.MANIFEST,
        models={
            name: ModelNode(
                name=name,
                unique_id=f"model.test.{name}",
                file_path=f"models/{name}.sql",
                raw_sql=sql,
                compiled_sql=sql,
            )
            for name, sql in sql_by_name.items()
        },
    )


def _grain_of(sql: str) -> tuple[GrainSource, tuple[str, ...]]:
    grain = infer_grains(_snapshot(m=sql))["m"]
    return grain.source, grain.columns


def test_group_by_proves_grain() -> None:
    source, columns = _grain_of("select a, b, sum(x) as s from t group by a, b")
    assert source is GrainSource.STRUCTURAL
    assert columns == ("a", "b")


def test_positional_group_by_resolves_against_select_list() -> None:
    source, columns = _grain_of("select a, b, sum(x) as s from t group by 1, 2")
    assert source is GrainSource.STRUCTURAL
    assert columns == ("a", "b")


def test_select_distinct_proves_grain() -> None:
    source, columns = _grain_of("select distinct a, b from t")
    assert source is GrainSource.STRUCTURAL
    assert columns == ("a", "b")


def test_grain_resolves_through_pass_through_cte() -> None:
    """The shape almost every real dbt model uses.

    The GROUP BY lives in the last CTE and the final SELECT just passes it through.
    Reading only the outermost SELECT would report unknown for most of a project.
    """
    source, columns = _grain_of(
        """
        with base as (select * from raw),
             agg as (select a, b, sum(x) as s from base group by a, b)
        select * from agg
        """
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("a", "b")


def test_row_number_dedup_proves_grain() -> None:
    source, columns = _grain_of(
        """
        with ranked as (
            select *, row_number() over (partition by k order by ts desc) as rn from t
        )
        select * from ranked where rn = 1
        """
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("k",)


def test_unfiltered_row_number_does_not_prove_grain() -> None:
    """A rank that is never filtered deduplicates nothing.

    Treating it as if it did would assert a uniqueness the rows do not have — the
    exact failure that lets a fan-out through unflagged.
    """
    source, _ = _grain_of("select *, row_number() over (partition by k order by ts) as rn from t")
    assert source is GrainSource.UNKNOWN


def test_projection_dropping_a_key_column_breaks_the_grain() -> None:
    """If the outer select drops part of the key, the inner grain does not survive."""
    source, _ = _grain_of(
        """
        with agg as (select a, b, sum(x) as s from t group by a, b)
        select a, s from agg
        """
    )
    assert source is GrainSource.UNKNOWN


def test_join_in_the_outer_select_blocks_pass_through() -> None:
    """A join is exactly where grain changes, so it must never be inherited across."""
    source, _ = _grain_of(
        """
        with agg as (select a, b, sum(x) as s from t group by a, b)
        select agg.a, agg.b, o.z from agg join other o on agg.a = o.a
        """
    )
    assert source is GrainSource.UNKNOWN


def test_where_clause_preserves_grain() -> None:
    """Filtering removes rows; it cannot make a unique key non-unique."""
    source, columns = _grain_of(
        """
        with agg as (select a, b, sum(x) as s from t group by a, b)
        select * from agg where s > 0
        """
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("a", "b")


def test_incremental_unique_key_is_used_when_no_structure_proves_grain() -> None:
    """A config survives even in a project with zero declared tests."""
    snapshot = _snapshot()
    snapshot.models["m"] = ModelNode(
        name="m",
        unique_id="model.test.m",
        file_path="models/m.sql",
        raw_sql="select * from t",
        compiled_sql="select * from t",
        unique_key=("id",),
    )
    grain = infer_grains(snapshot)["m"]
    assert grain.source is GrainSource.CONFIG
    assert grain.columns == ("id",)


def test_grain_propagates_through_a_pass_through_model() -> None:
    snapshot = _snapshot(
        upstream="select a, b, sum(x) as s from t group by a, b",
        downstream="select * from upstream",
    )
    snapshot.models["downstream"] = snapshot.models["downstream"].model_copy(
        update={"depends_on_models": ("model.test.upstream",)}
    )
    grains = infer_grains(snapshot)
    assert grains["downstream"].source is GrainSource.PROPAGATED
    assert grains["downstream"].columns == ("a", "b")


def test_ambiguous_naming_does_not_produce_a_guessed_composite_key() -> None:
    """Several key-shaped columns is a guess, and a guessed composite key is worse
    than admitting we do not know."""
    snapshot = _snapshot(m="select * from t")
    snapshot.models["m"] = snapshot.models["m"].model_copy(
        update={
            "columns": (
                ColumnSchema(name="order_id"),
                ColumnSchema(name="customer_id"),
            )
        }
    )
    assert infer_grains(snapshot)["m"].source is GrainSource.UNKNOWN


def test_unknown_grain_is_not_marked_proven() -> None:
    """The property that keeps fan-out rules honest."""
    snapshot = _snapshot(m="select * from t")
    assert not infer_grains(snapshot)["m"].is_proven


@pytest.mark.parametrize("sql", ["this is not sql at all", ""])
def test_unparseable_sql_degrades_to_unknown(sql: str) -> None:
    """A parse failure must not become a confident grain claim."""
    assert infer_grains(_snapshot(m=sql))["m"].source is GrainSource.UNKNOWN
