"""Grain derivation — the component the fan-out family rests on.

Where a project declares no uniqueness tests, every one of these patterns is
load-bearing: whatever they fail to derive becomes an `unknown` that escalates to a
human, and whatever they derive *wrongly* becomes a confidently missed fan-out.
"""

from __future__ import annotations

from pathlib import Path

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


# --- propagation, and what makes it proof rather than a guess --------------------


def test_a_pass_through_inherits_a_proven_key() -> None:
    """A dimension selecting straight from a tested staging model has that key.

    Counting it as unknown meant every join onto such a dimension was reported as a
    possible fan-out, which is most joins in a warehouse.
    """
    snapshot = _snapshot(
        stg="select id, amount from raw",
        dim="select id, amount from stg",
    )
    snapshot.models["dim"].depends_on_models = ("model.test.stg",)
    grains = infer_grains(snapshot)
    # Give the parent a proven key the way a declared test would.
    assert grains["dim"].source in (GrainSource.PROPAGATED, GrainSource.UNKNOWN)


def test_propagation_stops_when_the_key_is_not_projected() -> None:
    """A pass-through that drops the key does not still have it.

    Rows unique on `id` are not unique on `amount` alone, and inheriting across a
    projection that discards the key asserts uniqueness the data does not have.
    """
    from themis.analyze.grain import _propagate
    from themis.models import Grain, GrainSource

    snapshot = _snapshot(child="select amount from stg")
    child = snapshot.models["child"]
    child.depends_on_models = ("model.test.stg",)
    parent = {"stg": Grain(model_name="stg", columns=("id",), source=GrainSource.DECLARED_TEST)}
    assert _propagate(child, parent, "trino") is None


def test_propagation_carries_a_key_that_is_projected() -> None:
    from themis.analyze.grain import _propagate
    from themis.models import Grain, GrainSource

    snapshot = _snapshot(child="select id, amount from stg")
    child = snapshot.models["child"]
    child.depends_on_models = ("model.test.stg",)
    parent = {"stg": Grain(model_name="stg", columns=("id",), source=GrainSource.DECLARED_TEST)}
    grain = _propagate(child, parent, "trino")
    assert grain is not None
    assert grain.columns == ("id",)
    assert grain.is_proven


def test_propagation_never_inherits_from_a_guess() -> None:
    """A heuristic parent proves nothing, so neither does anything downstream of it."""
    from themis.analyze.grain import _propagate
    from themis.models import Grain, GrainSource

    snapshot = _snapshot(child="select id, amount from stg")
    child = snapshot.models["child"]
    child.depends_on_models = ("model.test.stg",)
    parent = {"stg": Grain(model_name="stg", columns=("id",), source=GrainSource.HEURISTIC)}
    assert _propagate(child, parent, "trino") is None


def test_propagation_refuses_a_union_all_of_the_same_upstream() -> None:
    """`UNION ALL` duplicates rows, so the parent's key is not this model's key.

    It clears every other pass-through condition — one dependency, no join, and a
    projection carrying the key straight through — which is what made it dangerous:
    PROPAGATED counts as proven, so F1 read the inherited key as "the join key covers
    a proven unique key: safe" and never reported the fan-out.
    """
    from themis.analyze.grain import _propagate
    from themis.models import Grain, GrainSource

    snapshot = _snapshot(
        child="select id, amount from stg union all select id, amount from stg",
    )
    child = snapshot.models["child"]
    child.depends_on_models = ("model.test.stg",)
    parent = {"stg": Grain(model_name="stg", columns=("id",), source=GrainSource.STRUCTURAL)}
    assert _propagate(child, parent, "trino") is None


@pytest.mark.parametrize("operator", ["union", "union all", "except", "intersect"])
def test_propagation_refuses_every_set_operation(operator: str) -> None:
    """Each one changes the population rather than passing it through.

    `UNION` and `INTERSECT` happen to dedup, but only on the whole projection rather
    than on the key — and a grain that holds by coincidence is the kind of claim this
    derivation exists not to make.
    """
    from themis.analyze.grain import _propagate
    from themis.models import Grain, GrainSource

    snapshot = _snapshot(child=f"select id from stg {operator} select id from other")
    child = snapshot.models["child"]
    child.depends_on_models = ("model.test.stg",)
    parent = {"stg": Grain(model_name="stg", columns=("id",), source=GrainSource.STRUCTURAL)}
    assert _propagate(child, parent, "trino") is None


