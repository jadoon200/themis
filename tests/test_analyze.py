"""Semantic diffing and macro impact.

The property that matters most here is the negative one: reformatting must produce
nothing. A reviewer who gets flagged for whitespace stops reading the flags, and then
the real finding underneath goes unread too.
"""

from __future__ import annotations

from sqlglot import exp

from themis.analyze.parse import find_joins, join_kind, parse_sql, semantic_diff
from themis.models import Backend
from themis.snapshot import MacroNode, ModelNode, ProjectSnapshot

BASE = "select a, b from t inner join u on t.id = u.id where a > 0"


def test_reformatting_is_not_a_change() -> None:
    reformatted = """
        SELECT
            a,
            b
        FROM t
        INNER JOIN u
            ON t.id = u.id
        WHERE a > 0
    """
    assert semantic_diff(BASE, reformatted).is_noop


def test_added_comments_are_not_a_change() -> None:
    commented = """
        -- pull the joined rows
        select a, b from t
        inner join u on t.id = u.id  -- match on id
        where a > 0
    """
    assert semantic_diff(BASE, commented).is_noop


def test_join_type_flip_is_detected_under_reformatting() -> None:
    """The case a text diff buries: a semantic change inside a whitespace change."""
    flipped = """
        SELECT
            a,
            b
        FROM t
        LEFT JOIN u
            ON t.id = u.id
        WHERE a > 0
    """
    assert not semantic_diff(BASE, flipped).is_noop


def test_dropped_predicate_is_detected() -> None:
    diff = semantic_diff(BASE, "select a, b from t inner join u on t.id = u.id")
    assert not diff.is_noop


def test_unqualified_join_reads_as_inner() -> None:
    """sqlglot leaves side and kind empty for a bare JOIN; in Trino that is INNER."""
    joins = find_joins(parse_sql("select 1 from a join b on a.id = b.id"))
    assert join_kind(joins[0]) == "INNER"


def test_left_join_kind_is_reported() -> None:
    joins = find_joins(parse_sql("select 1 from a left join b on a.id = b.id"))
    assert "LEFT" in join_kind(joins[0])


def test_diff_exposes_changed_nodes_by_type() -> None:
    diff = semantic_diff(BASE, "select a, b from t inner join u on t.id = u.other_id")
    assert diff.change_count > 0
    assert diff.nodes_of_type(exp.Column)


def _snapshot_with_macros() -> ProjectSnapshot:
    """A helper macro that no model references directly.

    ``minor_to_major`` is only reachable through ``signed_amount``. Walking direct
    references alone would report an edit to it as touching nothing.
    """
    return ProjectSnapshot(
        revision="test",
        backend=Backend.MANIFEST,
        macros={
            "minor_to_major": MacroNode(
                name="minor_to_major",
                unique_id="macro.d.minor_to_major",
                file_path="macros/money.sql",
                raw_sql="",
            ),
            "signed_amount": MacroNode(
                name="signed_amount",
                unique_id="macro.d.signed_amount",
                file_path="macros/money.sql",
                raw_sql="",
                depends_on_macros=("macro.d.minor_to_major",),
            ),
        },
        models={
            "stg_gl": ModelNode(
                name="stg_gl",
                unique_id="model.d.stg_gl",
                file_path="models/stg_gl.sql",
                raw_sql="select 1",
                compiled_sql="select 1",
                depends_on_macros=("macro.d.signed_amount",),
            ),
            "unrelated": ModelNode(
                name="unrelated",
                unique_id="model.d.unrelated",
                file_path="models/unrelated.sql",
                raw_sql="select 1",
                compiled_sql="select 1",
            ),
        },
    )


def test_macro_impact_is_transitive() -> None:
    snapshot = _snapshot_with_macros()
    assert snapshot.models_using_macro("minor_to_major") == ("stg_gl",)


def test_direct_macro_reference_still_resolves() -> None:
    snapshot = _snapshot_with_macros()
    assert snapshot.models_using_macro("signed_amount") == ("stg_gl",)


def test_macro_impact_does_not_over_reach() -> None:
    """Transitivity must not turn into 'every model'."""
    snapshot = _snapshot_with_macros()
    assert "unrelated" not in snapshot.models_using_macro("minor_to_major")


def test_downstream_walk_is_cycle_safe() -> None:
    """A malformed manifest must not spin the blast-radius walk."""
    snapshot = ProjectSnapshot(
        revision="test",
        backend=Backend.MANIFEST,
        child_map={"a": ("b",), "b": ("a",)},
    )
    assert set(snapshot.downstream_of("a")) == {"a", "b"}


def test_macros_in_a_file_are_all_found() -> None:
    """A macro file defines several macros.

    Resolving a changed file by its filename finds only the macro that happens to
    share the name, so edits to every other macro in that file route to the wrong
    models — or to none at all.
    """
    snapshot = _snapshot_with_macros()
    assert set(snapshot.macros_in_file("macros/money.sql")) == {
        "minor_to_major",
        "signed_amount",
    }


def test_changing_a_macro_file_reaches_models_using_any_macro_in_it() -> None:
    snapshot = _snapshot_with_macros()
    assert snapshot.models_using_macro_file("macros/money.sql") == ("stg_gl",)


