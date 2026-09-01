"""SQL parsing, normalisation, and semantic diffing.

Always parses with the Trino dialect — Starburst is Trino — regardless of what engine
executes the project. The demo project runs on DuckDB purely so results can be
compared cheaply; nothing here ever executes SQL.

The important property is that reformatting must not produce findings. A reviewer who
gets flagged for whitespace stops reading the flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.diff import Insert, Keep, Move, Remove, Update

from themis.logging import get_logger

log = get_logger(__name__)

DIALECT = "trino"


class ParseError(RuntimeError):
    """SQL that sqlglot could not parse in the configured dialect."""


def parse_sql(sql: str, *, dialect: str = DIALECT) -> exp.Expression:
    """Parse one statement, raising rather than returning a partial tree.

    sqlglot v30 widened ``parse_one`` to return ``Expr``, the base of ``Expression``.
    Narrowing here is a real check rather than a cast: everything downstream walks the
    tree assuming node semantics, and a bare ``Expr`` would fail obscurely later.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as exc:  # sqlglot raises several unrelated types
        raise ParseError(f"could not parse SQL as {dialect}: {exc}") from exc
    if not isinstance(tree, exp.Expression):
        raise ParseError("SQL parsed to an empty or non-expression tree")
    return tree


def normalise(tree: exp.Expression) -> exp.Expression:
    """Strip the differences a reviewer does not care about.

    Comments and formatting carry no semantics, so removing them means the diff that
    follows reports behaviour changes only. Identifier casing is left alone: Trino is
    case-sensitive for quoted identifiers and silently folding them would be wrong.
    """
    cleaned = tree.copy()
    for node in cleaned.walk():
        if node.comments:
            node.comments = None
    return cleaned


@dataclass
class SemanticDiff:
    """The behavioural difference between two versions of a model.

    ``is_noop`` is the load-bearing property: it is what lets a 300-line reformat pass
    silently while a single LEFT-to-INNER flip inside it surfaces.
    """

    inserted: list[exp.Expr] = field(default_factory=list)
    removed: list[exp.Expr] = field(default_factory=list)
    updated: list[tuple[exp.Expr, exp.Expr]] = field(default_factory=list)
    moved: list[exp.Expr] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        """True when nothing semantically changed — a pure reformat or rename."""
        return not (self.inserted or self.removed or self.updated)

    @property
    def change_count(self) -> int:
        return len(self.inserted) + len(self.removed) + len(self.updated)

    def nodes_of_type(self, *types: type[exp.Expression]) -> list[exp.Expression]:
        """Every changed node matching a type, for rules that care about one construct."""
        out: list[exp.Expression] = []
        for node in (*self.inserted, *self.removed, *self.moved):
            if isinstance(node, types):
                out.append(node)
        for before, after in self.updated:
            for node in (before, after):
                if isinstance(node, types):
                    out.append(node)
        return out


def semantic_diff(before_sql: str, after_sql: str, *, dialect: str = DIALECT) -> SemanticDiff:
    """Diff two SQL statements structurally rather than textually."""
    before = normalise(parse_sql(before_sql, dialect=dialect))
    after = normalise(parse_sql(after_sql, dialect=dialect))

    result = SemanticDiff()
    for edit in sqlglot.diff(before, after):
        if isinstance(edit, Keep):
            continue
        if isinstance(edit, Insert):
            result.inserted.append(edit.expression)
        elif isinstance(edit, Remove):
            result.removed.append(edit.expression)
        elif isinstance(edit, Update):
            result.updated.append((edit.source, edit.target))
        elif isinstance(edit, Move):
            result.moved.append(edit.source)
    return result


def find_joins(tree: exp.Expression) -> list[exp.Join]:
    """Every join in a statement, CTEs included."""
    return list(tree.find_all(exp.Join))


def join_kind(join: exp.Join) -> str:
    """Normalise a join's type to a comparable string.

    sqlglot represents an unqualified JOIN with empty side and kind, which in Trino
    means INNER — spelling that out here keeps the comparison rules simple.
    """
    side = (join.side or "").upper()
    kind = (join.kind or "").upper()
    if not side and not kind:
        return "INNER"
    return f"{side} {kind}".strip()


def select_from(select: exp.Select) -> exp.From | None:
    """The FROM of a select, in this select's own scope.

    sqlglot renamed this arg from ``from`` to ``from_`` in v30. Reading both keeps
    THEMIS working across versions, and doing it here means the rest of the codebase
    never has to know. Using the arg rather than ``find(exp.From)`` matters too:
    traversal would descend into CTEs and return the wrong scope's FROM.
    """
    source = select.args.get("from_") or select.args.get("from")
    return source if isinstance(source, exp.From) else None


def select_joins(select: exp.Select) -> list[exp.Join]:
    """Joins belonging to this select only, not to its CTEs or subqueries."""
    joins = select.args.get("joins") or []
    return [j for j in joins if isinstance(j, exp.Join)]


def find_ctes(tree: exp.Expression) -> dict[str, exp.Expression]:
    """CTE name to its body, for tracing where a column actually comes from."""
    return {cte.alias_or_name: cte.this for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
