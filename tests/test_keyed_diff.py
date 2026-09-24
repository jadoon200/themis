"""Pairing rows on the derived key, so values that move between keys are measured.

The execution oracle asked whether rows, totals or the column set moved. That is the
right question for a fan-out and the wrong one for a reclassification: flipping how
uncontracted revenue is recognised leaves every row and every total exactly where it
was, and the review came back clean. A keyed comparison sees it, because it asks what
happened to each row rather than to the sum of them.

The guards matter as much as the comparison. Pairing on a key that does not identify a
row would report every duplicate as a change, so it runs only on a key Stage 3 counted
unique in *both* builds — the derivation proposes, the count decides. And arithmetic
reordered by a refactor must not read as every row having changed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
import sqlglot

from themis.execute.differ import pair_rows
from themis.execute.warehouse import DuckDBClient, Relation, paired_rows_sql
from themis.models import (
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Grain,
    GrainSource,
    KeyedDiff,
    Severity,
)


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[DuckDBClient]:
    db = tmp_path / "paired.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema base; create schema head;")
    for schema in ("base", "head"):
        con.execute(
            f"create table {schema}.fct (entry_id varchar, period integer, "
            "recognition varchar, amount double, _loaded_at timestamp)"
        )
    con.execute(
        """insert into base.fct values
        ('E1', 1, 'point_in_time', 100.0, timestamp '2026-01-01 00:00:00'),
        ('E2', 1, 'point_in_time', 0.1 + 0.2, timestamp '2026-01-01 00:00:00'),
        ('E3', 1, 'over_time', 50.0, timestamp '2026-01-01 00:00:00'),
        ('E4', 1, 'point_in_time', 10.0, timestamp '2026-01-01 00:00:00')"""
    )
    con.execute(
        """insert into head.fct values
        ('E1', 1, 'over_time', 100.0, timestamp '2026-01-02 00:00:00'),
        ('E2', 1, 'point_in_time', 0.3, timestamp '2026-01-02 00:00:00'),
        ('E3', 1, 'over_time', NULL, timestamp '2026-01-02 00:00:00'),
        ('E5', 1, 'point_in_time', 10.0, timestamp '2026-01-02 00:00:00')"""
    )
    con.close()
    client = DuckDBClient(db)
    try:
        yield client
    finally:
        client.close()


def _unique(columns: tuple[str, ...] = ("entry_id", "period")) -> Grain:
    return Grain(model_name="fct", columns=columns, source=GrainSource.MEASURED, rows_per_key=1.0)


def _pair(client: DuckDBClient, **overrides: object) -> tuple[KeyedDiff | None, str | None]:
    arguments: dict[str, object] = dict(
        base=Relation(None, "base", "fct"),
        head=Relation(None, "head", "fct"),
        head_grain=_unique(),
        base_grain=_unique(),
        max_rows=1_000_000,
        ignore=("_loaded_at",),
    )
    arguments.update(overrides)
    return pair_rows(client, "fct", **arguments)  # type: ignore[arg-type]


# --- what the comparison sees ---------------------------------------------------


def test_a_value_that_changed_is_counted_against_its_column(warehouse: DuckDBClient) -> None:
    keyed, reason = _pair(warehouse)
    assert reason is None and keyed is not None
    assert keyed.columns_changed["recognition"] == 1


def test_float_noise_is_not_a_change(warehouse: DuckDBClient) -> None:
    """0.1 + 0.2 is not 0.3 in binary floating point. A refactor that reorders
    arithmetic must not read as a row having changed."""
    keyed, _ = _pair(warehouse)
    assert keyed is not None
    # E1 (recognition) and E3 (amount became NULL); E2's float noise is not counted.
    assert keyed.rows_changed == 2


def test_a_value_becoming_null_is_a_change(warehouse: DuckDBClient) -> None:
    keyed, _ = _pair(warehouse)
    assert keyed is not None
    assert keyed.columns_changed["amount"] == 1


def test_keys_on_one_side_only_are_added_or_removed(warehouse: DuckDBClient) -> None:
    keyed, _ = _pair(warehouse)
    assert keyed is not None
    assert (keyed.rows_added, keyed.rows_removed) == (1, 1)


def test_a_key_with_nulls_is_refused(tmp_path: Path) -> None:
    """Pairing joins on equality, which cannot match a NULL key, and a null-safe join is
    one Trino plans as a filter — 300 times slower on 200,000 rows and quadratic beyond.
    A key with NULLs does not identify its rows, so the comparison is refused."""
    db = tmp_path / "nulls.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema base; create schema head;")
    for schema in ("base", "head"):
        con.execute(f"create table {schema}.fct (entry_id varchar, period integer, v varchar)")
        con.execute(f"insert into {schema}.fct values ('E1', 1, 'a'), (NULL, 1, 'b')")
    con.close()
    client = DuckDBClient(db)
    try:
        keyed, reason = _pair(client)
    finally:
        client.close()
    assert keyed is None
    assert reason is not None and "has NULL values in the base build" in reason


def test_load_metadata_is_not_compared_and_says_so(warehouse: DuckDBClient) -> None:
    """Every row's load timestamp differs between two builds of the same code."""
    keyed, _ = _pair(warehouse)
    assert keyed is not None
    assert "_loaded_at" not in keyed.columns_changed
    assert keyed.ignored_columns == ("_loaded_at",)


