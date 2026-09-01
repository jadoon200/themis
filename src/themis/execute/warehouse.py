"""Reading measured facts back out of a warehouse.

Stage 3 builds both revisions and then has to compare them, which means querying the
results. The queries are deliberately cheap aggregates — counts, sums, null rates —
rather than row-by-row comparison: the goal is evidence a reviewer can act on, not a
full data diff.

Only DuckDB is implemented. That is honest rather than limiting: the POC runs on
DuckDB, and a Trino client is a small addition against the same protocol when the
office lane is picked up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from themis.logging import get_logger

log = get_logger(__name__)

# Column types that can hold money. DOUBLE and FLOAT are included deliberately: a
# monetary column stored as one is itself a defect, and excluding them here would hide
# exactly the case F3 exists to catch.
_NUMERIC_TYPES = (
    "decimal",
    "numeric",
    "double",
    "float",
    "real",
    "bigint",
    "integer",
    "int",
    "hugeint",
)


@dataclass(frozen=True)
class TableShape:
    """What a materialised table looks like, as measured rather than declared."""

    exists: bool
    row_count: int = 0
    column_types: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.column_types is None:
            object.__setattr__(self, "column_types", {})

    @property
    def numeric_columns(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, type_name in self.column_types.items()
            if any(t in type_name.lower() for t in _NUMERIC_TYPES)
        )


class WarehouseClient(Protocol):
    """The measurements Stage 3 needs. Deliberately small."""

    def shape(self, schema: str, table: str) -> TableShape: ...

    def sums(self, schema: str, table: str, columns: tuple[str, ...]) -> dict[str, float]: ...

    def null_rates(self, schema: str, table: str, columns: tuple[str, ...]) -> dict[str, float]: ...

    def distinct_count(self, schema: str, table: str, columns: tuple[str, ...]) -> int | None: ...

    def close(self) -> None: ...


class DuckDBClient:
    """DuckDB implementation. Read-only — Stage 3 measures, dbt writes."""

    def __init__(self, database: Path) -> None:
        import duckdb

        self._conn = duckdb.connect(str(database), read_only=True)

    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        try:
            return list(self._conn.execute(sql).fetchall())
        except Exception as exc:
            # A missing table is a normal outcome — a model may not exist on one side
            # of the diff. Measurement failure must degrade to "unknown", never to a
            # wrong number presented as measured.
            log.debug("warehouse.query_failed", sql=sql[:120], error=str(exc)[:200])
            return []

    @staticmethod
    def _quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def _ref(self, schema: str, table: str) -> str:
        return f"{self._quote(schema)}.{self._quote(table)}"

    def shape(self, schema: str, table: str) -> TableShape:
        columns = self._query(
            "select column_name, data_type from information_schema.columns "
            f"where table_schema = '{schema}' and table_name = '{table}'"
        )
        if not columns:
            return TableShape(exists=False)
        rows = self._query(f"select count(*) from {self._ref(schema, table)}")
        return TableShape(
            exists=True,
            row_count=int(rows[0][0]) if rows else 0,
            column_types={str(name): str(dtype) for name, dtype in columns},
        )

    def sums(self, schema: str, table: str, columns: tuple[str, ...]) -> dict[str, float]:
        if not columns:
            return {}
        # One query for every column: a sum per column across millions of rows is
        # still a single scan, and N queries would be N scans.
        projection = ", ".join(f"sum({self._quote(c)})" for c in columns)
        rows = self._query(f"select {projection} from {self._ref(schema, table)}")
        if not rows:
            return {}
        return {
            column: float(value)
            for column, value in zip(columns, rows[0], strict=False)
            if value is not None
        }

    def null_rates(self, schema: str, table: str, columns: tuple[str, ...]) -> dict[str, float]:
        if not columns:
            return {}
        projection = ", ".join(
            f"cast(count(*) - count({self._quote(c)}) as double) / nullif(count(*), 0)"
            for c in columns
        )
        rows = self._query(f"select {projection} from {self._ref(schema, table)}")
        if not rows:
            return {}
        return {
            column: float(value)
            for column, value in zip(columns, rows[0], strict=False)
            if value is not None
        }

    def distinct_count(self, schema: str, table: str, columns: tuple[str, ...]) -> int | None:
        """Distinct combinations of a candidate key.

        Paired with the row count this settles grain outright: equal means the key is
        genuinely unique, and a shortfall gives the exact rows-per-key multiplier that
        inference can only guess at.
        """
        if not columns:
            return None
        key = ", ".join(self._quote(c) for c in columns)
        expression = f"({key})" if len(columns) > 1 else key
        rows = self._query(f"select count(distinct {expression}) from {self._ref(schema, table)}")
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def close(self) -> None:
        self._conn.close()


def client_for_profile(profile: dict[str, Any], project_dir: Path) -> WarehouseClient | None:
    """Build a client from a resolved dbt profile output, or None if unsupported."""
    adapter = str(profile.get("type", "")).lower()
    if adapter != "duckdb":
        log.warning(
            "warehouse.unsupported_adapter",
            adapter=adapter,
            hint="Stage 3 measurement currently supports duckdb only",
        )
        return None
    raw_path = str(profile.get("path", ""))
    if not raw_path or raw_path == ":memory:":
        # An in-memory database does not survive the dbt process, so there is nothing
        # left to measure once the build finishes.
        log.warning("warehouse.no_persistent_database", path=raw_path)
        return None
    database = Path(raw_path)
    if not database.is_absolute():
        database = (project_dir / database).resolve()
    if not database.exists():
        log.warning("warehouse.database_missing", path=str(database))
        return None
    return DuckDBClient(database)
