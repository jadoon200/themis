"""Stage 3 — execution and measurement.

The property under test is that measurement either produces a true number or produces
nothing. A measured finding carries more weight than any other kind, so a wrong one is
worse than no measurement at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from themis.execute.differ import diff_tables, measure_grain
from themis.execute.profiles import (
    ProfileError,
    project_profile_name,
    read_profile,
    write_profile_for_schema,
)
from themis.execute.warehouse import TableShape
from themis.models import Grain, GrainSource


class FakeWarehouse:
    """A warehouse whose contents are declared rather than built."""

    def __init__(self, tables: dict[tuple[str, str], TableShape], values: dict | None = None):
        self._tables = tables
        self._values = values or {}

    def shape(self, schema: str, table: str) -> TableShape:
        return self._tables.get((schema, table), TableShape(exists=False))

    def sums(self, schema: str, table: str, columns: tuple[str, ...]) -> dict[str, float]:
        return self._values.get(("sums", schema, table), {})

    def null_rates(self, schema: str, table: str, columns: tuple[str, ...]) -> dict[str, float]:
        return self._values.get(("nulls", schema, table), {})

    def distinct_count(self, schema: str, table: str, columns: tuple[str, ...]) -> int | None:
        return self._values.get(("distinct", schema, table))

    def close(self) -> None:
        return None


def _shape(rows: int, **columns: str) -> TableShape:
    return TableShape(exists=True, row_count=rows, column_types=dict(columns))


def _candidate(*columns: str) -> Grain:
    return Grain(model_name="m", columns=columns, source=GrainSource.HEURISTIC)


def _diff(client: FakeWarehouse, model: str = "m"):
    return diff_tables(client, model, base_schema="b", head_schema="h", max_rows=1_000_000)


def test_row_count_growth_is_measured() -> None:
    client = FakeWarehouse(
        {("b", "m"): _shape(18, amount_usd="DECIMAL"), ("h", "m"): _shape(54, amount_usd="DECIMAL")}
    )
    delta = _diff(client)
    assert delta.row_delta == 36
    assert delta.is_material


def test_identical_tables_are_not_material() -> None:
    """The control case. A refactor that measures as changed is a false positive with
    a number attached, which is worse than one without."""
    shape = _shape(15, amount_usd="DECIMAL")
    client = FakeWarehouse(
        {("b", "m"): shape, ("h", "m"): shape},
        {
            ("sums", "b", "m"): {"amount_usd": 13_112_347.70},
            ("sums", "h", "m"): {"amount_usd": 13_112_347.70},
        },
    )
    delta = _diff(client)
    assert delta.row_delta == 0
    assert not delta.is_material


def test_stable_row_count_with_moved_total_is_still_material() -> None:
    """The regulatory-mart case, and the reason row counts alone are not enough.

    An aggregate has a fixed grain, so an upstream fan-out leaves its row count
    untouched while tripling the money. Checking rows only would report no change on
    precisely the table that reaches the regulator.
    """
    shape = _shape(9, revenue_usd="DECIMAL")
    client = FakeWarehouse(
        {("b", "m"): shape, ("h", "m"): shape},
        {
            ("sums", "b", "m"): {"revenue_usd": 13_112_347.70},
            ("sums", "h", "m"): {"revenue_usd": 39_318_036.05},
        },
    )
    delta = _diff(client)
    assert delta.row_delta == 0
    assert delta.is_material


def test_retyped_column_is_detected() -> None:
    client = FakeWarehouse(
        {
            ("b", "m"): _shape(5, amount_usd="DECIMAL(38,6)"),
            ("h", "m"): _shape(5, amount_usd="DOUBLE"),
        }
    )
    delta = _diff(client)
    assert delta.columns_retyped["amount_usd"] == ("DECIMAL(38,6)", "DOUBLE")
    assert delta.is_material


def test_missing_table_on_one_side_degrades_rather_than_inventing() -> None:
    client = FakeWarehouse({("h", "m"): _shape(5, x="INTEGER")})
    delta = _diff(client)
    assert delta.rows_before is None
    assert delta.rows_after == 5


def test_absent_everywhere_yields_an_empty_delta() -> None:
    delta = _diff(FakeWarehouse({}))
    assert delta.rows_before is None and delta.rows_after is None
    assert not delta.is_material


def test_oversized_tables_skip_aggregates_but_keep_row_counts() -> None:
    client = FakeWarehouse(
        {
            ("b", "m"): _shape(10_000_000, amount_usd="DECIMAL"),
            ("h", "m"): _shape(10_000_001, amount_usd="DECIMAL"),
        }
    )
    delta = diff_tables(client, "m", base_schema="b", head_schema="h", max_rows=1000)
    assert delta.row_delta == 1
    assert delta.sum_deltas == {}


# --- grain measurement --------------------------------------------------------


def test_measured_grain_reports_the_exact_multiplier() -> None:
    """What inference cannot do: 3.00 rows per key, not 'may fan out'."""
    client = FakeWarehouse(
        {("h", "m"): _shape(45, entry_id="VARCHAR")}, {("distinct", "h", "m"): 15}
    )
    grain = measure_grain(
        client,
        "m",
        schema="h",
        candidate=Grain(model_name="m", columns=("entry_id",), source=GrainSource.HEURISTIC),
    )
    assert grain is not None
    assert grain.source is GrainSource.MEASURED
    assert grain.rows_per_key == pytest.approx(3.0)
    assert "does NOT identify a row" in (grain.note or "")


def test_measurement_confirms_a_genuinely_unique_key() -> None:
    client = FakeWarehouse(
        {("h", "m"): _shape(15, entry_id="VARCHAR")}, {("distinct", "h", "m"): 15}
    )
    grain = measure_grain(
        client,
        "m",
        schema="h",
        candidate=Grain(model_name="m", columns=("entry_id",), source=GrainSource.HEURISTIC),
    )
    assert grain is not None
    assert grain.rows_per_key == pytest.approx(1.0)
    assert "does NOT" not in (grain.note or "")


def test_a_derived_key_absent_from_the_table_is_not_measured() -> None:
    """Never report a measurement of a column that does not exist."""
    client = FakeWarehouse({("h", "m"): _shape(10, other="VARCHAR")}, {("distinct", "h", "m"): 10})
    assert (
        measure_grain(
            client,
            "m",
            schema="h",
            candidate=Grain(model_name="m", columns=("entry_id",), source=GrainSource.HEURISTIC),
        )
        is None
    )


def test_unknown_grain_is_not_measured() -> None:
    client = FakeWarehouse({("h", "m"): _shape(10)}, {("distinct", "h", "m"): 10})
    assert (
        measure_grain(
            client,
            "m",
            schema="h",
            candidate=Grain(model_name="m", columns=(), source=GrainSource.UNKNOWN),
        )
        is None
    )


# --- profile generation -------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "dbt_project.yml").write_text(yaml.safe_dump({"name": "p", "profile": "myprofile"}))
    (tmp_path / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "myprofile": {
                    "target": "dev",
                    "outputs": {
                        "dev": {"type": "duckdb", "path": "warehouse.duckdb", "schema": "main"}
                    },
                }
            }
        )
    )
    return tmp_path


def test_generated_profile_uses_the_name_dbt_project_expects(project: Path, tmp_path: Path) -> None:
    """dbt looks the profile up by name; any other name is simply not found."""
    out = write_profile_for_schema(
        project, tmp_path / "gen", target="dev", schema="themis_head", anchor_dir=project
    )
    written = yaml.safe_load((out / "profiles.yml").read_text())
    assert project_profile_name(project) in written


def test_generated_profile_overrides_only_the_schema(project: Path, tmp_path: Path) -> None:
    out = write_profile_for_schema(
        project, tmp_path / "gen", target="dev", schema="themis_head", anchor_dir=project
    )
    output = yaml.safe_load((out / "profiles.yml").read_text())["myprofile"]["outputs"]["dev"]
    assert output["schema"] == "themis_head"
    assert output["type"] == "duckdb"


def test_relative_database_path_anchors_to_the_original_project(
    project: Path, tmp_path: Path
) -> None:
    """The bug this guards against silently compared against nothing.

    The base revision builds inside a temporary worktree. Resolving a relative database
    path against *that* directory creates a fresh, empty database which is deleted with
    the worktree — so the diff runs against an absent table and reports whatever it
    likes. Both revisions must anchor to the real project.
    """
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "dbt_project.yml").write_text((project / "dbt_project.yml").read_text())
    (worktree / "profiles.yml").write_text((project / "profiles.yml").read_text())

    out = write_profile_for_schema(
        worktree, tmp_path / "gen", target="dev", schema="themis_base", anchor_dir=project
    )
    path = yaml.safe_load((out / "profiles.yml").read_text())["myprofile"]["outputs"]["dev"]["path"]
    assert path == str((project / "warehouse.duckdb").resolve())
    assert "worktree" not in path


def test_unknown_target_is_refused(project: Path) -> None:
    with pytest.raises(ProfileError):
        read_profile(project, target="nonexistent")
