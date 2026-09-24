"""Reading measured facts back out of a warehouse.

Stage 3 builds both revisions and then has to compare them, which means querying the
results. The queries are deliberately cheap aggregates — counts, sums, null rates —
rather than row-by-row comparison: the goal is evidence a reviewer can act on, not a
full data diff.

Two adapters: DuckDB for the demo project, and Trino, which is what the tool is aimed
at. They differ in one respect that matters — DuckDB addresses a file and a schema,
Trino addresses a catalog and a schema, so the same "schema" argument means different
things and the Trino client carries its catalog explicitly.
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


class WarehouseUnavailable(RuntimeError):
    """THEMIS cannot reach, or cannot log in to, the warehouse it measures on.

    Kept apart from "this table does not exist" on purpose. A table missing on both sides
    is an empty delta, and an empty delta reads as *nothing moved*: a login that fails on
    every query would otherwise turn a whole review into "measured, unchanged" while dbt,
    logging in its own way, built everything fine. A warehouse THEMIS cannot read stops
    the measurement, and the review says it is incomplete.
    """


# Logins that need a person at a browser. A review runs unattended.
_INTERACTIVE_LOGINS = frozenset({"oauth", "oauth_console"})

# Trino errors that mean "you may not read this", not "this is not here". Access control
# also hides tables from information_schema, so a denied read can look like an absent one.
_NOT_READABLE = frozenset({"PERMISSION_DENIED", "CATALOG_NOT_FOUND"})


def trino_connect(profile: dict[str, Any]) -> Any:
    """A Trino connection made the way dbt-trino makes one, from the same profile.

    Built from dbt-trino's own credential classes rather than a re-implementation, so
    every login dbt supports — ldap, jwt, certificate, kerberos, gssapi — works here with
    the same fields and the same meaning, and cannot drift from what dbt did when it built
    the tables. The profile is rendered first (`{{ env_var(...) }}`), in memory only.
    """
    import trino

    from themis.execute.profiles import ProfileError, render_profile

    try:
        rendered = render_profile(profile)
    except ProfileError as exc:
        raise WarehouseUnavailable(str(exc)) from exc

    method = str(rendered.get("method") or "none").lower()
    if method in _INTERACTIVE_LOGINS:
        raise WarehouseUnavailable(
            f"the profile logs in with {method!r}, which needs a person at a browser, and a "
            "review runs unattended. Use a service account with ldap, jwt, certificate or "
            "kerberos for the target THEMIS uses."
        )

    from dbt.adapters.trino.connections import TrinoCredentialsFactory

    fields = {key: value for key, value in rendered.items() if key != "type"}
    try:
        credentials = TrinoCredentialsFactory.from_dict(
            TrinoCredentialsFactory.translate_aliases(fields)
        )
        auth = credentials.trino_auth()
    except ImportError as exc:
        raise WarehouseUnavailable(
            f"the {method!r} login needs a package that is not installed ({exc.name}): "
            "uv pip install requests-kerberos (kerberos) or requests-gssapi (gssapi)"
        ) from exc
    except Exception as exc:
        raise WarehouseUnavailable(f"the profile does not describe a Trino login: {exc}") from exc

    # The same arguments dbt-trino's own connection.open() passes.
    user = getattr(credentials, "impersonation_user", None) or credentials.user
    connect: Any = trino.dbapi.connect
    return connect(
        host=credentials.host,
        port=int(credentials.port),
        user=user,
        client_tags=getattr(credentials, "client_tags", None),
        roles=getattr(credentials, "roles", None),
        catalog=credentials.database,
        schema=credentials.schema,
        http_scheme=credentials.http_scheme.value,
        http_headers=getattr(credentials, "http_headers", None),
        session_properties=getattr(credentials, "session_properties", None) or None,
        auth=auth,
        max_attempts=getattr(credentials, "retries", None) or 3,
        verify=credentials.cert if credentials.cert is not None else True,
        source="themis",
    )


def check_warehouse(profile: dict[str, Any], project_dir: Path) -> str:
    """Connect and read one row the way measurement will, or raise WarehouseUnavailable.

    `dbt debug` proves dbt can log in. This proves THEMIS can — they are different code
    and can use different credentials — and it runs before minutes of building, not after.
    """
    adapter = str(profile.get("type", "")).lower()
    if adapter == "trino":
        conn = trino_connect(profile)
        try:
            cursor = conn.cursor()
            cursor.execute("select current_user")
            (who,) = cursor.fetchone()
        except Exception as exc:
            raise WarehouseUnavailable(f"could not query the warehouse: {exc}") from exc
        finally:
            conn.close()
        return f"logged in to Trino as {who}"
    client = client_for_profile(profile, project_dir)
    if client is None:
        raise WarehouseUnavailable(f"no warehouse client for the {adapter!r} adapter")
    client.close()
    return f"opened the {adapter} database"


@dataclass(frozen=True)
class Relation:
    """Where a model physically is: catalog, schema, table.

    Taken from what dbt reports it built, never assembled from the run's schema and the
    model's name. A model with a custom schema, an alias or a catalog of its own lives
    somewhere else — and a lookup in the wrong place finds nothing on either side, which
    the differ reads as "nothing moved". On a project that sets `+schema:` per folder,
    as most do, that is most of the project reported clean without being measured.

    ``catalog`` None means the connection's own.
    """

    catalog: str | None
    schema: str
    name: str

    def describe(self) -> str:
        return ".".join(p for p in (self.catalog, self.schema, self.name) if p)


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


@dataclass(frozen=True)
class PairedRows:
    """Raw counts from pairing two builds of one table on a key."""

    rows_added: int
    rows_removed: int
    rows_changed: int
    columns_changed: dict[str, int]
    sample_keys: tuple[str, ...]
    # Only when the key carries a period. The latest period in the base build, how many
    # rows moved in a period earlier than it, and the earliest such period.
    latest_period: str | None = None
    prior_period_rows: int = 0
    earliest_changed_period: str | None = None


# The marker a paired row carries to say which side it came from. Quoted everywhere it
# is used, and long enough that no model column will share it.
_SIDE = "__themis_paired_side"

# Relative tolerance for numeric values. Summing or multiplying in a different order
# changes the last bits of a binary float, and a refactor that reorders arithmetic must
# not read as every row having changed. Far below any difference a reviewer cares about.
_RELATIVE_TOLERANCE = "1e-9"


def paired_rows_sql(
    base_ref: str,
    head_ref: str,
    *,
    key: tuple[str, ...],
    columns: tuple[str, ...],
    numeric: frozenset[str],
    quote: Any,
    sample_limit: int = 5,
    period: str | None = None,
) -> tuple[str, str]:
    """The two queries a keyed comparison needs: counts, and a few example keys.

    Portable between DuckDB and Trino on purpose — a full outer join on plain equality,
    and ``IS DISTINCT FROM`` so a value that became NULL counts as changed. Both engines
    accept all of it, so there is one statement to reason about rather than two.

    The join is ``=`` and not ``IS NOT DISTINCT FROM``, though the latter would pair NULL
    keys. Trino plans a null-safe comparison as a join *filter* rather than hash criteria:
    200,000 rows paired in 66 seconds against 0.2 with ``=``, and the cost grows with the
    square of the table — at the configured row ceiling it would not finish. So the caller
    refuses a key that contains NULLs, which is not a row identifier in any case.

    Whether the key identifies a row is not decided here either: the caller only asks once
    both builds have been counted unique on it.
    """
    q = quote
    side = q(_SIDE)
    selected = ", ".join(q(c) for c in (*key, *columns))

    def changed(column: str) -> str:
        b, h = f"b.{q(column)}", f"h.{q(column)}"
        if column in numeric:
            return (
                f"case when ({b} is null) <> ({h} is null) then 1 "
                f"when {b} is null then 0 "
                f"when abs({b} - {h}) > {_RELATIVE_TOLERANCE} * greatest(abs({b}), abs({h})) "
                "then 1 else 0 end"
            )
        return f"case when {b} is distinct from {h} then 1 else 0 end"

    flags = [f"{changed(c)} as {q('__changed_' + str(i))}" for i, c in enumerate(columns)]
    join = " and ".join(f"b.{q(k)} = h.{q(k)}" for k in key)
    key_text = ", ".join(
        f"coalesce(cast(coalesce(h.{q(k)}, b.{q(k)}) as varchar), 'NULL')" for k in key
    )
    period_select = (
        f", coalesce(h.{q(period)}, b.{q(period)}) as {q('period_value')}"
        if period is not None
        else ""
    )
    paired = (
        f"with b as (select 1 as {side}, {selected} from {base_ref}), "
        f"h as (select 1 as {side}, {selected} from {head_ref}), "
        "paired as (select "
        f"b.{side} as in_base, h.{side} as in_head, "
        f"concat_ws(' | ', {key_text}) as key_text"
        + period_select
        + (", " + ", ".join(flags) if flags else "")
        + f" from b full outer join h on {join}) "
    )
    both = "in_base is not null and in_head is not null"
    any_changed = " or ".join(f"{q('__changed_' + str(i))} = 1" for i in range(len(columns)))
    differs_for_period = "in_base is null or in_head is null" + (
        f" or ({any_changed})" if columns else ""
    )

    counts = [
        "sum(case when in_base is null then 1 else 0 end)",
        "sum(case when in_head is null then 1 else 0 end)",
        (f"sum(case when {both} and ({any_changed}) then 1 else 0 end)" if columns else "0"),
        *(
            f"sum(case when {both} then {q('__changed_' + str(i))} else 0 end)"
            for i in range(len(columns))
        ),
    ]
    if period is not None:
        # A period in the key makes one more question answerable, and it is the question a
        # bank asks first: did anything move in a period that has already been reported?
        # The latest period is the one still open; a row that changed in any earlier period
        # is a restatement, whatever else the change is. Measured in the same pass, because
        # a second query over the same tables is the expensive part.
        counts += [
            f"max(cast({q('period_value')} as varchar))",
            (
                f"sum(case when ({differs_for_period}) and {q('period_value')} < "
                f"(select max({q(period)}) from b) then 1 else 0 end)"
            ),
            (
                f"min(case when ({differs_for_period}) and {q('period_value')} < "
                f"(select max({q(period)}) from b) "
                f"then cast({q('period_value')} as varchar) end)"
            ),
        ]
    counts_sql = paired + "select " + ", ".join(counts) + " from paired"

    differs = "in_base is null or in_head is null" + (f" or ({any_changed})" if columns else "")
    sample_sql = (
        paired
        + f"select key_text from paired where {differs} order by key_text limit {sample_limit}"
    )
    return counts_sql, sample_sql


def _paired_rows(
    run: Any,
    base_ref: str,
    head_ref: str,
    *,
    key: tuple[str, ...],
    columns: tuple[str, ...],
    numeric: frozenset[str],
    quote: Any,
    period: str | None = None,
) -> PairedRows | None:
    counts_sql, sample_sql = paired_rows_sql(
        base_ref,
        head_ref,
        key=key,
        columns=columns,
        numeric=numeric,
        quote=quote,
        period=period,
    )
    rows = run(counts_sql)
    if not rows or rows[0][0] is None:
        return None
    values = [int(v or 0) for v in rows[0][: 3 + len(columns)]]
    samples = run(sample_sql)
    period_values: list[Any] = list(rows[0][3 + len(columns) :]) if period is not None else []
    return PairedRows(
        rows_added=values[0],
        rows_removed=values[1],
        rows_changed=values[2],
        columns_changed={
            column: count
            for column, count in zip(columns, values[3 : 3 + len(columns)], strict=False)
            if count
        },
        sample_keys=tuple(str(row[0]) for row in samples if row and row[0] is not None),
        latest_period=(
            str(period_values[0]) if period_values and period_values[0] is not None else None
        ),
        prior_period_rows=(int(period_values[1] or 0) if len(period_values) > 1 else 0),
        earliest_changed_period=(
            str(period_values[2])
            if len(period_values) > 2 and period_values[2] is not None
            else None
        ),
    )


class WarehouseClient(Protocol):
    """The measurements Stage 3 needs. Deliberately small."""

    def shape(self, relation: Relation) -> TableShape: ...

    def sums(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]: ...

    def null_rates(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]: ...

    def distinct_count(self, relation: Relation, columns: tuple[str, ...]) -> int | None: ...

    def paired_rows(
        self,
        base: Relation,
        head: Relation,
        *,
        key: tuple[str, ...],
        columns: tuple[str, ...],
        numeric: frozenset[str],
        period: str | None = None,
    ) -> PairedRows | None: ...

    def close(self) -> None: ...


class DuckDBClient:
    """DuckDB implementation. Read-only — Stage 3 measures, dbt writes."""

    def __init__(self, database: Path, attached: tuple[tuple[Path, str], ...] = ()) -> None:
        import duckdb

        self._conn = duckdb.connect(str(database), read_only=True)
        # A model dbt put in an attached database is only readable once it is attached
        # here too, under the alias dbt used as its catalog.
        for path, alias in attached:
            if path.exists():
                quoted = str(path).replace("'", "''")
                self._conn.execute(f"attach '{quoted}' as {self._quote(alias)} (read_only)")

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

    @staticmethod
    def _literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _ref(self, relation: Relation) -> str:
        parts = (relation.catalog, relation.schema, relation.name)
        return ".".join(self._quote(part) for part in parts if part)

    def shape(self, relation: Relation) -> TableShape:
        catalog = (
            f"and table_catalog = {self._literal(relation.catalog)} " if relation.catalog else ""
        )
        columns = self._query(
            "select column_name, data_type from information_schema.columns "
            f"where table_schema = {self._literal(relation.schema)} "
            f"and table_name = {self._literal(relation.name)} {catalog}"
        )
        if not columns:
            return TableShape(exists=False)
        rows = self._query(f"select count(*) from {self._ref(relation)}")
        return TableShape(
            exists=True,
            row_count=int(rows[0][0]) if rows else 0,
            column_types={str(name): str(dtype) for name, dtype in columns},
        )

    def sums(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]:
        if not columns:
            return {}
        # One query for every column: a sum per column across millions of rows is
        # still a single scan, and N queries would be N scans.
        projection = ", ".join(f"sum({self._quote(c)})" for c in columns)
        rows = self._query(f"select {projection} from {self._ref(relation)}")
        if not rows:
            return {}
        return {
            column: float(value)
            for column, value in zip(columns, rows[0], strict=False)
            if value is not None
        }

    def null_rates(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]:
        if not columns:
            return {}
        projection = ", ".join(
            f"cast(count(*) - count({self._quote(c)}) as double) / nullif(count(*), 0)"
            for c in columns
        )
        rows = self._query(f"select {projection} from {self._ref(relation)}")
        if not rows:
            return {}
        return {
            column: float(value)
            for column, value in zip(columns, rows[0], strict=False)
            if value is not None
        }

    def distinct_count(self, relation: Relation, columns: tuple[str, ...]) -> int | None:
        """Distinct combinations of a candidate key.

        Paired with the row count this settles grain outright: equal means the key is
        genuinely unique, and a shortfall gives the exact rows-per-key multiplier that
        inference can only guess at.
        """
        if not columns:
            return None
        key = ", ".join(self._quote(c) for c in columns)
        expression = f"({key})" if len(columns) > 1 else key
        rows = self._query(f"select count(distinct {expression}) from {self._ref(relation)}")
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def paired_rows(
        self,
        base: Relation,
        head: Relation,
        *,
        key: tuple[str, ...],
        columns: tuple[str, ...],
        numeric: frozenset[str],
        period: str | None = None,
    ) -> PairedRows | None:
        """Pair base and head rows on ``key`` and count what differs."""
        return _paired_rows(
            self._query,
            self._ref(base),
            self._ref(head),
            key=key,
            columns=columns,
            numeric=numeric,
            quote=self._quote,
            period=period,
        )

    def close(self) -> None:
        self._conn.close()


class TrinoClient:
    """Trino, which is what this tool is actually aimed at.

    Every query is a bounded aggregate — counts, sums, null rates — because Stage 3
    exists to produce evidence a reviewer can act on, not to diff data. That matters
    more on Trino than on DuckDB: a full comparison against a warehouse table would be
    a real cost, and the whole design depends on Stage 3 being cheap enough to run on
    every pull request.
    """

    def __init__(
        self,
        *,
        catalog: str,
        host: str = "",
        port: int = 8080,
        user: str = "themis",
        http_scheme: str = "http",
        password: str | None = None,
        connection: Any | None = None,
    ) -> None:
        import trino

        self._catalog = catalog
        if connection is not None:
            # Made by trino_connect, from the profile, with whatever login it names.
            self._conn = connection
            return
        auth = trino.auth.BasicAuthentication(user, password) if password else None
        # The driver ships no annotations, so its DB-API entry point reads as untyped.
        # Narrowed here rather than by relaxing the check for this module, which would
        # also hide genuinely untyped calls elsewhere in the file.
        connect: Any = trino.dbapi.connect
        self._conn = connect(
            host=host,
            port=port,
            user=user,
            catalog=catalog,
            http_scheme=http_scheme,
            auth=auth,
        )

    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        import trino

        try:
            cursor = self._conn.cursor()
            cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        except trino.exceptions.TrinoUserError as exc:
            if getattr(exc, "error_name", None) in _NOT_READABLE:
                raise WarehouseUnavailable(f"not allowed to read: {exc.message}") from exc
            # A query the table cannot answer — a model missing on one side of the diff,
            # a column of a type the aggregate refuses. That degrades to "unknown" for
            # this model, never to a wrong number presented as measured.
            log.debug("warehouse.query_failed", sql=sql[:120], error=str(exc)[:200])
            return []
        except Exception as exc:
            # Unreachable, refused the login, or failed inside: not a fact about any
            # table, and never to be read as one.
            raise WarehouseUnavailable(f"could not query the warehouse: {exc}") from exc

    @staticmethod
    def _quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    @staticmethod
    def _literal(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _ref(self, relation: Relation) -> str:
        # Fully qualified. Trino resolves an unqualified name against the session
        # catalog, and a model may live in another catalog as well as another schema.
        parts = (relation.catalog or self._catalog, relation.schema, relation.name)
        return ".".join(self._quote(part) for part in parts)

    def shape(self, relation: Relation) -> TableShape:
        # The information schema of the catalog the model is in: the session's catalog
        # knows nothing about a table in another one.
        catalog = self._quote(relation.catalog or self._catalog)
        columns = self._query(
            f"select column_name, data_type from {catalog}.information_schema.columns "
            f"where table_schema = {self._literal(relation.schema)} "
            f"and table_name = {self._literal(relation.name)}"
        )
        if not columns:
            return TableShape(exists=False)
        rows = self._query(f"select count(*) from {self._ref(relation)}")
        return TableShape(
            exists=True,
            row_count=int(rows[0][0]) if rows else 0,
            column_types={str(name): str(dtype) for name, dtype in columns},
        )

    def sums(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]:
        if not columns:
            return {}
        projection = ", ".join(f"sum({self._quote(c)})" for c in columns)
        rows = self._query(f"select {projection} from {self._ref(relation)}")
        if not rows:
            return {}
        return {
            column: float(value)
            for column, value in zip(columns, rows[0], strict=False)
            if value is not None
        }

    def null_rates(self, relation: Relation, columns: tuple[str, ...]) -> dict[str, float]:
        if not columns:
            return {}
        projection = ", ".join(
            f"cast(count(*) - count({self._quote(c)}) as double) / nullif(count(*), 0)"
            for c in columns
        )
        rows = self._query(f"select {projection} from {self._ref(relation)}")
        if not rows:
            return {}
        return {
            column: float(value)
            for column, value in zip(columns, rows[0], strict=False)
            if value is not None
        }

    def distinct_count(self, relation: Relation, columns: tuple[str, ...]) -> int | None:
        """Distinct combinations of a candidate key.

        Trino has no row-constructor equality in count(distinct ...), so a composite
        key is concatenated instead. The separator is a character that cannot occur in
        an identifier or a normal value, so two different keys cannot collide by
        happening to concatenate to the same string.
        """
        if not columns:
            return None
        if len(columns) == 1:
            expression = self._quote(columns[0])
        else:
            parts = ", ".join(f"cast({self._quote(c)} as varchar)" for c in columns)
            expression = f"concat_ws(chr(31), {parts})"
        rows = self._query(f"select count(distinct {expression}) from {self._ref(relation)}")
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    def paired_rows(
        self,
        base: Relation,
        head: Relation,
        *,
        key: tuple[str, ...],
        columns: tuple[str, ...],
        numeric: frozenset[str],
        period: str | None = None,
    ) -> PairedRows | None:
        """Pair base and head rows on ``key`` and count what differs."""
        return _paired_rows(
            self._query,
            self._ref(base),
            self._ref(head),
            key=key,
            columns=columns,
            numeric=numeric,
            quote=self._quote,
            period=period,
        )

    def close(self) -> None:
        self._conn.close()


def _belongs_to_run(schema: str, prefixes: tuple[str, ...]) -> bool:
    """Whether a schema is one this run created.

    dbt appends a model's custom schema to the target schema (``themis_head_1a2b_main``),
    so a run owns its own names and anything extending them — and nothing else. The run
    token in the prefix is what keeps this from ever matching a schema somebody uses.
    """
    lowered = schema.lower()
    return any(lowered == p.lower() or lowered.startswith(p.lower() + "_") for p in prefixes)


def _duckdb_files(profile: dict[str, Any], project_dir: Path) -> list[Path]:
    """The database file a DuckDB profile writes to, plus every file it attaches."""
    candidates: list[str] = [str(profile.get("path", ""))]
    for entry in profile.get("attach") or []:
        if isinstance(entry, dict) and entry.get("path"):
            candidates.append(str(entry["path"]))
    files: list[Path] = []
    for raw in candidates:
        if not raw or raw == ":memory:" or "://" in raw or raw.startswith("md:"):
            continue
        path = Path(raw)
        files.append(path if path.is_absolute() else (project_dir / path).resolve())
    return [f for f in files if f.exists()]


def drop_run_schemas(
    profile: dict[str, Any],
    project_dir: Path,
    prefixes: tuple[str, ...],
    *,
    catalogs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Drop the schemas one Stage 3 run built into. Returns what was dropped.

    This is the only write THEMIS makes itself — dbt does every other one — and it is
    confined to names carrying this run's token. It exists because the alternative is
    worse in both directions: schemas shared between runs let one run measure another's
    leftovers, and per-run schemas never removed accumulate a copy of the measured
    closure per review.

    Best effort. A failure is logged and the review stands: a stray schema costs space,
    and the measurement already happened against relations this run built.
    """
    adapter = str(profile.get("type", "")).lower()
    dropped: list[str] = []
    try:
        if adapter == "duckdb":
            import duckdb

            for database in _duckdb_files(profile, project_dir):
                conn = duckdb.connect(str(database))
                try:
                    names = [
                        str(row[0])
                        for row in conn.execute(
                            "select schema_name from information_schema.schemata "
                            "where catalog_name = current_database()"
                        ).fetchall()
                    ]
                    for name in names:
                        if _belongs_to_run(name, prefixes):
                            conn.execute(f'drop schema if exists "{name}" cascade')
                            dropped.append(name)
                finally:
                    conn.close()
        elif adapter == "trino":
            # The profile's catalog and every other one the builds put a model in: a run
            # that wrote Hive marts and Iceberg reference data has schemas in both.
            own = str(profile.get("database") or profile.get("catalog") or "")
            for catalog in dict.fromkeys((own, *catalogs)):
                if catalog:
                    dropped.extend(_drop_trino_schemas(profile, prefixes, catalog))
        else:
            log.warning("warehouse.cleanup_unsupported", adapter=adapter)
    except Exception as exc:
        log.warning("warehouse.cleanup_failed", error=str(exc)[:300], prefixes=list(prefixes))
    if dropped:
        log.info("warehouse.schemas_dropped", schemas=dropped)
    return tuple(dropped)


