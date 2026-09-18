"""Narrowing what Stage 3 builds, and — mostly — refusing to.

This is the one thing in THEMIS whose failure is silence. Every other refusal errs towards
reporting too much; a wrong narrowing means a model is never built, never compared, and
looks exactly like a model that did not move. So the tests that matter here are the ones
where it declines: a filter change, an untraced model, a changed seed, a star.

The shape it has to work on is the one every dbt model has — a chain of CTEs ending in
`select * from final` — because reading only the outermost select answers "a star, nothing
can be confined" for an entire project, and a narrowing that never applies is not a feature.
"""

from __future__ import annotations

from themis.analyze.impact import changed_outputs, dirty_columns, narrow
from themis.analyze.lineage import LineageIndex
from themis.models import Backend
from themis.snapshot import ModelNode, ProjectSnapshot

STAGING = """
select
    entry_id,
    account_id,
    amount_usd,
    currency_code
from raw_entries
"""

# The shape a real dbt model has: work in a CTE, `select *` at the end.
MART_AMOUNT = """
with entries as (select * from stg_entries),
totals as (
    select account_id, sum(amount_usd) as total_usd from entries group by account_id
)
select * from totals
"""

MART_COUNT = """
with entries as (select * from stg_entries),
counted as (
    select account_id, count(*) as entry_count from entries group by account_id
)
select * from counted
"""


def _snapshot(**sql_by_name: str) -> ProjectSnapshot:
    models = {
        name: ModelNode(
            name=name,
            unique_id=f"model.t.{name}",
            file_path=f"models/{name}.sql",
            raw_sql=sql,
            compiled_sql=sql,
        )
        for name, sql in sql_by_name.items()
    }
    for name, model in models.items():
        if name != "stg_entries":
            models[name] = model.model_copy(update={"depends_on_models": ("model.t.stg_entries",)})
    child = {"stg_entries": tuple(sorted(n for n in models if n != "stg_entries"))}
    return ProjectSnapshot(revision="r", backend=Backend.MANIFEST, models=models, child_map=child)


def test_a_changed_column_is_named_through_the_shape_every_dbt_model_has() -> None:
    changed = MART_AMOUNT.replace("sum(amount_usd)", "sum(amount_usd) * 100")
    assert changed_outputs(MART_AMOUNT, changed, dialect="duckdb") == ("total_usd",)


def test_an_identical_model_has_no_dirty_columns() -> None:
    assert changed_outputs(MART_AMOUNT, MART_AMOUNT, dialect="duckdb") == ()


def test_a_filter_cannot_be_confined_to_columns() -> None:
    """A different WHERE changes which rows exist, and that reaches every column."""
    filtered = MART_AMOUNT.replace("from entries", "from entries where amount_usd > 0")
    assert changed_outputs(MART_AMOUNT, filtered, dialect="duckdb") is None


def test_a_changed_join_cannot_be_confined_either() -> None:
    joined = MART_AMOUNT.replace(
        "from entries", "from entries join accounts on entries.account_id = accounts.id"
    )
    assert changed_outputs(MART_AMOUNT, joined, dialect="duckdb") is None


def test_a_star_in_the_projection_confines_nothing() -> None:
    assert changed_outputs("select * from t", "select * from t where x", dialect="duckdb") is None


def test_a_model_that_reads_no_changed_column_is_skipped() -> None:
    """The saving: a mart counting rows does not read the amount that changed."""
    snapshot = _snapshot(stg_entries=STAGING, mart_amount=MART_AMOUNT, mart_count=MART_COUNT)
    graph = LineageIndex(before_snapshot=snapshot, after_snapshot=snapshot).after

    result = narrow(
        changed={"stg_entries": ("amount_usd",)},
        candidates={"mart_amount", "mart_count"},
        graph=graph,
        snapshot=snapshot,
    )
    assert result.refused is None
    assert result.kept == frozenset({"mart_amount"})
    assert set(result.excluded) == {"mart_count"}
    assert result.applied


