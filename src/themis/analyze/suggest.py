"""Emit the uniqueness tests the project never declared.

THEMIS derives each model's grain because nothing asserts it. Having derived it, the
obvious next move is to hand it back: *this model appears unique on
``(account_id, period_end)``; nothing checks that; here is the test.*

On a project with no test coverage that is arguably worth as much as the review, and
it compounds — every test accepted becomes a ``DECLARED_TEST`` grain, which grounds
the next review more firmly than any inference can.

The discipline that makes it useful is refusing to suggest anything that might fail.
A suggested test that turns red on first run is worse than no suggestion: it teaches
the reader that these are guesses. So only proven grains qualify, a measured
multiplier above 1.0 disqualifies outright — the key does not identify a row, and
asserting it would be asserting something false — and a key naming a column the model
does not emit is dropped rather than printed.
"""

from __future__ import annotations

from dataclasses import dataclass

from themis.models import Grain, GrainSource
from themis.snapshot import DeclaredTest, ProjectSnapshot

# Tests that assert uniqueness under one name or another. A model already carrying one
# of these on its key needs nothing from us.
_UNIQUENESS_TESTS = frozenset(
    {
        "unique",
        "unique_combination_of_columns",
        "dbt_utils.unique_combination_of_columns",
    }
)


@dataclass(frozen=True)
class TestSuggestion:
    """One uniqueness assertion THEMIS is prepared to stand behind."""

    model_name: str
    columns: tuple[str, ...]
    basis: GrainSource
    rows_per_key: float | None = None

    @property
    def test_name(self) -> str:
        """dbt has a built-in for one column and a package test for several."""
        return "unique" if len(self.columns) == 1 else "dbt_utils.unique_combination_of_columns"

    @property
    def evidence(self) -> str:
        """Why this key, in one line — the reader has to judge it before accepting."""
        if self.basis is GrainSource.MEASURED and self.rows_per_key is not None:
            return f"measured: {self.rows_per_key:.2f} rows per key"
        if self.basis is GrainSource.STRUCTURAL:
            return "derived from the model's own GROUP BY / DISTINCT / dedup"
        if self.basis is GrainSource.CONFIG:
            return "declared as the incremental unique_key"
        return f"grain source: {self.basis.value}"

    @property
    def yaml(self) -> str:
        """A ``schema.yml`` fragment, ready to paste."""
        if len(self.columns) == 1:
            return (
                f"  - name: {self.model_name}\n"
                f"    columns:\n"
                f"      - name: {self.columns[0]}\n"
                f"        tests:\n"
                f"          - unique\n"
                f"          - not_null\n"
            )
        listed = "\n".join(f"            - {column}" for column in self.columns)
        return (
            f"  - name: {self.model_name}\n"
            f"    tests:\n"
            f"      - dbt_utils.unique_combination_of_columns:\n"
            f"          combination_of_columns:\n"
            f"{listed}\n"
        )


def _already_asserted(
    tests: tuple[DeclaredTest, ...], model: str, columns: tuple[str, ...]
) -> bool:
    """Whether something already checks this key.

    Matched on the column set rather than the test name, so a uniqueness test spelled
    a different way still counts and the suggestion stays quiet.
    """
    wanted = set(columns)
    for test in tests:
        if test.model_name != model:
            continue
        if test.test_name.split(".")[-1] not in _UNIQUENESS_TESTS:
            continue
        if not test.columns or set(test.columns) == wanted:
            return True
    return False


def suggest_tests(
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    *,
    outputs: dict[str, tuple[str, ...]] | None = None,
) -> list[TestSuggestion]:
    """Every uniqueness test worth proposing, for models that assert nothing.

    ``outputs`` is the column list per model, from column lineage. When supplied, a
    key naming a column the model does not actually emit is dropped: a grain derived
    from a GROUP BY inside a CTE can name something the final SELECT never projects,
    and a test referencing a missing column fails to compile rather than failing
    honestly.
    """
    suggestions: list[TestSuggestion] = []
    for name, grain in sorted(grains.items()):
        if not grain.columns or not grain.is_proven:
            continue
        if grain.source is GrainSource.DECLARED_TEST:
            continue  # it is already declared; that is where the grain came from
        if grain.rows_per_key is not None and grain.rows_per_key > 1.0:
            # Measured and not unique. Suggesting this would assert something false.
            continue
        if _already_asserted(snapshot.tests, name, grain.columns):
            continue
        emitted = (outputs or {}).get(name)
        if emitted is not None and not set(grain.columns) <= set(emitted):
            continue
        suggestions.append(
            TestSuggestion(
                model_name=name,
                columns=grain.columns,
                basis=grain.source,
                rows_per_key=grain.rows_per_key,
            )
        )
    return suggestions


def render_yaml(suggestions: list[TestSuggestion]) -> str:
    """The suggestions as one ``schema.yml`` document.

    Composite keys use ``dbt_utils``, so the dependency is stated rather than assumed —
    pasting a test whose package is not installed is a compile error, not a review.
    """
    if not suggestions:
        return ""
    body = "".join(suggestion.yaml for suggestion in suggestions)
    header = "version: 2\n\nmodels:\n"
    needs_utils = any(len(s.columns) > 1 for s in suggestions)
    note = (
        "\n# Composite keys use dbt_utils.unique_combination_of_columns — add dbt_utils\n"
        "# to packages.yml if it is not already there.\n"
        if needs_utils
        else ""
    )
    return header + body + note