def test_example_keys_are_kept_for_the_reviewer(warehouse: DuckDBClient) -> None:
    keyed, _ = _pair(warehouse)
    assert keyed is not None
    assert "E1 | 1" in keyed.sample_keys


# --- when pairing must refuse ---------------------------------------------------


def test_a_key_that_does_not_identify_a_row_is_refused(warehouse: DuckDBClient) -> None:
    """Pairing on duplicates would report every duplicate as a change."""
    fanned = Grain(
        model_name="fct", columns=("period",), source=GrainSource.MEASURED, rows_per_key=5.0
    )
    keyed, reason = _pair(warehouse, head_grain=fanned, base_grain=_unique(("period",)))
    assert keyed is None
    assert reason is not None and "does not identify a row in the head build" in reason


def test_a_key_counted_on_only_one_side_is_refused(warehouse: DuckDBClient) -> None:
    keyed, reason = _pair(warehouse, base_grain=None)
    assert keyed is None and reason == "no key could be counted in both builds"


def test_a_table_over_the_budget_is_refused_with_the_reason(warehouse: DuckDBClient) -> None:
    keyed, reason = _pair(warehouse, max_rows=2)
    assert keyed is None and reason is not None and "time budget" in reason


# --- what the rest of the system does with it -----------------------------------


def test_values_moving_with_every_total_intact_is_material() -> None:
    """The case the oracle could not see: same rows, same totals, different values."""
    held = ExecutionDelta(
        model_name="fct", rows_before=5, rows_after=5, sum_deltas={"amount": (161.0, 161.0)}
    )
    assert not held.is_material
    moved = held.model_copy(
        update={"keyed": KeyedDiff(key=("entry_id",), rows_changed=3, columns_changed={"r": 3})}
    )
    assert moved.is_material


def test_a_pairing_that_found_nothing_is_not_material() -> None:
    delta = ExecutionDelta(
        model_name="fct", rows_before=5, rows_after=5, keyed=KeyedDiff(key=("entry_id",))
    )
    assert not delta.is_material


def test_the_query_is_valid_trino() -> None:
    """One statement for both engines, so it has to parse as the target dialect."""
    counts, sample = paired_rows_sql(
        '"memory"."base"."fct"',
        '"memory"."head"."fct"',
        key=("entry_id", "period"),
        columns=("recognition", "amount"),
        numeric=frozenset({"amount"}),
        quote=lambda name: '"' + name + '"',
    )
    for statement in (counts, sample):
        assert sqlglot.parse_one(statement, read="trino") is not None


