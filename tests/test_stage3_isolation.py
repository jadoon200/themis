"""Stage 3 — measuring only what this run built.

Every test here is a shape of one failure, reproduced before it was fixed: a head build
failed, the previous review's table was still sitting in the shared schema, and the
review reported that table's numbers — a six-fold revenue jump — at MEASURED confidence
against a change that never built. A measurement is the strongest claim this tool makes,
so the property under test is that a model this run did not build is never measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from themis.acquire.dbt_runner import node_statuses
from themis.execute.runner import BuildOutcome, ExecutionResult, _build, _measure
from themis.execute.warehouse import Relation, TableShape, drop_run_schemas
from themis.models import Backend, Confidence, Evidence, ExecutionDelta, Finding, Severity
from themis.pipeline import attach_execution, build_failure_findings, unexplained_change_findings
from themis.snapshot import ModelNode, ProjectSnapshot


class _Warehouse:
    """Whatever is in the schemas, whoever put it there."""

    def __init__(self, tables: dict[tuple[str, str], TableShape], sums: dict | None = None):
        self._tables = tables
        self._sums = sums or {}

    def shape(self, relation: Relation) -> TableShape:
        return self._tables.get((relation.schema, relation.name), TableShape(exists=False))

    def sums(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]:
        return self._sums.get((relation.schema, relation.name), {})

    def null_rates(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]:
        return {}

    def distinct_count(self, relation: Relation, columns: tuple[str, ...]) -> int | None:
        return None

    def close(self) -> None:
        return None


def _shape(rows: int) -> TableShape:
    return TableShape(exists=True, row_count=rows, column_types={"revenue_usd": "DECIMAL"})


def _measure_one(
    warehouse: _Warehouse, *, head: BuildOutcome, base: BuildOutcome, model: str = "mart"
) -> ExecutionDelta:
    result = _measure(
        warehouse,
        models=(model,),
        base_schema="b",
        head_schema="h",
        max_rows=1_000_000,
        head_build=head,
        base_build=base,
        grain_candidates={},
    )
    return result.deltas[model]


# --- which models a build produced --------------------------------------------------


def test_a_table_left_behind_by_a_failed_build_is_not_measured() -> None:
    """The reproduced defect: the relation exists, so it used to be measured."""
    warehouse = _Warehouse(
        {("b", "mart"): _shape(33), ("h", "mart"): _shape(34)},
        {
            ("b", "mart"): {"revenue_usd": 334_586_894.0},
            ("h", "mart"): {"revenue_usd": 2_009_759_297.0},
        },
    )
    delta = _measure_one(
        warehouse,
        head=BuildOutcome(error="Binder Error: currency_code", statuses={"mart": "error"}),
        base=BuildOutcome(statuses={"mart": "success"}),
    )
    assert delta.build_error is not None
    assert delta.failed_revision == "head"
    assert delta.rows_after is None
    assert delta.sum_deltas == {}


def test_a_model_skipped_behind_a_failure_is_not_built_either() -> None:
    warehouse = _Warehouse({("b", "mart"): _shape(9), ("h", "mart"): _shape(9)})
    delta = _measure_one(
        warehouse,
        head=BuildOutcome(error="boom", statuses={"int": "error", "mart": "skipped"}),
        base=BuildOutcome(statuses={"int": "success", "mart": "success"}),
    )
    assert delta.failed_revision == "head"
    assert delta.build_skipped
    assert delta.rows_after is None


def test_a_base_that_did_not_build_is_named_as_the_base() -> None:
    warehouse = _Warehouse({("b", "mart"): _shape(9), ("h", "mart"): _shape(9)})
    delta = _measure_one(
        warehouse,
        head=BuildOutcome(statuses={"mart": "success"}),
        base=BuildOutcome(error="old error", statuses={"mart": "error"}),
    )
    assert delta.failed_revision == "base"
    assert delta.rows_before is None
    assert delta.rows_after == 9


def test_a_build_that_recorded_nothing_is_judged_by_its_exit_code() -> None:
    """No run_results.json at all — dbt died before any node ran. Nothing was built."""
    assert BuildOutcome(error="dbt crashed").failure("mart") == "dbt crashed"
    assert BuildOutcome().failure("mart") is None


def test_both_revisions_built_is_measured_normally() -> None:
    warehouse = _Warehouse(
        {("b", "mart"): _shape(15), ("h", "mart"): _shape(45)},
    )
    delta = _measure_one(
        warehouse,
        head=BuildOutcome(statuses={"mart": "success"}),
        base=BuildOutcome(statuses={"mart": "success"}),
    )
    assert delta.build_error is None
    assert delta.row_delta == 30


def test_node_statuses_are_read_for_models_and_seeds_only(tmp_path: Path) -> None:
    (tmp_path / "run_results.json").write_text(
        json.dumps(
            {
                "results": [
                    {"unique_id": "seed.p.raw", "status": "success"},
                    {"unique_id": "model.p.mart", "status": "error"},
                    {"unique_id": "test.p.unique_mart_id", "status": "fail"},
                ]
            }
        )
    )
    assert node_statuses(tmp_path) == {"raw": "success", "mart": "error"}
    assert node_statuses(tmp_path / "absent") == {}


def test_a_second_pass_that_fails_before_recording_leaves_its_models_unbuilt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first pass wrote the incremental table; the second never finished it.

    run_results.json is removed before each pass, so the first pass's "success" cannot
    stand in for a second pass that recorded nothing.
    """
    from themis.config import Settings
    from themis.execute import runner

    class _Result:
        def __init__(self, ok: bool) -> None:
            self.ok = ok
            self.stdout = "" if ok else "Runtime Error in model inc (models/inc.sql)\n  boom"

    passes: list[list[str]] = []

    def fake_run_dbt(project_dir: Path, args: list[str], **kwargs: object) -> _Result:
        passes.append(args)
        target = kwargs["target_path"]
        assert isinstance(target, Path)
        target.mkdir(parents=True, exist_ok=True)
        if len(passes) == 1:
            (target / "run_results.json").write_text(
                json.dumps({"results": [{"unique_id": "model.p.inc", "status": "success"}]})
            )
            return _Result(ok=True)
        return _Result(ok=False)

    monkeypatch.setattr(runner, "run_dbt", fake_run_dbt)
    monkeypatch.setattr(runner, "write_profile_for_schema", lambda *a, **k: tmp_path / "profiles")

    outcome = _build(
        tmp_path,
        models=("inc",),
        schema="themis_head_x",
        target="dev",
        settings=Settings(),
        profiles_root=tmp_path,
        anchor_dir=tmp_path,
        label="head",
        incremental_models=("inc",),
    )
    assert len(passes) == 2
    assert outcome.failure("inc") is not None
    assert "boom" in (outcome.error or "")