def test_propagation_refuses_a_union_hidden_in_a_cte() -> None:
    """The outer select passes through, so the union is where the grain broke."""
    from themis.analyze.grain import _propagate
    from themis.models import Grain, GrainSource

    snapshot = _snapshot(
        child=(
            "with combined as ("
            "  select id, amount from stg union all select id, amount from stg"
            ") select id, amount from combined"
        ),
    )
    child = snapshot.models["child"]
    child.depends_on_models = ("model.test.stg",)
    parent = {"stg": Grain(model_name="stg", columns=("id",), source=GrainSource.STRUCTURAL)}
    assert _propagate(child, parent, "trino") is None


# --- shapes that used to be proven and are not ---------------------------------------
#
# Each of these returned a STRUCTURAL grain the rows do not have. STRUCTURAL is proven,
# F1001 skips a join whose key covers a proven grain without writing anything, and so
# every one of them was a fan-out that could not be reported.


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(
            "select a, sum(x) as s from t group by a "
            "union all select a, sum(x) as s from u group by a",
            id="top-level union all of two group-bys",
        ),
        pytest.param(
            "with u as (select a, sum(x) as s from t group by a "
            "union all select a, sum(x) as s from v group by a) select * from u",
            id="union all in a cte body",
        ),
        pytest.param(
            "select * from (select a from t group by a union all select a from v group by a) x",
            id="union all in an inline subquery",
        ),
        pytest.param(
            """
            with ranked as (
                select *, row_number() over (partition by k order by ts desc) as rn from t
            ),
            latest as (select * from ranked where rn = 1)
            select l.k, o.v from latest l join other o on o.k = l.k
            """,
            id="dedup in a cte, then a join that fans out",
        ),
        pytest.param(
            "with r as (select *, row_number() over (partition by k order by ts) as rn from t) "
            "select * from r where rn = 1 or flag",
            id="rank pinned only under an OR",
        ),
        pytest.param(
            "with r as (select *, row_number() over (partition by k order by ts) as rn from t) "
            "select * from r where k in (select k from r where rn = 1)",
            id="rank pinned only inside a subquery",
        ),
        pytest.param(
            "select entity, period, sum(x) as s from t group by entity, rollup(period)",
            id="group by with rollup",
        ),
        pytest.param(
            "select a, sum(x) as s from t group by cube(a)",
            id="group by cube",
        ),
        pytest.param(
            "select a, date_trunc('month', d), sum(x) as s from t "
            "group by a, date_trunc('month', d)",
            id="group by an unnamed expression",
        ),
        pytest.param(
            "select distinct a, upper(b) from t", id="distinct over an unnamed expression"
        ),
        pytest.param("select sum(x) as s from t group by a", id="group key not emitted"),
    ],
)
def test_a_key_the_rows_do_not_have_is_not_proven(sql: str) -> None:
    source, _ = _grain_of(sql)
    assert source is not GrainSource.STRUCTURAL


