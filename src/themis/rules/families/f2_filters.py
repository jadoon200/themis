"""F2 — filters and NULL semantics.

A predicate is the cheapest possible way to change what a number means. Adding one
line to a WHERE clause can remove a whole class of rows from a total, and the diff
looks like a one-line addition rather than a restatement.

NULL semantics make it worse: ``NOT IN`` against a nullable subquery returns nothing at
all, and ``<>`` silently drops NULL rows. Neither errors, and both look like ordinary
SQL.

Predicates are compared across the whole statement, CTEs included. dbt models filter
inside CTEs far more often than in the final SELECT, and a rule that reads only the
outer WHERE would never fire on a real project.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql
from themis.models import Confidence, Evidence, Finding, Severity
from themis.rules.base import Rule, RuleContext

FAMILY = "F2"


def _predicates(sql: str, dialect: str) -> dict[str, str]:
    """Every top-level conjunct of every WHERE, keyed by a normalised form.

    Splitting on AND matters: a WHERE gaining one condition should read as one added
    predicate, not as a wholesale replacement of the clause.
    """
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return {}

    found: dict[str, str] = {}
    for where in tree.find_all(exp.Where):
        condition = where.this
        if condition is None:
            continue
        for conjunct in _split_conjuncts(condition):
            rendered = conjunct.sql(dialect=dialect)
            # Normalise whitespace so reformatting is not a change.
            found[" ".join(rendered.split())] = rendered
    return found


def _split_conjuncts(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _split_conjuncts(node.this) + _split_conjuncts(node.expression)
    if isinstance(node, exp.Paren) and node.this is not None:
        return _split_conjuncts(node.this)
    return [node]


def _severity_for(ctx: RuleContext, base: Severity) -> Severity:
    if ctx.is_governed or ctx.reaches_exposure:
        return Severity.CRITICAL if base is Severity.HIGH else Severity.HIGH
    return base


@dataclass
class FilterChangedRule(Rule):
    """A WHERE predicate was added or removed, changing which rows are counted."""

    rule_id: str = field(init=False, default="F2001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        before = _predicates(before_sql, ctx.dialect)
        after = _predicates(after_sql, ctx.dialect)

        added = [after[k] for k in after.keys() - before.keys()]
        removed = [before[k] for k in before.keys() - after.keys()]
        if not added and not removed:
            return []

        findings: list[Finding] = []
        for predicate in sorted(added):
            findings.append(self._finding(ctx, predicate, added=True))
        for predicate in sorted(removed):
            findings.append(self._finding(ctx, predicate, added=False))
        return findings

    def _finding(self, ctx: RuleContext, predicate: str, *, added: bool) -> Finding:
        assert ctx.after is not None
        verb = "added" if added else "removed"
        return Finding(
            rule_id=self.rule_id,
            family=self.family,
            title=f"Filter {verb}: {predicate[:80]}",
            severity=_severity_for(ctx, self.severity),
            confidence=Confidence.PROVEN,
            evidence=Evidence(
                model_name=ctx.model_name,
                file_path=ctx.after.file_path,
                sql_after=predicate,
                note=f"WHERE conjunct {verb}",
            ),
            consequence=(
                (
                    "Rows that previously qualified are now excluded, so every total "
                    "computed downstream falls by whatever those rows contributed."
                    if added
                    else "Rows that were previously excluded now qualify, so every "
                    "total computed downstream rises by whatever they contribute."
                )
                + " Nothing errors and the output is still well-formed; only the "
                "figure changes."
            ),
            suggestion=(
                "Confirm the change in scope is intended, and that any reconciliation "
                "or prior-period comparison downstream expects the new population."
            ),
            blast_radius=ctx.blast_radius,
        )


@dataclass
class NotInNullableRule(Rule):
    """``NOT IN`` against a subquery — returns nothing at all if it yields a NULL.

    A three-valued-logic trap: ``x NOT IN (1, 2, NULL)`` is never true, so the result
    is silently empty rather than wrong-looking.
    """

    rule_id: str = field(init=False, default="F2002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []
        try:
            tree = parse_sql(ctx.after.analysable_sql, dialect=ctx.dialect)
        except ParseError:
            return []

        before_sql = ctx.before.analysable_sql if ctx.before else None
        existing = set()
        if before_sql:
            try:
                before_tree = parse_sql(before_sql, dialect=ctx.dialect)
                existing = {n.sql(dialect=ctx.dialect) for n in _not_in_subqueries(before_tree)}
            except ParseError:
                existing = set()

        findings: list[Finding] = []
        for node in _not_in_subqueries(tree):
            rendered = node.sql(dialect=ctx.dialect)
            if rendered in existing:
                continue
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title="NOT IN against a subquery may return no rows at all",
                    severity=_severity_for(ctx, self.severity),
                    confidence=Confidence.LIKELY,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        sql_after=rendered[:400],
                        note="NOT IN with a subquery operand",
                    ),
                    consequence=(
                        "If the subquery returns even one NULL, the NOT IN is never "
                        "true and this filter removes every row. The model does not "
                        "fail — it produces an empty result, or drops a whole "
                        "population from a total, with nothing to indicate it."
                    ),
                    suggestion=("Use NOT EXISTS, or add an IS NOT NULL guard inside the subquery."),
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings


def _not_in_subqueries(tree: exp.Expression) -> list[exp.Expression]:
    """``NOT IN (subquery)`` nodes — the dangerous form.

    A NOT IN over an inline literal list is fine unless a NULL is written into it, so
    only the subquery form is flagged; flagging both would bury the real case.
    """
    found: list[exp.Expression] = []
    for node in tree.find_all(exp.Not):
        inner = node.this
        if not isinstance(inner, exp.In):
            continue
        if inner.args.get("query") is not None or any(
            isinstance(e, exp.Select | exp.Subquery) for e in inner.expressions
        ):
            found.append(node)
    return found


RULES: tuple[Rule, ...] = (FilterChangedRule(), NotInNullableRule())