def _finding_with(keyed: KeyedDiff) -> Finding:
    return Finding(
        rule_id="X0001",
        family="X",
        title="`fct` changed and no rule explains why",
        severity=Severity.HIGH,
        confidence=Confidence.MEASURED,
        evidence=Evidence(model_name="fct", note="paired", identity=""),
        consequence="values moved",
        execution_delta=ExecutionDelta(model_name="fct", rows_before=5, rows_after=5, keyed=keyed),
    )


def test_the_report_names_the_columns_that_moved() -> None:
    from themis.report import markdown

    text = markdown.render(
        [
            _finding_with(
                KeyedDiff(
                    key=("entry_id",),
                    rows_changed=3,
                    columns_changed={"recognition_method": 3},
                    sample_keys=("E1",),
                )
            )
        ],
        skipped=[],
        models_reviewed=1,
        executed=True,
    )
    assert "Rows paired on (`entry_id`)" in text
    assert "`recognition_method` changed in 3 row(s)" in text
    assert "`E1`" in text


def test_a_redacted_report_never_carries_key_values() -> None:
    """A key value can identify a customer. The count of rows that moved cannot."""
    from themis.report import json_out

    finding = _finding_with(
        KeyedDiff(
            key=("customer_id",),
            rows_changed=2,
            columns_changed={"segment": 2},
            sample_keys=("CUST-00042",),
        )
    )
    redacted = json_out.render(
        [finding],
        skipped=[],
        grains={},
        deltas={"fct": finding.execution_delta},  # type: ignore[dict-item]
        models_reviewed=("fct",),
        executed=True,
        redact="salt",
    )
    assert "CUST-00042" not in redacted
    assert "customer_id" not in redacted
    payload = json.loads(redacted)
    assert payload["execution_deltas"][0]["paired_rows_moved"] == 2


def test_volatile_columns_are_not_compared_and_are_named(warehouse: DuckDBClient) -> None:
    """A column the SQL stamps with the build time differs in every row of any two builds.
    Excluded from the comparison — and named, so it never reads as having held."""
    keyed, _ = _pair(warehouse, ignore=(), volatile=frozenset({"_loaded_at"}))
    assert keyed is not None
    assert "_loaded_at" not in keyed.columns_changed
    assert keyed.volatile_columns == ("_loaded_at",)
    assert keyed.ignored_columns == ()


def test_the_report_says_which_columns_the_sql_made_volatile() -> None:
    from themis.report import markdown

    finding = _finding_with(
        KeyedDiff(
            key=("entry_id",),
            rows_changed=1,
            columns_changed={"recognition_method": 1},
            volatile_columns=("processed_at",),
        )
    )
    text = markdown.render([finding], skipped=[], models_reviewed=1, executed=True)
    assert "because the SQL makes them differ in any two builds" in text
    assert "`processed_at`" in text


# --- a period that was already reported ----------------------------------------------------


def _delta_with_keyed(**keyed: object) -> ExecutionDelta:
    from themis.models import KeyedDiff

    return ExecutionDelta(
        model_name="fct_regulatory_summary",
        rows_before=100,
        rows_after=100,
        keyed=KeyedDiff(key=("period_month", "entity_code"), **keyed),  # type: ignore[arg-type]
    )


def _result(delta: ExecutionDelta):
    from themis.execute.runner import ExecutionResult

    return ExecutionResult(deltas={delta.model_name: delta})


def _snapshot_with(tags: tuple[str, ...]):
    from themis.models import Backend
    from themis.snapshot import ModelNode, ProjectSnapshot

    node = ModelNode(
        name="fct_regulatory_summary",
        unique_id="model.t.fct_regulatory_summary",
        file_path="models/marts/fct_regulatory_summary.sql",
        tags=tags,
    )
    return ProjectSnapshot(
        revision="r", backend=Backend.MANIFEST, models={node.name: node}, child_map={}
    )


