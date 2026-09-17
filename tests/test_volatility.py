"""Values that differ between two builds of identical code.

Found on a comment-only change: audit columns stamped with current_timestamp,
'{{ run_started_at }}' and '{{ invocation_id }}' made an untouched model read as changed,
with every row different in all three. Real dbt projects stamp audit columns on nearly
every model, so this would have fired on almost every review at work.

The refusals matter as much as the detection: a date alone is not masked, current_date is
not volatile, and a timestamp outside the compile's own window is left exactly as written.
"""

from __future__ import annotations

from datetime import UTC, datetime

from themis.analyze.volatility import (
    INVOCATION_ID,
    RUN_STARTED_AT,
    invocation_window,
    mask_invocation_literals,
    volatile_columns,
)
from themis.models import Backend
from themis.snapshot import ModelNode, ProjectSnapshot

_METADATA = {
    "invocation_id": "962e2d12-c079-4675-96fb-ac113584bdd7",
    "invocation_started_at": "2026-09-17T15:25:55.055285Z",
    "generated_at": "2026-09-17T15:25:55.346793Z",
}


def _mask(sql: str) -> str:
    return mask_invocation_literals(
        sql,
        invocation_id=str(_METADATA["invocation_id"]),
        window=invocation_window(_METADATA),
    )


# --- compile-time: literals rendered from the invocation ------------------------------


def test_the_rendered_run_timestamp_is_masked() -> None:
    """dbt renders run_started_at a few hundred microseconds after it records the start."""
    sql = "select '2026-09-17 15:25:55.055542+00:00' as loaded_at from t"
    assert _mask(sql) == f"select '{RUN_STARTED_AT}' as loaded_at from t"


def test_the_invocation_id_is_masked() -> None:
    sql = "select '962e2d12-c079-4675-96fb-ac113584bdd7' as batch_id from t"
    assert _mask(sql) == f"select '{INVOCATION_ID}' as batch_id from t"


def test_two_compiles_of_the_same_code_compare_equal_once_masked() -> None:
    """The actual failure: identical code, different compiled SQL, read as a change."""
    other = {
        "invocation_id": "a6b8ae52-69a0-4506-bdb6-5c4a65f53fde",
        "invocation_started_at": "2026-09-17T15:25:58.191245Z",
        "generated_at": "2026-09-17T15:25:58.385835Z",
    }
    first = _mask(
        "select '2026-09-17 15:25:55.055542+00:00' as a, "
        "'962e2d12-c079-4675-96fb-ac113584bdd7' as b"
    )
    second = mask_invocation_literals(
        "select '2026-09-17 15:25:58.191384+00:00' as a, "
        "'a6b8ae52-69a0-4506-bdb6-5c4a65f53fde' as b",
        invocation_id=other["invocation_id"],
        window=invocation_window(other),
    )
    assert first == second


def test_a_business_timestamp_outside_the_compile_is_left_alone() -> None:
    sql = "select * from t where posted_at >= '2026-01-01 00:00:00'"
    assert _mask(sql) == sql


def test_a_date_alone_is_never_masked() -> None:
    """Two compiles minutes apart agree on the date, and masking one could hide a real
    hard-coded date in a filter."""
    sql = "select * from t where d = '2026-09-17'"
    assert _mask(sql) == sql


def test_without_metadata_nothing_is_masked() -> None:
    sql = "select '2026-09-17 15:25:55.055542+00:00' as a"
    assert mask_invocation_literals(sql, invocation_id=None, window=None) == sql


def test_the_window_falls_back_to_generated_at() -> None:
    window = invocation_window({"generated_at": "2026-09-17T15:25:55Z"})
    assert window is not None
    assert window[0] < datetime(2026, 9, 17, 15, 0, tzinfo=UTC) < window[1]


# --- build-time: columns computed from values that change on every build -------------


def _snapshot(**models: tuple[str, tuple[str, ...]]) -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            name: ModelNode(
                name=name,
                unique_id=f"model.d.{name}",
                file_path=f"models/{name}.sql",
                compiled_sql=sql,
                depends_on_models=tuple(f"model.d.{d}" for d in deps),
            )
            for name, (sql, deps) in models.items()
        },
    )


def test_a_column_stamped_with_the_build_time_is_volatile() -> None:
    found = volatile_columns(
        _snapshot(m=("select id, amount, current_timestamp as processed_at from raw", ()))
    )
    assert found == {"m": frozenset({"processed_at"})}


def test_the_function_is_found_whatever_the_column_is_called() -> None:
    """Not a name list: a load time called `x` is still a load time."""
    found = volatile_columns(
        _snapshot(m=("select id, now() as x, uuid() as y, random() as z from raw", ()))
    )
    assert found == {"m": frozenset({"x", "y", "z"})}


def test_a_masked_invocation_literal_is_volatile() -> None:
    found = volatile_columns(
        _snapshot(
            m=(f"select id, '{RUN_STARTED_AT}' as loaded, '{INVOCATION_ID}' as b from raw", ())
        )
    )
    assert found == {"m": frozenset({"loaded", "b"})}


def test_current_date_is_not_volatile() -> None:
    """Two builds minutes apart agree on the date. Excluding every column computed from it
    would stop measuring columns that compare perfectly well."""
    found = volatile_columns(_snapshot(m=("select id, current_date as report_date from raw", ())))
    assert found == {}


def test_volatility_is_followed_through_ctes_and_renames() -> None:
    sql = (
        "with a as (select id, current_timestamp as ts from raw), "
        "b as (select id, ts as loaded_on from a) "
        "select id, loaded_on, cast(loaded_on as varchar) as loaded_text from b"
    )
    found = volatile_columns(_snapshot(m=(sql, ())))
    assert found == {"m": frozenset({"loaded_on", "loaded_text"})}


def test_volatility_propagates_to_downstream_models() -> None:
    found = volatile_columns(
        _snapshot(
            stg=("select id, amount, current_timestamp as loaded_at from raw", ()),
            mart=(
                "select id, amount, loaded_at, date_trunc('day', loaded_at) as d from stg",
                ("stg",),
            ),
            star=("select * from mart", ("mart",)),
        )
    )
    assert found["stg"] == {"loaded_at"}
    assert found["mart"] == {"loaded_at", "d"}
    assert found["star"] == {"loaded_at", "d"}


def test_a_stable_column_beside_a_volatile_one_stays_comparable() -> None:
    found = volatile_columns(
        _snapshot(m=("select id, amount * 2 as doubled, now() as at from raw", ()))
    )
    assert "doubled" not in found["m"] and "amount" not in found["m"]