def test_nothing_is_narrowed_when_a_change_is_not_confined_to_columns() -> None:
    snapshot = _snapshot(stg_entries=STAGING, mart_amount=MART_AMOUNT, mart_count=MART_COUNT)
    graph = LineageIndex(before_snapshot=snapshot, after_snapshot=snapshot).after

    result = narrow(
        changed={"stg_entries": None},
        candidates={"mart_amount", "mart_count"},
        graph=graph,
        snapshot=snapshot,
    )
    assert result.refused and "not confined" in result.refused
    assert result.kept == frozenset({"mart_amount", "mart_count"})
    assert not result.applied


def test_a_changed_seed_stops_narrowing_outright() -> None:
    """A data change has no columns to confine it to — every column below it may move."""
    snapshot = _snapshot(stg_entries=STAGING, mart_amount=MART_AMOUNT)
    graph = LineageIndex(before_snapshot=snapshot, after_snapshot=snapshot).after

    result = narrow(
        changed={"stg_entries": ("amount_usd",)},
        candidates={"mart_amount"},
        graph=graph,
        snapshot=snapshot,
        changed_seeds=("raw_entries",),
    )
    assert result.refused and "seed" in result.refused
    assert result.kept == frozenset({"mart_amount"})


def test_an_untraced_model_stops_narrowing_outright() -> None:
    """Incomplete lineage cannot prove an absence, and absence is what this rests on."""
    snapshot = _snapshot(stg_entries=STAGING, mart_amount=MART_AMOUNT)
    graph = LineageIndex(before_snapshot=snapshot, after_snapshot=snapshot).after
    graph.unresolved["mart_amount"] = "could not be parsed"

    result = narrow(
        changed={"stg_entries": ("amount_usd",)},
        candidates={"mart_amount"},
        graph=graph,
        snapshot=snapshot,
    )
    assert result.refused and "lineage" in result.refused
    assert result.kept == frozenset({"mart_amount"})


def test_no_lineage_at_all_narrows_nothing() -> None:
    snapshot = _snapshot(stg_entries=STAGING)
    result = narrow(changed={}, candidates={"a"}, graph=None, snapshot=snapshot)
    assert result.refused and result.kept == frozenset({"a"})


def test_dirty_columns_reports_a_model_it_cannot_read_as_unconfined() -> None:
    """A model with no compiled SQL on one side is not "unchanged"."""
    before = _snapshot(stg_entries=STAGING)
    after = ProjectSnapshot(revision="r", backend=Backend.MANIFEST, models={})
    assert dirty_columns(before, after, {"stg_entries"}, dialect="duckdb") == {"stg_entries": None}


def test_a_removed_column_is_traced_in_the_revision_where_it_still_exists() -> None:
    """The failure the corpus caught, and the mistake F6 was already written around.

    Who reads a column this change *removed* has an answer only in the before graph. In
    the after graph the column is gone, nothing resolves to it, every model below looks
    untouched — and the mart whose build the removal was supposed to break is skipped. The
    corpus case that declares "this must fail to build" came back built.
    """
    before = _snapshot(stg_entries=STAGING, mart_amount=MART_AMOUNT, mart_count=MART_COUNT)
    after_staging = STAGING.replace("    amount_usd,\n", "")
    after = _snapshot(stg_entries=after_staging, mart_amount=MART_AMOUNT, mart_count=MART_COUNT)

    index = LineageIndex(before_snapshot=before, after_snapshot=after)
    result = narrow(
        changed={"stg_entries": ("amount_usd",)},
        candidates={"mart_amount", "mart_count"},
        graph=index.after,
        before_graph=index.before,
        snapshot=after,
    )
    # mart_amount reads the removed column in the revision that still has it.
    assert "mart_amount" in result.kept
    assert "mart_amount" not in result.excluded


def test_the_after_graph_alone_would_have_missed_it() -> None:
    """The control for the control: without the before graph, the bug comes back."""
    before = _snapshot(stg_entries=STAGING, mart_amount=MART_AMOUNT, mart_count=MART_COUNT)
    after_staging = STAGING.replace("    amount_usd,\n", "")
    after = _snapshot(stg_entries=after_staging, mart_amount=MART_AMOUNT, mart_count=MART_COUNT)

    index = LineageIndex(before_snapshot=before, after_snapshot=after)
    blind = narrow(
        changed={"stg_entries": ("amount_usd",)},
        candidates={"mart_amount", "mart_count"},
        graph=index.after,
        snapshot=after,
    )
    assert "mart_amount" in blind.excluded