# --- what the review says about it ---------------------------------------------------


def _snapshot() -> ProjectSnapshot:
    def node(name: str, deps: tuple[str, ...] = ()) -> ModelNode:
        return ModelNode(
            name=name,
            unique_id=f"model.p.{name}",
            file_path=f"models/{name}.sql",
            compiled_sql=f"select 1 as x -- {name}",
            depends_on_models=tuple(f"model.p.{d}" for d in deps),
        )

    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={"int": node("int"), "mart": node("mart", ("int",))},
        child_map={"int": ("mart",)},
    )


def _failed_head_result(*, base_errored: bool = False) -> ExecutionResult:
    head = BuildOutcome(error="Binder Error: x", statuses={"int": "error", "mart": "skipped"})
    base = BuildOutcome(
        error="old" if base_errored else None,
        statuses={"int": "error" if base_errored else "success", "mart": "success"},
    )
    return ExecutionResult(
        deltas={
            "int": ExecutionDelta(model_name="int", build_error="x", failed_revision="head"),
            "mart": ExecutionDelta(
                model_name="mart", build_error="x", failed_revision="head", build_skipped=True
            ),
        },
        head_build=head,
        base_build=base,
    )


def test_a_head_that_does_not_build_is_a_finding_even_when_no_rule_fired() -> None:
    """It used to be "No findings": X0001 ignores build errors and nothing else looked."""
    findings = build_failure_findings(_failed_head_result(), _snapshot())
    assert [f.rule_id for f in findings] == ["X0002"]
    finding = findings[0]
    assert finding.evidence.model_name == "int"
    assert finding.confidence is Confidence.MEASURED
    assert "mart" in finding.consequence


def test_a_model_that_never_built_on_the_base_either_is_not_this_changes_doing() -> None:
    assert build_failure_findings(_failed_head_result(base_errored=True), _snapshot()) == []


def test_a_build_failure_does_not_promote_a_rule_finding_to_measured() -> None:
    """A grain-change prediction is not demonstrated by the model failing to compile."""
    finding = Finding(
        rule_id="F1003",
        family="F1",
        title="Output grain changed",
        severity=Severity.HIGH,
        confidence=Confidence.PROVEN,
        evidence=Evidence(model_name="int"),
        consequence="c",
    )
    attached = attach_execution([finding], _failed_head_result())
    assert attached[0].confidence is Confidence.PROVEN
    assert attached[0].execution_delta is None


def test_a_changed_seed_owns_what_moved_beneath_it() -> None:
    """A data change has no SQL, so it was never an origin and nothing below had an owner."""
    snapshot = _snapshot()
    snapshot.models["raw_fx"] = ModelNode(
        name="raw_fx", unique_id="seed.p.raw_fx", file_path="seeds/raw_fx.csv", resource_type="seed"
    )
    snapshot.child_map["raw_fx"] = ("int",)
    result = ExecutionResult(
        deltas={
            "raw_fx": ExecutionDelta(model_name="raw_fx", rows_before=30, rows_after=31),
            "int": ExecutionDelta(model_name="int", rows_before=10, rows_after=12),
        }
    )
    findings = unexplained_change_findings(
        result, [], snapshot, snapshot, changed_seeds=("raw_fx",)
    )
    assert [f.evidence.model_name for f in findings] == ["raw_fx"]
    assert "seed's data changed" in findings[0].consequence


# --- cleanup -------------------------------------------------------------------------


