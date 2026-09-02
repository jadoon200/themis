"""F8 — Trino and Starburst engine behaviour.

Cost rather than correctness, with one exception. Trino is a federated engine: a join
across two catalogs cannot be pushed down, so both sides are pulled to the coordinator
and joined in memory. That turns a query that reads like any other into one that moves
the whole table across the network — and the SQL gives no hint.

The exception is the cartesian join, which is a correctness problem wearing a
performance costume.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.parse import (
    ParseError,
    find_joins,
    parse_sql,
    resolve_relation,
    select_from,
)
from themis.models import Confidence, Evidence, Finding, Severity
from themis.rules.base import Rule, RuleContext

FAMILY = "F8"


def _catalog_of(table: exp.Table) -> str | None:
    """The catalog part of a three-part name, if written."""
    catalog = table.args.get("catalog")
    if isinstance(catalog, exp.Identifier):
        return catalog.name
    return str(catalog) if catalog else None


@dataclass
class CrossCatalogJoinRule(Rule):
    """A join spanning two Trino catalogs — no pushdown, everything goes to the coordinator."""

    rule_id: str = field(init=False, default="F8001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.MEDIUM)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []
        try:
            tree = parse_sql(ctx.after.analysable_sql, dialect=ctx.dialect)
        except ParseError:
            return []

        catalogs: set[str] = set()
        for table in tree.find_all(exp.Table):
            catalog = _catalog_of(table)
            if catalog:
                catalogs.add(catalog)
        if len(catalogs) < 2 or not find_joins(tree):
            return []

        before_catalogs: set[str] = set()
        if ctx.before is not None and ctx.before.analysable_sql is not None:
            try:
                before_tree = parse_sql(ctx.before.analysable_sql, dialect=ctx.dialect)
                before_catalogs = {
                    c for t in before_tree.find_all(exp.Table) if (c := _catalog_of(t))
                }
            except ParseError:
                before_catalogs = set()
        if len(before_catalogs) >= 2:
            return []  # already federated before this change

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=f"Join now spans {len(catalogs)} catalogs: {', '.join(sorted(catalogs))}",
                severity=self.severity,
                confidence=Confidence.LIKELY,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"catalogs referenced: {', '.join(sorted(catalogs))}",
                ),
                consequence=(
                    "Trino cannot push a join down across catalogs. Both sides are "
                    "read in full and joined on the coordinator, so this moves the "
                    "data over the network rather than filtering it at the source. "
                    "The query is correct and can be orders of magnitude slower."
                ),
                suggestion=(
                    "Land the smaller side into the same catalog first, or filter "
                    "aggressively before the join so less crosses the boundary."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class CartesianJoinRule(Rule):
    """A join with no ON condition, or one that is always true.

    Filed under cost, but it is a correctness bug: every row on the left is paired
    with every row on the right, so any total is multiplied by the size of the other
    side.
    """

    rule_id: str = field(init=False, default="F8002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.CRITICAL)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []
        try:
            tree = parse_sql(ctx.after.analysable_sql, dialect=ctx.dialect)
        except ParseError:
            return []

        before_bad: set[str] = set()
        if ctx.before is not None and ctx.before.analysable_sql is not None:
            try:
                before_tree = parse_sql(ctx.before.analysable_sql, dialect=ctx.dialect)
                before_bad = {
                    j.sql(dialect=ctx.dialect) for j in find_joins(before_tree) if _is_cartesian(j)
                }
            except ParseError:
                before_bad = set()

        findings: list[Finding] = []
        for join in find_joins(tree):
            if not _is_cartesian(join):
                continue
            rendered = join.sql(dialect=ctx.dialect)
            if rendered in before_bad:
                continue
            relation = (
                resolve_relation(tree, join.this.name if join.this else "") or "the joined table"
            )
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title=f"Join to {relation} has no effective condition",
                    severity=self.severity,
                    confidence=Confidence.PROVEN,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        sql_after=rendered[:300],
                        note="join has no ON clause, or one that is always true",
                    ),
                    consequence=(
                        "Every row on one side pairs with every row on the other. Row "
                        "count becomes the product of the two, and any amount summed "
                        "afterwards is multiplied by the size of the opposite side."
                    ),
                    suggestion="Add the join key, or use CROSS JOIN explicitly if it is intended.",
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings


def _is_cartesian(join: exp.Join) -> bool:
    """No ON condition, or one that cannot filter anything.

    A CROSS JOIN written deliberately is excluded: the author said what they meant.
    """
    if (join.side or "").upper() == "CROSS" or (join.kind or "").upper() == "CROSS":
        return False
    condition = join.args.get("on")
    if condition is None:
        return join.args.get("using") is None
    if isinstance(condition, exp.Boolean) and condition.this is True:
        return True
    if isinstance(condition, exp.EQ):
        left, right = condition.this, condition.expression
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return left.name == right.name
    return False


@dataclass
class PartitionPruningLostRule(Rule):
    """A filter on a date column wrapped in a function, defeating partition pruning."""

    rule_id: str = field(init=False, default="F8003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.LOW)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        after_sql = ctx.after.analysable_sql
        before_sql = ctx.before.analysable_sql
        if after_sql is None or before_sql is None:
            return []

        after_wrapped = _wrapped_filter_columns(after_sql, ctx.dialect)
        before_wrapped = _wrapped_filter_columns(before_sql, ctx.dialect)
        introduced = after_wrapped - before_wrapped
        if not introduced:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=f"Filter wraps `{', '.join(sorted(introduced))}` in a function",
                severity=self.severity,
                confidence=Confidence.POSSIBLE,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"columns wrapped in a predicate: {', '.join(sorted(introduced))}",
                ),
                consequence=(
                    "A predicate that applies a function to a column cannot be used to "
                    "prune partitions, so the engine reads every partition and filters "
                    "afterwards. Results are correct; the scan is not."
                ),
                suggestion=(
                    "Rewrite so the bare column is compared against a computed bound — "
                    "`d >= date '2026-01-01'` rather than `date_trunc('month', d) = ...`."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


def _wrapped_filter_columns(sql: str, dialect: str) -> set[str]:
    """Date-ish columns compared while wrapped in a function call."""
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return set()

    wrapped: set[str] = set()
    for where in tree.find_all(exp.Where):
        for comparison in where.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
            operand = comparison.this
            if isinstance(operand, exp.Column) or operand is None:
                continue
            for column in operand.find_all(exp.Column):
                name = column.name.lower()
                if any(hint in name for hint in ("date", "day", "month", "ts", "time", "period")):
                    wrapped.add(column.name)
    return wrapped


@dataclass
class UnorderedLimitRule(Rule):
    """``LIMIT`` without ``ORDER BY`` — which rows you get is undefined."""

    rule_id: str = field(init=False, default="F8004")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.MEDIUM)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []
        try:
            tree = parse_sql(ctx.after.analysable_sql, dialect=ctx.dialect)
        except ParseError:
            return []

        select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
        if not isinstance(select, exp.Select):
            return []
        if select.args.get("limit") is None or select.args.get("order") is not None:
            return []
        if select_from(select) is None:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="LIMIT without ORDER BY",
                severity=self.severity,
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note="limit applied with no ordering",
                ),
                consequence=(
                    "Which rows survive is whatever the engine produced first, and "
                    "that can differ between runs on identical data. A model built "
                    "this way is not reproducible."
                ),
                suggestion="Add an ORDER BY, or remove the LIMIT if it was for development only.",
                blast_radius=ctx.blast_radius,
            )
        ]


RULES: tuple[Rule, ...] = (
    CrossCatalogJoinRule(),
    CartesianJoinRule(),
    PartitionPruningLostRule(),
    UnorderedLimitRule(),
)