def test_an_unrelated_macro_file_reaches_nothing() -> None:
    snapshot = _snapshot_with_macros()
    assert snapshot.models_using_macro_file("macros/other.sql") == ()


def test_macro_file_matches_whether_or_not_the_path_carries_a_prefix() -> None:
    """git reports paths from the repository root; the manifest stores them relative
    to the project. Comparing in one direction only matches nothing."""
    snapshot = _snapshot_with_macros()
    assert snapshot.macros_in_file("macros/money.sql")
    assert snapshot.macros_in_file("demo_project/macros/money.sql")
    assert snapshot.macros_in_file("./macros/money.sql")


def _snapshot_with_generator() -> ProjectSnapshot:
    """A macro that queries at compile time, and one that merely calls it."""
    return ProjectSnapshot(
        revision="test",
        backend=Backend.MANIFEST,
        macros={
            "build_case": MacroNode(
                name="build_case",
                unique_id="macro.d.build_case",
                file_path="macros/gen.sql",
                raw_sql="{% set rows = run_query('select 1') %}{{ rows }}",
            ),
            "wrapper": MacroNode(
                name="wrapper",
                unique_id="macro.d.wrapper",
                file_path="macros/gen.sql",
                raw_sql="{{ build_case() }}",
                depends_on_macros=("macro.d.build_case",),
            ),
        },
        models={
            "generated": ModelNode(
                name="generated",
                unique_id="model.d.generated",
                file_path="models/generated.sql",
                raw_sql="select 1",
                compiled_sql="select 1",
                depends_on_macros=("macro.d.wrapper",),
            ),
            "ordinary": ModelNode(
                name="ordinary",
                unique_id="model.d.ordinary",
                file_path="models/ordinary.sql",
                raw_sql="select 1",
                compiled_sql="select 1",
            ),
        },
    )


def test_a_macro_that_queries_at_compile_time_is_detected() -> None:
    """Its compiled SQL is a function of the data, so a structural diff of a model
    using it reports differences nobody made."""
    snapshot = _snapshot_with_generator()
    assert snapshot.macros["build_case"].reads_data_at_compile_time


def test_a_plain_macro_is_not_flagged() -> None:
    snapshot = _snapshot_with_macros()
    assert not snapshot.macros["signed_amount"].reads_data_at_compile_time


def test_the_property_is_inherited_through_a_calling_macro() -> None:
    """A model calling a wrapper is just as affected as one calling the generator."""
    affected = _snapshot_with_generator().data_dependent_models()
    assert "generated" in affected
    assert affected["generated"] == ("wrapper",)


def test_models_not_using_a_generator_are_unaffected() -> None:
    assert "ordinary" not in _snapshot_with_generator().data_dependent_models()


def test_a_project_without_generators_reports_nothing() -> None:
    assert _snapshot_with_macros().data_dependent_models() == {}


# --- hooks, and the macro call sites that hide what they do ---------------------


def _snapshot_with_hook(hook: str, *, macro_body: str | None = None) -> ProjectSnapshot:
    macros = {}
    if macro_body is not None:
        macros["partition_overwrite_hook"] = MacroNode(
            name="partition_overwrite_hook",
            unique_id="macro.d.partition_overwrite_hook",
            file_path="macros/partitions.sql",
            raw_sql=macro_body,
        )
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        macros=macros,
        models={
            "m": ModelNode(
                name="m",
                unique_id="model.d.m",
                file_path="models/m.sql",
                compiled_sql="select 1",
                materialization="incremental",
                pre_hooks=(hook,),
            )
        },
    )


_OVERWRITE = (
    "{% macro partition_overwrite_hook() %}set session "
    "hive.insert_existing_partitions_behavior = 'OVERWRITE'{% endmacro %}"
)


def test_a_literal_hook_is_read_directly() -> None:
    snapshot = _snapshot_with_hook(
        "set session hive.insert_existing_partitions_behavior = 'OVERWRITE'"
    )
    assert snapshot.overwrites_partitions(snapshot.models["m"])


def test_a_hook_behind_a_macro_call_is_resolved() -> None:
    """dbt records hooks unrendered, so the manifest holds the call, not the setting.

    A macro-heavy project keeps write semantics in exactly one macro rather than
    repeating a session setting in forty configs — so a rule matching the hook text
    directly would read every one of those models as having no hook at all.
    """
    snapshot = _snapshot_with_hook("{{ partition_overwrite_hook() }}", macro_body=_OVERWRITE)
    assert snapshot.overwrites_partitions(snapshot.models["m"])


def test_a_macro_call_naming_nothing_leaves_the_hook_alone() -> None:
    snapshot = _snapshot_with_hook("{{ some_macro_from_a_package() }}")
    assert not snapshot.overwrites_partitions(snapshot.models["m"])
    assert "some_macro_from_a_package" in snapshot.hook_text(snapshot.models["m"])


def test_an_unrelated_hook_does_not_read_as_partition_overwrite() -> None:
    snapshot = _snapshot_with_hook(
        "{{ audit_log() }}",
        macro_body="{% macro audit_log() %}insert into audit select 1{% endmacro %}",
    )
    assert not snapshot.overwrites_partitions(snapshot.models["m"])
