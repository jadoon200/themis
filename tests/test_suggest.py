"""Suggested tests — and, more importantly, the ones THEMIS refuses to suggest.

A suggested test that fails on first run is worse than no suggestion at all: it
teaches the reader that these are guesses, and the next one gets ignored. So the
refusals are the load-bearing behaviour here, not the emissions.
"""

from __future__ import annotations

from themis.analyze.suggest import render_yaml, suggest_tests
from themis.models import Backend, Grain, GrainSource
from themis.snapshot import DeclaredTest, ModelNode, ProjectSnapshot


def _snapshot(*, tests: tuple[DeclaredTest, ...] = ()) -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "m": ModelNode(
                name="m",
                unique_id="model.t.m",
                file_path="models/m.sql",
                compiled_sql="select a, b from t group by a, b",
            )
        },
        tests=tests,
    )


def _grain(
    source: GrainSource, columns: tuple[str, ...] = ("a",), *, rows_per_key: float | None = None
) -> dict[str, Grain]:
    grain = Grain(model_name="m", columns=columns, source=source, rows_per_key=rows_per_key)
    return {"m": grain}


def test_a_structural_grain_is_offered() -> None:
    suggestions = suggest_tests(_snapshot(), _grain(GrainSource.STRUCTURAL))
    assert [s.model_name for s in suggestions] == ["m"]
    assert suggestions[0].test_name == "unique"


def test_a_composite_key_uses_the_package_test() -> None:
    suggestions = suggest_tests(_snapshot(), _grain(GrainSource.STRUCTURAL, ("a", "b")))
    assert suggestions[0].test_name == "dbt_utils.unique_combination_of_columns"
    assert "combination_of_columns" in suggestions[0].yaml
    assert "dbt_utils" in render_yaml(suggestions)


def test_a_heuristic_grain_is_never_offered() -> None:
    """Naming raises a question. Asserting an answer to it would be a guess."""
    assert suggest_tests(_snapshot(), _grain(GrainSource.HEURISTIC)) == []


def test_an_unknown_grain_is_never_offered() -> None:
    assert suggest_tests(_snapshot(), _grain(GrainSource.UNKNOWN, ())) == []


def test_a_measured_key_that_is_not_unique_is_refused() -> None:
    """Counting settled it: this key does not identify a row.

    Emitting the test anyway would assert something already known to be false.
    """
    grains = _grain(GrainSource.MEASURED, ("a",), rows_per_key=1.4)
    assert suggest_tests(_snapshot(), grains) == []


def test_a_measured_key_that_is_unique_is_offered() -> None:
    grains = _grain(GrainSource.MEASURED, ("a",), rows_per_key=1.0)
    suggestions = suggest_tests(_snapshot(), grains)
    assert suggestions and "measured" in suggestions[0].evidence


def test_an_existing_uniqueness_test_suppresses_the_suggestion() -> None:
    tests = (DeclaredTest(test_name="unique", model_name="m", columns=("a",)),)
    assert suggest_tests(_snapshot(tests=tests), _grain(GrainSource.STRUCTURAL)) == []


def test_a_key_the_model_does_not_emit_is_dropped() -> None:
    """A GROUP BY inside a CTE can name a column the final SELECT never projects.

    Emitted, that test would fail to compile rather than fail honestly.
    """
    suggestions = suggest_tests(
        _snapshot(), _grain(GrainSource.STRUCTURAL, ("a",)), outputs={"m": ("b", "c")}
    )
    assert suggestions == []


def test_nothing_to_suggest_renders_nothing() -> None:
    assert render_yaml([]) == ""