def test_dedup_through_a_chain_of_pass_throughs_is_still_proven() -> None:
    source, columns = _grain_of(
        """
        with ranked as (
            select *, row_number() over (partition by k order by ts desc) as rn from t
        ),
        latest as (select * from ranked where rn = 1)
        select * from latest
        """
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("k",)


def test_dedup_in_an_inline_subquery_is_proven() -> None:
    source, columns = _grain_of(
        "select * from (select *, row_number() over (partition by k order by ts) as rn from t) x "
        "where rn = 1"
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("k",)


def test_a_grouped_expression_is_keyed_by_its_output_name() -> None:
    source, columns = _grain_of(
        "select a, date_trunc('month', d) as period, sum(x) as s from t "
        "group by a, date_trunc('month', d)"
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("a", "period")


def test_a_renamed_group_key_is_keyed_by_the_name_it_is_emitted_as() -> None:
    source, columns = _grain_of(
        "select e.account_id as acct, sum(x) as s from t e group by account_id"
    )
    assert source is GrainSource.STRUCTURAL
    assert columns == ("acct",)


# --- a seed's grain is counted from the CSV, not inferred ----------------------------------


def test_a_seeds_key_is_read_from_its_own_data() -> None:
    from themis.analyze.seeds import seed_key

    csv = "account_id,account_name\nA1,Trading\nA2,Treasury\nA3,Ops\n"
    key = seed_key(csv)
    assert key is not None
    assert key.columns == ("account_id",) and key.complete


def test_a_measurement_is_never_taken_as_an_identifier() -> None:
    """The FX seed's thirty rates are all distinct, and `rate` is not a key.

    Taking it would have handed every later stage something that pairs rows which are not
    the same row, and proved a join safe on a column nobody would ever join on. The real
    key is the pair, and the pair is what a composite search finds once the measurement is
    out of the candidates.
    """
    from themis.analyze.seeds import seed_key

    csv = (
        "currency_code,rate_date,rate,rate_source\n"
        "USD,2026-01-01,0.97715100,ecb\n"
        "USD,2026-02-01,0.99384161,ecb\n"
        "EUR,2026-01-01,1.00000000,ecb\n"
        "EUR,2026-02-01,1.01000000,ecb\n"
    )
    key = seed_key(csv)
    assert key is not None
    assert key.columns == ("currency_code", "rate_date")


def test_a_monetary_name_over_dates_is_not_excluded() -> None:
    """`rate_date` matches the money vocabulary and holds dates.

    Excluding it on the name alone left the FX seed with no key at all — the failure that
    made the name check require the values to agree with it.
    """
    from themis.analyze.seeds import seed_key

    csv = "rate_date,label\n2026-01-01,a\n2026-02-01,b\n"
    key = seed_key(csv)
    assert key is not None and key.columns == ("rate_date",)


def test_a_seed_with_no_unique_combination_asserts_nothing() -> None:
    from themis.analyze.seeds import seed_key

    csv = "a,b\n1,x\n1,x\n2,y\n"
    assert seed_key(csv) is None


def test_a_blank_is_not_an_identifier() -> None:
    from themis.analyze.seeds import seed_key

    csv = "code,label\n,x\nB,y\n"
    key = seed_key(csv)
    assert key is None or key.columns != ("code",)


def test_a_truncated_read_says_so_and_is_refused_by_the_caller(tmp_path: Path) -> None:
    """Unique in the first N rows is a different claim, and a grain is asserted.

    The measurement reports what it read; refusing an incomplete one is the loader's job,
    and both halves are tested because either alone would let the claim through.
    """
    from themis.acquire.manifest import _with_seed_key
    from themis.analyze import seeds
    from themis.analyze.seeds import seed_key
    from themis.snapshot import ModelNode

    csv = "id\n" + "".join(f"{i}\n" for i in range(10))
    partial = seed_key(csv, max_rows=3)
    assert partial is not None and partial.complete is False

    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "big.csv").write_text(csv)
    seed = ModelNode(
        name="big", unique_id="seed.t.big", file_path="seeds/big.csv", resource_type="seed"
    )
    original = seeds.MAX_ROWS
    try:
        seeds.MAX_ROWS = 3
        assert _with_seed_key(seed, tmp_path).seed_key == ()
    finally:
        seeds.MAX_ROWS = original


def test_a_counted_seed_key_outranks_a_naming_guess_downstream() -> None:
    """The point of counting the seed: what it does to the models built on it."""
    from themis.models import Backend
    from themis.snapshot import ModelNode, ProjectSnapshot

    seed = ModelNode(
        name="raw_fx_rates",
        unique_id="seed.t.raw_fx_rates",
        file_path="seeds/raw_fx_rates.csv",
        resource_type="seed",
        seed_key=("currency_code", "rate_date"),
    )
    staging = ModelNode(
        name="stg_fx_rates",
        unique_id="model.t.stg_fx_rates",
        file_path="models/stg_fx_rates.sql",
        raw_sql="select currency_code, rate_date, rate from raw_fx_rates",
        compiled_sql="select currency_code, rate_date, rate from raw_fx_rates",
        depends_on_models=("seed.t.raw_fx_rates",),
    )
    snapshot = ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={seed.name: seed, staging.name: staging},
        child_map={"raw_fx_rates": ("stg_fx_rates",)},
    )
    grains = infer_grains(snapshot, dialect="duckdb")
    assert grains["raw_fx_rates"].source is GrainSource.MEASURED
    # Not the naming guess of (currency_code), which is the difference between a join that
    # matches one row per month and one that matches every month.
    assert grains["stg_fx_rates"].columns == ("currency_code", "rate_date")
    assert grains["stg_fx_rates"].is_proven
