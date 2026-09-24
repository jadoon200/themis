"""Reading a snapshot: one version per key, or every version.

A dbt snapshot's table keeps one row per version of each key. A model reading it wants
one of two things: the version in force now (``where dbt_valid_to is null``), or the one
in force at some moment (an as-of join between ``dbt_valid_from`` and ``dbt_valid_to``).
Either restricts the read to a row per key. A read that does neither gets every version,
so each key counts once per change ever recorded against it — a fan-out that grows by
itself, with no change to any code, every time the source changes.

On the day it is written the fan-out is invisible: a snapshot holds one version of
everything until something changes. That is why this is read from the SQL rather than
measured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql, select_from, select_joins
from themis.snapshot import ModelNode, ProjectSnapshot

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def key_columns(node: ModelNode, *, dialect: str = "trino") -> tuple[str, ...]:
    """The columns a snapshot's unique_key is made of.

    Usually the key is a column or a list of them. Projects written before list keys
    existed concatenate instead — `currency_code || '|' || cast(rate_date as varchar)` —
    and compared as text, that expression covers no column at all, so a key that does
    identify a row read as one that does not. An entry nothing can be read from empties
    the answer: a key that cannot be read is not a key anything can rely on.
    """
    columns: list[str] = []
    for entry in node.unique_key:
        text = entry.strip().strip('"')
        if _IDENTIFIER.match(text):
            names = [text]
        else:
            try:
                names = [
                    column.name for column in parse_sql(entry, dialect=dialect).find_all(exp.Column)
                ]
            except ParseError:
                names = []
        if not names:
            return ()
        columns.extend(name for name in names if name not in columns)
    return tuple(columns)


@dataclass(frozen=True)
class SnapshotRead:
    """One place a query consumes a snapshot's rows."""

    snapshot: str
    # A predicate on the valid-to column, here or in a CTE the rows came through,
    # limits the read to one row per key.
    one_version: bool


def snapshot_parents(model: ModelNode, project: ProjectSnapshot) -> dict[str, ModelNode]:
    """The snapshots a model reads, by name."""
    parents: dict[str, ModelNode] = {}
    for dependency in model.depends_on_models:
        name = dependency.split(".")[-1]
        node = project.models.get(name)
        if node is not None and node.is_snapshot and node.history is not None:
            parents[name] = node
    return parents


def _table_names(node: ModelNode) -> set[str]:
    """Every spelling a compiled reference to this node can carry."""
    names = {node.name.lower()}
    if node.relation_name:
        qualified = node.relation_name.replace('"', "").replace("`", "").lower()
        names.add(qualified)
        names.add(qualified.split(".")[-1])
    return names


def _spellings(table: exp.Table) -> set[str]:
    parts = (table.args.get("catalog"), table.args.get("db"), table.this)
    qualified = ".".join(part.name for part in parts if isinstance(part, exp.Identifier))
    return {qualified.lower(), table.name.lower()}


def _mentions(expression: exp.Expression | None, columns: set[str]) -> bool:
    if expression is None:
        return False
    return any(column.name.lower() in columns for column in expression.find_all(exp.Column))


def _as_select(node: exp.Expression) -> exp.Select | None:
    return node if isinstance(node, exp.Select) else None


def _carriers(tree: exp.Expression, snapshot: ModelNode) -> dict[str, bool]:
    """Names that stand for the snapshot's rows, and whether those rows are one version.

    The table itself, and every CTE that passes it straight through — no join, reading
    the table or another such CTE. dbt models wrap each ref() in exactly such a CTE and
    filter later, so a restriction has to be followed along the chain, in either
    direction: filtered in the CTE and read plainly after, or read plainly into the CTE
    and filtered after.
    """
    history = snapshot.history
    valid_to = {history.valid_to.lower()} if history is not None else set()
    carriers = dict.fromkeys(_table_names(snapshot), False)
    ctes = {
        cte.alias_or_name.lower(): cte.this for cte in tree.find_all(exp.CTE) if cte.alias_or_name
    }
    for _ in range(len(ctes) + 1):
        grown = False
        for alias, body in ctes.items():
            if alias in carriers:
                continue
            select = _as_select(body)
            if select is None or select_joins(select):
                continue
            source = select_from(select)
            if source is None or not isinstance(source.this, exp.Table):
                continue
            hit = next((n for n in _spellings(source.this) if n in carriers), None)
            if hit is None:
                continue
            carriers[alias] = carriers[hit] or _mentions(select.args.get("where"), valid_to)
            grown = True
        if not grown:
            break
    return carriers


def reads_of(tree: exp.Expression, snapshot: ModelNode) -> list[SnapshotRead]:
    """Each place the snapshot's rows are consumed, and whether one version is taken.

    A consuming read is one that is not itself a pass-through CTE of the snapshot. It is
    restricted when the rows arrived restricted, or when the select consuming them filters
    on the valid-to column — in its WHERE, or in the ON of the join that brings them in.
    Any predicate on that column counts: ``is null`` for the current version, a comparison
    for an as-of join, the equality a project uses with ``dbt_valid_to_current``.
    """
    history = snapshot.history
    valid_to = {history.valid_to.lower()} if history is not None else set()
    carriers = _carriers(tree, snapshot)
    passthrough_bodies = {
        id(cte.this)
        for cte in tree.find_all(exp.CTE)
        if cte.alias_or_name and cte.alias_or_name.lower() in carriers
    }

    reads: list[SnapshotRead] = []
    for table in tree.find_all(exp.Table):
        hit = next((n for n in _spellings(table) if n in carriers), None)
        if hit is None:
            continue
        select = table.find_ancestor(exp.Select)
        if select is None or id(select) in passthrough_bodies:
            continue
        join = table.find_ancestor(exp.Join)
        on = (
            join.args.get("on")
            if join is not None and join.find_ancestor(exp.Select) is select
            else None
        )
        restricted = (
            carriers[hit]
            or _mentions(select.args.get("where"), valid_to)
            or _mentions(on, valid_to)
        )
        reads.append(SnapshotRead(snapshot=snapshot.name, one_version=restricted))
    return reads


def restricted_to_one_version(tree: exp.Expression, snapshot: ModelNode) -> bool:
    """Whether every read of the snapshot in this statement takes one version per key."""
    reads = reads_of(tree, snapshot)
    return bool(reads) and all(read.one_version for read in reads)


def keeps_versions(tree: exp.Expression, snapshot: ModelNode) -> bool:
    """Whether the statement's output hands a version column on, a star included.

    Handing them on is how a history model is written — a table of changes keyed by
    ``dbt_valid_from`` — so reading every version is the point of it. A star counts,
    because what it expands to cannot be told from here, and saying nothing is the
    answer that cannot invent a finding.
    """
    history = snapshot.history
    if history is None:
        return False
    versions = {name.lower() for name in history.meta_columns}
    outer = tree
    while isinstance(outer, exp.SetOperation):
        outer = outer.this
    if not isinstance(outer, exp.Select):
        return True
    for projection in outer.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            return True
        if projection.alias_or_name.lower() in versions or _mentions(projection, versions):
            return True
    return False


def reads_every_version(tree: exp.Expression, snapshot: ModelNode) -> bool:
    """Whether some read takes every version while the output drops them.

    What comes out then looks keyed by the snapshot's key and is not: each key appears
    once for every version recorded against it.
    """
    if keeps_versions(tree, snapshot):
        return False
    return any(not read.one_version for read in reads_of(tree, snapshot))
