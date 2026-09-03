"""The Trino warehouse client, against a real Trino.

Trino is the engine this tool is aimed at, and until this existed Stage 3 — where most
of the value sits — could not run against it at all. A client written without an engine
to test against would be a guess: the DB-API driver, `information_schema` shapes, and
the absence of row-constructor equality in `count(distinct ...)` are all things that
only show up when something actually runs.

Skipped when no Trino is reachable, so local runs stay fast:

    docker run -d -p 8085:8080 trinodb/trino:latest
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from themis.execute.differ import diff_tables, measure_grain
from themis.execute.warehouse import TrinoClient, client_for_profile
from themis.models import Grain, GrainSource

HOST = os.environ.get("THEMIS_TEST_TRINO_HOST", "127.0.0.1")
PORT = int(os.environ.get("THEMIS_TEST_TRINO_PORT", "8085"))


def _trino_available() -> bool:
    try:
        import trino

        conn = trino.dbapi.connect(
            host=HOST, port=PORT, user="themis", catalog="memory", schema="default"
        )
        cur = conn.cursor()
        cur.execute("select 1")
        cur.fetchall()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _trino_available(), reason="no Trino reachable; see this module's docstring"
)


@pytest.fixture
def warehouse() -> Iterator[TrinoClient]:
    import trino

    conn = trino.dbapi.connect(
        host=HOST, port=PORT, user="themis", catalog="memory", schema="default"
    )
    cur = conn.cursor()
    for statement in (
        "drop table if exists memory.themis_t_base.f",
        "drop table if exists memory.themis_t_head.f",
        "create schema if not exists memory.themis_t_base",
        "create schema if not exists memory.themis_t_head",
        """create table memory.themis_t_base.f as
           select 1 as entry_id, cast(100.00 as decimal(38,6)) as amount_usd,
                  cast(null as varchar) as contract_id
           union all select 2, cast(200.00 as decimal(38,6)), 'C2'""",
        """create table memory.themis_t_head.f as
           select e.entry_id, e.amount_usd, e.contract_id from
             (select 1 as entry_id, cast(100.00 as decimal(38,6)) as amount_usd,
                     cast(null as varchar) as contract_id
              union all select 2, cast(200.00 as decimal(38,6)), 'C2') e
           cross join (select 1 union all select 2) m""",
    ):
        cur.execute(statement)
        cur.fetchall()

    client = TrinoClient(host=HOST, port=PORT, user="themis", catalog="memory")
    try:
        yield client
    finally:
        client.close()


def test_shape_reports_rows_and_column_types(warehouse: TrinoClient) -> None:
    shape = warehouse.shape("themis_t_base", "f")
    assert shape.exists
    assert shape.row_count == 2
    assert "decimal" in shape.column_types["amount_usd"].lower()


def test_a_missing_table_degrades_rather_than_raising(warehouse: TrinoClient) -> None:
    """A model may legitimately not exist on one side of a diff. Measurement failure
    must become "unknown", never a wrong number presented as measured."""
    assert not warehouse.shape("themis_t_base", "does_not_exist").exists


def test_monetary_columns_are_recognised(warehouse: TrinoClient) -> None:
    shape = warehouse.shape("themis_t_base", "f")
    assert "amount_usd" in shape.numeric_columns


def test_sums_are_returned_as_numbers(warehouse: TrinoClient) -> None:
    """Trino returns DECIMAL as Decimal; the delta arithmetic needs floats."""
    sums = warehouse.sums("themis_t_base", "f", ("amount_usd",))
    assert sums["amount_usd"] == pytest.approx(300.0)


def test_null_rates_are_measured(warehouse: TrinoClient) -> None:
    rates = warehouse.null_rates("themis_t_base", "f", ("contract_id",))
    assert rates["contract_id"] == pytest.approx(0.5)


def test_distinct_count_on_a_single_column(warehouse: TrinoClient) -> None:
    assert warehouse.distinct_count("themis_t_base", "f", ("entry_id",)) == 2


def test_distinct_count_on_a_composite_key(warehouse: TrinoClient) -> None:
    """Trino has no row-constructor equality inside count(distinct ...), so a composite
    key is concatenated with a separator that cannot occur in a value."""
    assert warehouse.distinct_count("themis_t_base", "f", ("entry_id", "amount_usd")) == 2


def test_a_fan_out_is_measured_end_to_end(warehouse: TrinoClient) -> None:
    """The case the whole tool exists for, on the engine it targets."""
    delta = diff_tables(
        warehouse,
        "f",
        base_schema="themis_t_base",
        head_schema="themis_t_head",
        max_rows=1_000_000,
    )
    assert delta.rows_before == 2
    assert delta.rows_after == 4
    before, after = delta.sum_deltas["amount_usd"]
    assert after == pytest.approx(before * 2)
    assert delta.is_material


def test_grain_is_measured_as_rows_per_key(warehouse: TrinoClient) -> None:
    grain = measure_grain(
        warehouse,
        "f",
        schema="themis_t_head",
        candidate=Grain(model_name="f", columns=("entry_id",), source=GrainSource.HEURISTIC),
    )
    assert grain is not None
    assert grain.source is GrainSource.MEASURED
    assert grain.rows_per_key == pytest.approx(2.0)
    assert "does NOT identify a row" in (grain.note or "")


def test_a_trino_profile_builds_a_client() -> None:
    from pathlib import Path

    client = client_for_profile(
        {"type": "trino", "host": HOST, "port": PORT, "user": "themis", "database": "memory"},
        Path("."),
    )
    assert isinstance(client, TrinoClient)
    client.close()


def test_an_incomplete_trino_profile_is_refused() -> None:
    """Half a connection is worse than none: it would fail later, mid-review."""
    from pathlib import Path

    assert client_for_profile({"type": "trino", "host": HOST}, Path(".")) is None
