"""Column-level lineage — who actually reads a column.

Every test here is a case the name search this replaced gets wrong, in one direction
or the other. That is the point of the component: model-granular impact over-states
every change, and grepping downstream SQL for the column name both invents consumers
and misses real ones.
"""

from __future__ import annotations

from themis.analyze.lineage import ColumnRef, build_column_graph
from themis.models import Backend
from themis.snapshot import ModelNode, ProjectSnapshot


def _snapshot(**sql_by_name: str) -> ProjectSnapshot:
    """A project whose DAG is inferred from which models name which.

    Models address each other by bare name here rather than by a compiled relation,
    which is the shape a hand-written fixture takes; the resolver accepts both.
    """
    models = {}
    for name, sql in sql_by_name.items():
        depends = tuple(
            f"model.test.{other}" for other in sql_by_name if other != name and other in sql
        )
        models[name] = ModelNode(
            name=name,
            unique_id=f"model.test.{name}",
            file_path=f"models/{name}.sql",
            raw_sql=sql,
            compiled_sql=sql,
            depends_on_models=depends,
        )
    child_map: dict[str, tuple[str, ...]] = {}
    for name, model in models.items():
        for dep in model.depends_on_models:
            parent = dep.split(".")[-1]
            child_map[parent] = (*child_map.get(parent, ()), name)
    return ProjectSnapshot(
        revision="test", backend=Backend.MANIFEST, models=models, child_map=child_map
    )


def test_consumer_reading_through_a_star_is_found() -> None:
    """The false negative that matters most.

    dbt projects are built out of pass-through CTEs, so the model that breaks when a
    column disappears usually never writes its name anywhere.
    """
    graph = build_column_graph(
        _snapshot(
            stg=("select id, amount, currency from raw_table"),
            mart=("with s as (select * from stg) select id, amount from s"),
        )
    )
    assert graph.consumer_models("stg", "amount") == ("mart",)
    assert ColumnRef("mart", "amount") in graph.feeds[ColumnRef("stg", "amount")]


def test_a_renamed_column_is_still_traced() -> None:
    """The name a reviewer greps for stops existing one hop down."""
    graph = build_column_graph(
        _snapshot(
            stg="select id, amount from raw_table",
            mart="select id, sum(amount) as total_reported from stg group by id",
        )
    )
    assert graph.consumer_models("stg", "amount") == ("mart",)
    assert graph.consumers_of("stg", "amount") == (ColumnRef("mart", "total_reported"),)


def test_a_same_named_column_from_elsewhere_is_not_a_consumer() -> None:
    """The false positive: two upstreams, one column name, one real dependency."""
    graph = build_column_graph(
        _snapshot(
            left="select id, amount from raw_left",
            right="select id, amount from raw_right",
            mart="select left.id, right.amount from left join right on left.id = right.id",
        )
    )
    assert graph.consumer_models("right", "amount") == ("mart",)
    assert graph.consumer_models("left", "amount") == ()


def test_a_join_key_counts_as_a_consumer() -> None:
    """A column can break a model while contributing nothing to its output.

    Projection lineage alone goes silent here, which would be the worst possible place
    to be silent: removing a join key is how a model stops compiling.
    """
    graph = build_column_graph(
        _snapshot(
            rates="select currency, rate_date, rate from raw_rates",
            fact=(
                "select e.id, e.amount * r.rate as amount_usd "
                "from entries as e join rates as r on e.currency = r.currency"
            ),
        )
    )
    assert graph.consumer_models("rates", "currency") == ("fact",)
    assert graph.referencing_models("rates", "currency") == ("fact",)
    # The rate itself is carried forward, so it is a value dependency, not just a
    # reference — the distinction the report shows the reviewer.
    assert graph.referencing_models("rates", "rate") == ()


def test_a_column_only_pulled_in_by_a_star_is_not_a_dependency() -> None:
    """Deleting it upstream produces one column fewer, and nothing breaks."""
    graph = build_column_graph(
        _snapshot(
            stg="select id, amount, note from raw_table",
            mart="with s as (select * from stg) select id, amount from s",
        )
    )
    assert graph.consumer_models("stg", "note") == ()


def test_lineage_is_transitive_across_models() -> None:
    graph = build_column_graph(
        _snapshot(
            stg="select id, amount from raw_table",
            mid="select id, amount * 2 as doubled from stg",
            mart="select id, sum(doubled) as total from mid group by id",
        )
    )
    assert graph.consumer_models("stg", "amount") == ("mart", "mid")
    assert graph.sources_of("mart", "total") == (
        ColumnRef("mid", "doubled"),
        ColumnRef("stg", "amount"),
    )


def test_an_unresolvable_model_reads_as_unknown_not_as_no_consumers() -> None:
    """Silence from a lineage tool is how a breaking change gets approved."""
    graph = build_column_graph(
        _snapshot(
            stg="select id, amount from raw_table",
            broken="select * from some_table_nothing_declares",
        )
    )
    assert not graph.is_traced("broken")
    assert "broken" in graph.unresolved
    assert graph.is_traced("stg")


def test_models_outside_the_trace_set_are_untraced_not_clean() -> None:
    graph = build_column_graph(
        _snapshot(
            stg="select id, amount from raw_table",
            mart="select id, amount from stg",
        ),
        trace={"stg"},
    )
    assert graph.is_traced("stg")
    assert not graph.is_traced("mart")