def test_a_figure_moving_in_a_closed_period_is_reported_on_its_own() -> None:
    """A restatement is a different conversation from the defect that caused it.

    "The join lost a predicate" is something to fix before merging. "Last quarter's
    reported revenue is now a different number" is an event with a process attached. A
    reviewer can accept the first and still have to be told the second.
    """
    from themis.pipeline import restated_period_findings
    from themis.vocabulary import DEFAULT

    delta = _delta_with_keyed(
        period_column="period_month",
        latest_period="2026-09",
        prior_period_rows=12,
        earliest_changed_period="2026-06",
        rows_changed=12,
    )
    (finding,) = restated_period_findings(_result(delta), _snapshot_with(("regulatory",)), DEFAULT)
    assert finding.rule_id == "X0004"
    assert finding.confidence is Confidence.MEASURED
    # Critical only because it is measured *and* the model is governed.
    assert finding.severity is Severity.CRITICAL
    assert "12 row(s) changed in periods before 2026-09" in (finding.evidence.note or "")
    assert "2026-06" in (finding.evidence.note or "")


def test_an_ungoverned_model_restating_a_period_is_high_not_critical() -> None:
    from themis.pipeline import restated_period_findings
    from themis.vocabulary import DEFAULT

    delta = _delta_with_keyed(
        period_column="period_month", latest_period="2026-09", prior_period_rows=3
    )
    (finding,) = restated_period_findings(_result(delta), _snapshot_with(()), DEFAULT)
    assert finding.severity is Severity.HIGH


def test_a_change_confined_to_the_open_period_says_nothing() -> None:
    """The common case, and it must be silent or the finding means nothing."""
    from themis.pipeline import restated_period_findings
    from themis.vocabulary import DEFAULT

    delta = _delta_with_keyed(
        period_column="period_month",
        latest_period="2026-09",
        prior_period_rows=0,
        rows_changed=40,
    )
    assert restated_period_findings(_result(delta), _snapshot_with(("regulatory",)), DEFAULT) == []


def test_a_grain_with_no_period_in_it_is_never_guessed_at() -> None:
    from themis.pipeline import restated_period_findings
    from themis.vocabulary import DEFAULT

    delta = _delta_with_keyed(prior_period_rows=5)  # no period_column: nothing was measured
    assert restated_period_findings(_result(delta), _snapshot_with(("regulatory",)), DEFAULT) == []


@pytest.fixture
def periods(tmp_path: Path) -> Iterator[DuckDBClient]:
    """Three periods, with one row changed in the earliest and one in the latest."""
    db = tmp_path / "periods.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema base; create schema head;")
    for schema in ("base", "head"):
        con.execute(
            f"create table {schema}.fct (period_month varchar, entity_code varchar, revenue double)"
        )
    con.execute(
        """insert into base.fct values
        ('2026-06', 'A', 100.0), ('2026-06', 'B', 200.0),
        ('2026-07', 'A', 110.0), ('2026-07', 'B', 210.0),
        ('2026-08', 'A', 120.0), ('2026-08', 'B', 220.0)"""
    )
    con.execute(
        """insert into head.fct values
        ('2026-06', 'A', 100.0), ('2026-06', 'B', 999.0),
        ('2026-07', 'A', 110.0), ('2026-07', 'B', 210.0),
        ('2026-08', 'A', 120.0), ('2026-08', 'B', 888.0)"""
    )
    con.close()
    client = DuckDBClient(db)
    try:
        yield client
    finally:
        client.close()


def _period_grain() -> Grain:
    return Grain(
        model_name="fct",
        columns=("period_month", "entity_code"),
        source=GrainSource.MEASURED,
        rows_per_key=1.0,
    )


def test_rows_changed_in_an_earlier_period_are_counted_and_the_earliest_named(
    periods: DuckDBClient,
) -> None:
    keyed, reason = pair_rows(
        periods,
        "fct",
        base=Relation(None, "base", "fct"),
        head=Relation(None, "head", "fct"),
        head_grain=_period_grain(),
        base_grain=_period_grain(),
        max_rows=1000,
    )
    assert reason is None and keyed is not None
    assert keyed.period_column == "period_month"
    assert keyed.latest_period == "2026-08"
    # 2026-06 B moved; 2026-08 B moved too but that is the period still open.
    assert keyed.prior_period_rows == 1
    assert keyed.earliest_changed_period == "2026-06"
    assert keyed.restates_a_closed_period


