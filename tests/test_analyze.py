"""Semantic diffing and macro impact.

The property that matters most here is the negative one: reformatting must produce
nothing. A reviewer who gets flagged for whitespace stops reading the flags, and then
the real finding underneath goes unread too.
"""

from __future__ import annotations

from themis.analyze.parse import join_kind, semantic_diff
from themis.models import Backend
from themis.snapshot import MacroNode, ModelNode, ProjectSnapshot
from sqlglot import exp

from themis.analyze.parse import find_joins, parse_sql

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
