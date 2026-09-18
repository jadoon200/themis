"""Which models a change can actually reach, column by column.

Stage 3 builds and compares every model below the one that changed, which is correct and,
on a real warehouse, the expensive part of a review. A change to one column of a staging
model does not touch a mart that never reads that column, and SQLMesh's plan algorithm
narrows exactly this way: find the columns that changed, follow column lineage, and skip
the descendants that lineage says are untouched.

The reason this is opt-in here is the failure mode. Everything else in THEMIS fails towards
reporting too much; a wrong narrowing fails towards measuring nothing, and a model that was
never measured looks exactly like a model that did not move. So narrowing is refused
outright unless it can be *proved* for the whole set:

- every changed model's change confined to named output columns — a different FROM, join,
  filter, grouping or set operation changes which rows exist, and that reaches every column;
- every model in the affected set traced by column lineage, with nothing unresolved;
- no changed seed, because a data change has no columns to confine it to.

When any of that fails, nothing is narrowed and the reason is reported. What *is* narrowed
away is reported too, by name: a measurement that did not happen has to be visible, or the
saving is bought with the one thing this tool refuses to spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.grain import _passthrough_target
from themis.analyze.lineage import ColumnGraph
from themis.analyze.parse import (
    ParseError,
    find_ctes,
    normalise,
    parse_sql,
)
from themis.snapshot import ProjectSnapshot

# The clauses that decide which rows exist. A change in any of them can move any column.
_ROW_SHAPING = ("from", "joins", "where", "group", "having", "qualify", "distinct", "limit")


@dataclass(frozen=True)
class Narrowing:
    """What execution may skip, and why it may not."""

    kept: frozenset[str] = frozenset()
    excluded: dict[str, str] = field(default_factory=dict)
    refused: str | None = None

    @property
    def applied(self) -> bool:
        return self.refused is None and bool(self.excluded)


def _final_select(sql: str, dialect: str) -> exp.Select | None:
    """The select that decides this model's output, resolved through pass-throughs.

    Almost every real dbt model is `with ... final as (...) select * from final`, so
    reading the outermost select alone answers "a star, nothing can be confined" for the
    whole project and the narrowing never applies to anything. The grain lattice already
    resolves this shape; the same walk is used here, and it stops at anything that changes
    which rows exist.
    """
    try:
        tree = normalise(parse_sql(sql, dialect=dialect))
    except ParseError:
        return None
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None
    ctes = find_ctes(tree)
    seen: set[int] = set()
    # Bounded: a chain of pass-throughs is a few links, and a cyclic manifest must not spin.
    for _ in range(10):
        if id(select) in seen or not _is_pure_passthrough(select):
            break
        seen.add(id(select))
        target = _passthrough_target(select, ctes)
        if target is None:
            break
        select = target[1]
    return select


def _is_pure_passthrough(select: exp.Select) -> bool:
    """`select * from x`, with nothing else in it — no filter, no grouping, no distinct."""
    if len(select.expressions) != 1:
        return False
    item = select.expressions[0]
    star = isinstance(item, exp.Star) or (
        isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
    )
    if not star:
        return False
    return not any(select.args.get(key) for key in _ROW_SHAPING if key != "from")


def _shape(select: exp.Select, dialect: str) -> str:
    """Everything except the projection, as text — what decides which rows exist."""
    parts = []
    for key in _ROW_SHAPING:
        value = select.args.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            parts.append(f"{key}:" + "|".join(item.sql(dialect=dialect) for item in value))
        else:
            parts.append(f"{key}:{value.sql(dialect=dialect)}")
    return ";".join(parts)


def changed_outputs(before_sql: str, after_sql: str, *, dialect: str) -> tuple[str, ...] | None:
    """The output columns whose definition changed, or None if the change is not confined.

    None is not "nothing changed" — it is "this cannot be reduced to a set of columns", and
    every caller has to treat it as reaching everything.
    """
    before = _final_select(before_sql, dialect)
    after = _final_select(after_sql, dialect)
    if before is None or after is None:
        return None
    # A star hides which columns exist at all, so nothing about it can be confined.
    for select in (before, after):
        if any(isinstance(item, exp.Star) for item in select.expressions):
            return None
        if any(
            isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
            for item in select.expressions
        ):
            return None
    if _shape(before, dialect) != _shape(after, dialect):
        return None

    def projections(select: exp.Select) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in select.expressions:
            name = item.alias_or_name
            if not name:
                return {}
            inner = item.this if isinstance(item, exp.Alias) else item
            out[name] = inner.sql(dialect=dialect)
        return out

    old, new = projections(before), projections(after)
    if not old or not new:
        return None
    dirty = {name for name in set(old) | set(new) if old.get(name) != new.get(name)}
    return tuple(sorted(dirty))


def narrow(
    *,
    changed: dict[str, tuple[str, ...] | None],
    candidates: set[str],
    graph: ColumnGraph | None,
    snapshot: ProjectSnapshot,
    changed_seeds: tuple[str, ...] = (),
) -> Narrowing:
    """Which of ``candidates`` a change can reach, or a refusal to decide.

    ``changed`` maps each changed model to its dirty output columns, or None where the
    change could not be confined to columns.
    """
    keep_all = frozenset(candidates)
    if graph is None:
        return Narrowing(kept=keep_all, refused="column lineage was not built for this review")
    if changed_seeds:
        return Narrowing(
            kept=keep_all,
            refused=f"a seed changed ({', '.join(sorted(changed_seeds))}) — a data change "
            "has no columns to confine it to",
        )
    unconfined = sorted(name for name, columns in changed.items() if columns is None)
    if unconfined:
        return Narrowing(
            kept=keep_all,
            refused=(
                f"{', '.join(unconfined[:3])} changed in a way that is not confined to "
                "named columns, so every column below it may move"
            ),
        )
    untraced = sorted(name for name in candidates | set(changed) if not graph.is_traced(name))
    if untraced:
        return Narrowing(
            kept=keep_all,
            refused=(
                f"column lineage could not trace {', '.join(untraced[:3])}"
                + (f" and {len(untraced) - 3} more" if len(untraced) > 3 else "")
            ),
        )

    reached: set[str] = set()
    for model, columns in changed.items():
        for column in columns or ():
            for ref in graph.consumers_of(model, column):
                reached.add(ref.model)

    kept: set[str] = set()
    excluded: dict[str, str] = {}
    for name in candidates:
        if name in changed or name in reached:
            kept.add(name)
            continue
        if snapshot.models.get(name) is None:
            kept.add(name)
            continue
        excluded[name] = "reads no column that changed"
    return Narrowing(kept=frozenset(kept), excluded=excluded)


def dirty_columns(
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    names: set[str],
    *,
    dialect: str,
) -> dict[str, tuple[str, ...] | None]:
    """Each changed model's dirty output columns, or None where it cannot be confined."""
    out: dict[str, tuple[str, ...] | None] = {}
    for name in sorted(names):
        before_model = before.models.get(name)
        after_model = after.models.get(name)
        before_sql = before_model.analysable_sql if before_model else None
        after_sql = after_model.analysable_sql if after_model else None
        if before_sql is None or after_sql is None:
            out[name] = None
            continue
        out[name] = changed_outputs(before_sql, after_sql, dialect=dialect)
    return out