def test_only_this_runs_schemas_are_dropped(tmp_path: Path) -> None:
    """dbt appends custom schemas to the run's name; nothing else may match."""
    database = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(database))
    for schema in (
        "themis_head_ab12",
        "themis_head_ab12_main",
        "themis_head",
        "themis_head_ab123",
        "main",
    ):
        conn.execute(f'create schema if not exists "{schema}"')
        conn.execute(f'create table if not exists "{schema}".t as select 1 as x')
    conn.close()

    dropped = drop_run_schemas(
        {"type": "duckdb", "path": "warehouse.duckdb"}, tmp_path, ("themis_head_ab12",)
    )
    assert sorted(dropped) == ["themis_head_ab12", "themis_head_ab12_main"]

    conn = duckdb.connect(str(database), read_only=True)
    remaining = {
        row[0]
        for row in conn.execute(
            "select schema_name from information_schema.schemata "
            "where catalog_name = current_database()"
        ).fetchall()
    }
    conn.close()
    assert {"themis_head", "themis_head_ab123", "main"} <= remaining
    assert "themis_head_ab12" not in remaining


def test_attached_catalogs_are_cleaned_too(tmp_path: Path) -> None:
    """A model built into an attached catalog leaves its run schema there, not in the main file."""
    main = tmp_path / "main.duckdb"
    reference = tmp_path / "reference.duckdb"
    duckdb.connect(str(main)).close()
    conn = duckdb.connect(str(reference))
    conn.execute('create schema "themis_base_ff00_main"')
    conn.close()

    dropped = drop_run_schemas(
        {
            "type": "duckdb",
            "path": "main.duckdb",
            "attach": [{"path": "reference.duckdb", "alias": "reference"}],
        },
        tmp_path,
        ("themis_base_ff00",),
    )
    assert dropped == ("themis_base_ff00_main",)


def test_data_tests_never_decide_what_can_be_measured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failing `unique` test made `dbt build` skip every model below it.

    On the tested variant a fan-out failed the declared test, so the models that would
    have shown the fan-out were never built and the case could not be scored at all.
    """
    from themis.config import Settings
    from themis.execute import runner

    class _Result:
        ok = True
        stdout = ""

    calls: list[list[str]] = []

    def fake_run_dbt(project_dir: Path, args: list[str], **kwargs: object) -> _Result:
        calls.append(args)
        return _Result()

    monkeypatch.setattr(runner, "run_dbt", fake_run_dbt)
    monkeypatch.setattr(runner, "write_profile_for_schema", lambda *a, **k: tmp_path / "profiles")
    _build(
        tmp_path,
        models=("mart", "inc"),
        schema="themis_head_x",
        target="dev",
        settings=Settings(),
        profiles_root=tmp_path,
        anchor_dir=tmp_path,
        label="head",
        incremental_models=("inc",),
    )
    assert len(calls) == 2
    for args in calls:
        excluded = {args[i + 1] for i, a in enumerate(args) if a == "--exclude-resource-type"}
        assert {"test", "unit_test"} <= excluded


# --- measured where dbt put it ---------------------------------------------------------------


def test_a_model_with_a_custom_schema_is_measured_where_dbt_built_it() -> None:
    """Most real projects set `+schema:` per folder, and dbt then builds a model into
    `<run schema>_<custom>`. Looked up under the run schema alone it was absent on both
    sides — an empty delta, reported as nothing moved, with revenue doubled underneath."""
    base = BuildOutcome(
        statuses={"mart": "success"}, relations={"mart": Relation(None, "b_finance", "mart")}
    )
    head = BuildOutcome(
        statuses={"mart": "success"}, relations={"mart": Relation(None, "h_finance", "mart")}
    )
    warehouse = _Warehouse({("b_finance", "mart"): _shape(10), ("h_finance", "mart"): _shape(20)})
    delta = _measure_one(warehouse, head=head, base=base)
    assert (delta.rows_before, delta.rows_after) == (10, 20)
    assert delta.is_material


def test_without_a_manifest_a_model_is_looked_for_in_the_run_schema() -> None:
    """The old behaviour, kept only as the fallback when dbt wrote no manifest."""
    outcome = BuildOutcome(statuses={"mart": "success"})
    assert outcome.relation("mart", "themis_head_x") == Relation(None, "themis_head_x", "mart")


def test_where_dbt_built_each_model_is_read_from_the_manifest(tmp_path: Path) -> None:
    from themis.acquire.dbt_runner import built_relations

    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "model.p.fct": {
                        "name": "fct",
                        "database": "hive",
                        "schema": "themis_head_x_finance",
                        "alias": "fct_revenue_v2",
                    },
                    "model.p.ref_data": {
                        "name": "ref_data",
                        "database": "iceberg",
                        "schema": "themis_head_x_main",
                        "alias": None,
                    },
                    "test.p.unique_fct": {"name": "unique_fct", "schema": "x"},
                }
            }
        )
    )
    relations = built_relations(tmp_path)
    assert relations == {
        "fct": ("hive", "themis_head_x_finance", "fct_revenue_v2"),
        "ref_data": ("iceberg", "themis_head_x_main", "ref_data"),
    }
    assert built_relations(tmp_path / "missing") == {}
