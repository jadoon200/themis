"""Turn two materialised tables into an ``ExecutionDelta``.

Cheap aggregates only. The point is evidence a reviewer can act on — a row count that
moved, a total that moved — not a full row-by-row diff, which would cost far more and
say less.
"""

from __future__ import annotations

from themis.execute.warehouse import WarehouseClient
from themis.logging import get_logger
from themis.models import ExecutionDelta, Grain, GrainSource

log = get_logger(__name__)

# Column-name hints for money, matching the F3 rule family so a measured finding and a
# static one talk about the same set of columns.
_MONEY_HINTS = (
    "amount",
    "amt",
    "price",
    "cost",
    "revenue",
    "balance",
    "value",
    "total",
    "fee",
    "tax",
    "charge",
    "payment",
    "salary",
)


def _monetary(columns: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(c for c in columns if any(h in c.lower() for h in _MONEY_HINTS))


def diff_tables(
    client: WarehouseClient,
    model: str,
    *,
    base_schema: str,
    head_schema: str,
    max_rows: int,
) -> ExecutionDelta:
    """Compare one model built two ways."""
    before = client.shape(base_schema, model)
    after = client.shape(head_schema, model)

    if not before.exists and not after.exists:
        return ExecutionDelta(model_name=model)

    delta = ExecutionDelta(
        model_name=model,
        rows_before=before.row_count if before.exists else None,
        rows_after=after.row_count if after.exists else None,
        columns_added=tuple(sorted(set(after.column_types) - set(before.column_types))),
        columns_removed=tuple(sorted(set(before.column_types) - set(after.column_types))),
        columns_retyped={
            name: (before.column_types[name], after.column_types[name])
            for name in sorted(set(before.column_types) & set(after.column_types))
            if before.column_types[name] != after.column_types[name]
        },
    )

    if not (before.exists and after.exists):
        return delta

    # Guard the time budget rather than the correctness: on a very large table the
    # aggregates are skipped and the row count still stands on its own.
    if max(before.row_count, after.row_count) > max_rows:
        log.info(
            "differ.skipped_aggregates", model=model, rows=max(before.row_count, after.row_count)
        )
        return delta

    shared = tuple(sorted(set(before.column_types) & set(after.column_types)))
    money = _monetary(tuple(c for c in shared if c in set(after.numeric_columns)))

    sums_before = client.sums(base_schema, model, money)
    sums_after = client.sums(head_schema, model, money)
    nulls_before = client.null_rates(base_schema, model, shared)
    nulls_after = client.null_rates(head_schema, model, shared)

    return delta.model_copy(
        update={
            "sum_deltas": {
                column: (sums_before[column], sums_after[column])
                for column in money
                if column in sums_before and column in sums_after
            },
            "null_rate_deltas": {
                column: (nulls_before[column], nulls_after[column])
                for column in shared
                if column in nulls_before
                and column in nulls_after
                and abs(nulls_before[column] - nulls_after[column]) > 1e-9
            },
        }
    )


def measure_grain(
    client: WarehouseClient,
    model: str,
    *,
    schema: str,
    candidate: Grain | None,
) -> Grain | None:
    """Settle a model's grain by counting instead of inferring.

    This is what static derivation cannot do. ``count(*)`` against
    ``count(distinct key)`` either confirms the key is genuinely unique or gives the
    exact rows-per-key multiplier — the difference between "this join may fan out" and
    "this join produces 3.0 rows per key".
    """
    if candidate is None or not candidate.columns:
        return None

    shape = client.shape(schema, model)
    if not shape.exists or shape.row_count == 0:
        return None
    if not set(candidate.columns) <= set(shape.column_types):
        return None  # the derived key does not exist in the built table

    distinct = client.distinct_count(schema, model, candidate.columns)
    if distinct is None or distinct == 0:
        return None

    rows_per_key = shape.row_count / distinct
    unique = distinct == shape.row_count
    return Grain(
        model_name=model,
        columns=candidate.columns,
        source=GrainSource.MEASURED,
        rows_per_key=rows_per_key,
        note=(
            f"measured: {shape.row_count:,} rows, {distinct:,} distinct "
            f"({rows_per_key:.2f} rows per key)"
            + ("" if unique else " — the key does NOT identify a row")
        ),
    )