def test_a_change_only_in_the_latest_period_is_not_a_restatement(tmp_path: Path) -> None:
    db = tmp_path / "open.duckdb"
    con = duckdb.connect(str(db))
    con.execute("create schema base; create schema head;")
    for schema in ("base", "head"):
        con.execute(
            f"create table {schema}.fct (period_month varchar, entity_code varchar, revenue double)"
        )
    con.execute("insert into base.fct values ('2026-07', 'A', 1.0), ('2026-08', 'A', 2.0)")
    con.execute("insert into head.fct values ('2026-07', 'A', 1.0), ('2026-08', 'A', 9.0)")
    con.close()
    client = DuckDBClient(db)
    try:
        keyed, reason = pair_rows(
            client,
            "fct",
            base=Relation(None, "base", "fct"),
            head=Relation(None, "head", "fct"),
            head_grain=_period_grain(),
            base_grain=_period_grain(),
            max_rows=1000,
        )
    finally:
        client.close()
    assert reason is None and keyed is not None
    assert keyed.rows_changed == 1
    assert keyed.prior_period_rows == 0
    assert not keyed.restates_a_closed_period


def test_one_period_in_the_table_can_never_be_a_restatement(warehouse: DuckDBClient) -> None:
    """Its key does carry a period, and every row is in it — so nothing is *earlier*."""
    keyed, _ = _pair(warehouse)
    assert keyed is not None
    assert keyed.period_column == "period"
    assert keyed.rows_changed  # rows did move
    assert keyed.prior_period_rows == 0 and not keyed.restates_a_closed_period


def test_a_grain_with_no_period_column_at_all_measures_nothing_about_periods(
    warehouse: DuckDBClient,
) -> None:
    keyed, _ = _pair(
        warehouse, head_grain=_unique(("entry_id",)), base_grain=_unique(("entry_id",))
    )
    assert keyed is not None
    assert keyed.period_column is None
    assert keyed.latest_period is None and keyed.prior_period_rows == 0


def test_a_restatement_is_not_repeated_down_the_chain() -> None:
    """One change moves every model beneath it; the restatement is reported where it lands.

    Repeating it per affected model is the noise that made X0001 attribute to roots — one
    fact becoming five findings, four of which name a model nobody reports.
    """
    from themis.execute.runner import ExecutionResult
    from themis.models import Backend, KeyedDiff
    from themis.pipeline import restated_period_findings
    from themis.snapshot import ModelNode, ProjectSnapshot
    from themis.vocabulary import DEFAULT

    def node(name: str, tags: tuple[str, ...] = ()) -> ModelNode:
        return ModelNode(
            name=name, unique_id=f"model.t.{name}", file_path=f"models/{name}.sql", tags=tags
        )

    snapshot = ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "stg_entries": node("stg_entries"),
            "int_converted": node("int_converted"),
            "fct_reported": node("fct_reported", ("regulatory",)),
            "fct_internal": node("fct_internal"),
        },
        child_map={
            "stg_entries": ("int_converted",),
            "int_converted": ("fct_reported", "fct_internal"),
        },
    )
    keyed = KeyedDiff(
        key=("period_month",),
        period_column="period_month",
        latest_period="2026-09",
        prior_period_rows=4,
        earliest_changed_period="2026-01",
    )
    deltas = {
        name: ExecutionDelta(model_name=name, keyed=keyed)
        for name in ("stg_entries", "int_converted", "fct_reported", "fct_internal")
    }
    findings = restated_period_findings(ExecutionResult(deltas=deltas), snapshot, DEFAULT)
    named = sorted(f.evidence.model_name for f in findings)
    # The governed mart, and the leaf nobody builds on. Not the staging or intermediate
    # models, whose figures are not reported anywhere.
    assert named == ["fct_internal", "fct_reported"]