def _drop_trino_schemas(
    profile: dict[str, Any], prefixes: tuple[str, ...], catalog: str
) -> list[str]:
    """Drop a run's schemas in one Trino catalog, relation by relation.

    ``DROP SCHEMA ... CASCADE`` is not supported by every connector, so the relations
    are dropped first and the schema after — which works on all of them.
    """
    conn = trino_connect(profile)

    def run(sql: str) -> list[tuple[Any, ...]]:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [tuple(row) for row in cursor.fetchall()]

    def quote(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    dropped: list[str] = []
    try:
        schemas = [
            str(row[0])
            for row in run(f"select schema_name from {quote(catalog)}.information_schema.schemata")
        ]
        for schema in schemas:
            if not _belongs_to_run(schema, prefixes):
                continue
            relations = run(
                f"select table_name, table_type from {quote(catalog)}.information_schema.tables "
                f"where table_schema = '{schema}'"
            )
            for name, kind in relations:
                statement = "drop view" if str(kind).upper() == "VIEW" else "drop table"
                run(f"{statement} if exists {quote(catalog)}.{quote(schema)}.{quote(str(name))}")
            run(f"drop schema if exists {quote(catalog)}.{quote(schema)}")
            dropped.append(f"{catalog}.{schema}")
    finally:
        conn.close()
    return dropped


def client_for_profile(profile: dict[str, Any], project_dir: Path) -> WarehouseClient | None:
    """Build a client from a resolved dbt profile output, or None if unsupported."""
    adapter = str(profile.get("type", "")).lower()

    if adapter == "trino":
        catalog = str(profile.get("database") or profile.get("catalog") or "")
        if not catalog:
            raise WarehouseUnavailable("the Trino profile names no catalog")
        # Raises WarehouseUnavailable, naming why, rather than handing back half a client
        # that would fail later — mid-review, and quietly.
        return TrinoClient(catalog=catalog, connection=trino_connect(profile))

    if adapter != "duckdb":
        log.warning(
            "warehouse.unsupported_adapter",
            adapter=adapter,
            hint="Stage 3 measurement supports duckdb and trino",
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
    attached: list[tuple[Path, str]] = []
    for entry in profile.get("attach") or []:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        path = Path(str(entry["path"]))
        if not path.is_absolute():
            path = (project_dir / path).resolve()
        attached.append((path, str(entry.get("alias") or path.stem)))
    return DuckDBClient(database, tuple(attached))
