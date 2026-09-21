"""Turn two materialised tables into an ``ExecutionDelta``.

Cheap aggregates only. The point is evidence a reviewer can act on — a row count that
moved, a total that moved — not a full row-by-row diff, which would cost far more and
say less.
"""

from __future__ import annotations

from themis.execute.warehouse import WarehouseClient
from themis.logging import get_logger
from themis.models import ExecutionDelta, Grain, GrainSource, KeyedDiff
from themis.vocabulary import DEFAULT as DEFAULT_VOCABULARY
from themis.vocabulary import Vocabulary

log = get_logger(__name__)


def diff_tables(
    client: WarehouseClient,
    model: str,
    *,
    base_schema: str,
    head_schema: str,
    max_rows: int,
    vocabulary: Vocabulary = DEFAULT_VOCABULARY,
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
    numeric = set(after.numeric_columns)
    money = tuple(c for c in shared if c in numeric and vocabulary.is_monetary(c))

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


def pair_rows(
    client: WarehouseClient,
    model: str,
    *,
    base_schema: str,
    head_schema: str,
    head_grain: Grain | None,
    base_grain: Grain | None,
    max_rows: int,
    ignore: tuple[str, ...] = (),
    volatile: frozenset[str] = frozenset(),
    vocabulary: Vocabulary = DEFAULT_VOCABULARY,
) -> tuple[KeyedDiff | None, str | None]:
    """Compare base and head row by row on a key both builds have been counted unique on.

    Returns the comparison, or None and the reason it could not be made. The reason is
    kept because "not paired" and "paired and nothing changed" must never read the same:
    the first is a limit of the evidence, the second is evidence.
    """
    if head_grain is None or base_grain is None:
        return None, "no key could be counted in both builds"
    if head_grain.columns != base_grain.columns:
        return None, "the two builds were counted on different keys"
    for side, grain in (("head", head_grain), ("base", base_grain)):
        if grain.rows_per_key is None or abs(grain.rows_per_key - 1.0) > 1e-12:
            return None, (
                f"({', '.join(grain.columns)}) does not identify a row in the {side} build, "
                "so rows cannot be paired"
            )

    before = client.shape(base_schema, model)
    after = client.shape(head_schema, model)
    if not (before.exists and after.exists):
        return None, "the model is missing from one build"
    if max(before.row_count, after.row_count) > max_rows:
        return None, f"more than {max_rows:,} rows, over the time budget for pairing"

    key = head_grain.columns
    # Pairing joins on plain equality, which cannot match a NULL key — and a null-safe join
    # is one Trino cannot hash (see paired_rows_sql). A key with NULLs does not identify its
    # rows anyway, so the comparison is refused and says why.
    for schema, side in ((base_schema, "base"), (head_schema, "head")):
        rates = client.null_rates(schema, model, key)
        if set(rates) != set(key):
            return None, f"could not confirm ({', '.join(key)}) has no NULLs in the {side} build"
        if any(rate > 0 for rate in rates.values()):
            return None, (
                f"({', '.join(key)}) has NULL values in the {side} build, "
                "so rows cannot be paired on it"
            )

    ignored = {name.lower() for name in ignore}
    comparable = tuple(
        sorted(
            name
            for name in set(before.column_types) & set(after.column_types)
            if name not in key
            # A retyped column is already reported as a schema change, and comparing
            # a varchar to a decimal would fail the whole query.
            and before.column_types[name] == after.column_types[name]
            and name.lower() not in ignored
            and name not in volatile
        )
    )
    skipped = tuple(
        sorted(
            name
            for name in set(before.column_types) & set(after.column_types)
            # Named by the settings but not proven volatile by the SQL. A column both lists
            # would name is reported as volatile: the SQL is the stronger reason.
            if name.lower() in ignored and name not in key and name not in volatile
        )
    )
    unstable = tuple(
        sorted(
            name
            for name in set(before.column_types) & set(after.column_types)
            if name in volatile and name not in key
        )
    )
    numeric = frozenset(after.numeric_columns)

    # A period in the key makes one more question answerable in the same pass, and it is
    # the one a bank asks first: did anything move in a period that has already been
    # reported? Only from the key, because that is the column the rows are identified by —
    # a period column that is not part of the grain says nothing about which row is which.
    period = next((column for column in key if vocabulary.is_period_column(column)), None)

    paired = client.paired_rows(
        (base_schema, model),
        (head_schema, model),
        key=key,
        columns=comparable,
        numeric=numeric,
        period=period,
    )
    if paired is None:
        return None, "the paired comparison could not be run"

    return (
        KeyedDiff(
            key=key,
            rows_added=paired.rows_added,
            rows_removed=paired.rows_removed,
            rows_changed=paired.rows_changed,
            columns_changed=paired.columns_changed,
            ignored_columns=skipped,
            volatile_columns=unstable,
            sample_keys=paired.sample_keys,
            period_column=period,
            latest_period=paired.latest_period,
            prior_period_rows=paired.prior_period_rows,
            earliest_changed_period=paired.earliest_changed_period,
        ),
        None,
    )
