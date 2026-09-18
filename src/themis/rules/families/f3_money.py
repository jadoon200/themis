"""F3 — money: precision, types and rounding.

Binary floating point is not a valid representation for money. ``DOUBLE`` cannot
represent 0.01 exactly, so summing a ledger column drifts by fractions of a cent per
row — invisible on one row, a reconciliation break across millions. Trino will not warn
about it, and no test in a project without tests will catch it.

Reducing a DECIMAL's scale is the same failure with a different mechanism: the
arithmetic stays exact, but the value gets truncated on the way in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql
from themis.models import Confidence, Evidence, Finding, Severity
from themis.rules.base import Rule, RuleContext
from themis.vocabulary import Vocabulary

FAMILY = "F3"

# Binary floating point types. Exact-decimal types are fine.
# Trino's REAL parses to FLOAT, so the two are covered by one entry.
_INEXACT_TYPES = {
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT,
    exp.DataType.Type.UDOUBLE,
}


def _cast_context_name(cast: exp.Cast) -> str | None:
    """A name for what is being cast, for the reviewer to recognise.

    Prefers the alias the result is given, then any column inside the expression —
    a cast of an arithmetic expression has no single column name of its own.
    """
    parent = cast.parent
    if isinstance(parent, exp.Alias):
        return parent.alias
    inner = cast.this
    if isinstance(inner, exp.Column):
        return inner.name
    columns = [c.name for c in cast.find_all(exp.Column)]
    return columns[0] if columns else None


def _casts(sql: str, dialect: str) -> list[tuple[exp.Cast, str]]:
    """Every cast in a statement, paired with the name it applies to."""
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return []
    found: list[tuple[exp.Cast, str]] = []
    for cast in tree.find_all(exp.Cast):
        name = _cast_context_name(cast)
        if name:
            found.append((cast, name))
    return found


def _decimal_scale(data_type: exp.DataType) -> int | None:
    """The scale of a DECIMAL type, or None if it is not a decimal."""
    if data_type.this is not exp.DataType.Type.DECIMAL:
        return None
    params = data_type.expressions
    if len(params) < 2:
        return 0
    try:
        return int(params[1].name)
    except (ValueError, AttributeError):
        return None


@dataclass
class MoneyAsFloatRule(Rule):
    """A monetary value is cast to DOUBLE, FLOAT or REAL rather than DECIMAL.

    The canonical silent financial bug: nothing errors, every individual row looks
    right, and the total is wrong by an amount that grows with the row count.
    """

    rule_id: str = field(init=False, default="F3001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.CRITICAL)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []

        before_inexact = set()
        if ctx.before is not None and ctx.before.analysable_sql is not None:
            before_inexact = {
                name
                for cast, name in _casts(ctx.before.analysable_sql, ctx.dialect)
                if cast.to.this in _INEXACT_TYPES
            }

        findings: list[Finding] = []
        seen: set[str] = set()
        for cast, name in _casts(ctx.after.analysable_sql, ctx.dialect):
            if cast.to.this not in _INEXACT_TYPES or not ctx.vocabulary.is_monetary(name):
                continue
            if name in before_inexact or name in seen:
                continue  # pre-existing, or already reported for this model
            seen.add(name)
            findings.append(self._finding(ctx, cast, name))
        return findings

    def _finding(self, ctx: RuleContext, cast: exp.Cast, name: str) -> Finding:
        assert ctx.after is not None
        type_name = str(cast.to.this.name)
        via = f" via the `{ctx.via_macro}` macro" if ctx.via_macro else ""
        return Finding(
            rule_id=self.rule_id,
            family=self.family,
            title=f"Monetary value `{name}` cast to {type_name}",
            severity=Severity.CRITICAL
            if ctx.is_governed or ctx.reaches_exposure
            else self.severity,
            confidence=Confidence.PROVEN,
            evidence=Evidence(
                model_name=ctx.model_name,
                file_path=ctx.after.file_path,
                sql_after=cast.sql(dialect=ctx.dialect),
                note=f"cast to {type_name}{via}; monetary values need an exact decimal type",
            ),
            consequence=(
                f"{type_name} is binary floating point and cannot represent values like "
                "0.01 exactly. Every row carries a small representation error, and "
                "summing the column accumulates it — a drift that is invisible per row "
                "and material across a ledger. Nothing errors; the total is simply "
                "slightly wrong, and differently wrong each time the rows change."
            ),
            suggestion=(
                f"Cast `{name}` to `decimal(38, 6)` (or the project's standard money "
                "type) instead."
                + (
                    f" This is set in the `{ctx.via_macro}` macro, so the change affects "
                    "every model that uses it."
                    if ctx.via_macro
                    else ""
                )
            ),
            blast_radius=ctx.blast_radius,
        )


@dataclass
class DecimalScaleReducedRule(Rule):
    """A monetary DECIMAL's scale was reduced, truncating the values it stores."""

    rule_id: str = field(init=False, default="F3002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        before_scales: dict[str, int] = {}
        for cast, name in _casts(before_sql, ctx.dialect):
            scale = _decimal_scale(cast.to)
            if scale is not None:
                before_scales[name] = max(scale, before_scales.get(name, 0))

        findings: list[Finding] = []
        seen: set[str] = set()
        for cast, name in _casts(after_sql, ctx.dialect):
            scale = _decimal_scale(cast.to)
            previous = before_scales.get(name)
            if scale is None or previous is None or scale >= previous or name in seen:
                continue
            if not ctx.vocabulary.is_monetary(name):
                continue
            seen.add(name)
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title=f"Decimal scale of `{name}` reduced from {previous} to {scale}",
                    severity=self.severity,
                    confidence=Confidence.PROVEN,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        sql_after=cast.sql(dialect=ctx.dialect),
                        note=f"decimal scale {previous} -> {scale}",
                    ),
                    consequence=(
                        f"Values are now truncated to {scale} decimal place(s). Any "
                        f"precision beyond that is lost on write, so figures that "
                        "previously reconciled will no longer tie back, and the "
                        "difference cannot be recovered from the stored data."
                    ),
                    suggestion=(
                        f"Keep the scale at {previous} unless the loss is deliberate "
                        "and the downstream reconciliation tolerance allows it."
                    ),
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings


def _sign_assigning_cases(sql: str, dialect: str, vocab: Vocabulary) -> dict[str, str]:
    """CASE expressions that assign opposite signs to their branches.

    A ledger encodes debit and credit as a sign, and that encoding is almost always a
    CASE: one branch positive, the other negated. Flipping which side is which inverts
    every amount in the model, and the diff is a single changed string literal.

    Keyed by the branch outcomes so a changed *condition* is visible while a reordered
    or reformatted CASE is not.
    """
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return {}

    found: dict[str, str] = {}
    for case in tree.find_all(exp.Case):
        rendered = case.sql(dialect=dialect)
        if not _has_opposing_signs(case):
            continue
        columns = [c.name for c in case.find_all(exp.Column)]
        if not any(vocab.is_monetary(name) for name in columns):
            continue
        # The key is the branch shape; the value is the condition. Same shape with a
        # different condition means the sign convention moved.
        shape = "|".join(
            branch.args.get("true").sql(dialect=dialect)
            for branch in case.args.get("ifs") or []
            if branch.args.get("true") is not None
        )
        conditions = "|".join(
            branch.this.sql(dialect=dialect)
            for branch in case.args.get("ifs") or []
            if branch.this is not None
        )
        found[shape or rendered] = conditions
    return found


def _is_minus_one(node: exp.Expression) -> bool:
    """Whether a node is the literal -1, however the dialect spelled it."""
    if isinstance(node, exp.Neg):
        inner = node.this
        return isinstance(inner, exp.Literal) and inner.name == "1"
    return isinstance(node, exp.Literal) and node.name in ("-1", "-1.0")


def _split_sign(node: exp.Expression) -> tuple[bool, str]:
    """Separate a numeric expression into (is negated, the expression without its sign).

    The negation is rarely the outermost node. ``-1 * amount / 100`` parses as a
    division whose numerator carries the sign, so a check that only looks at the top
    node concludes the branches are not opposing and the rule never fires — which is
    exactly what happened on the real compiled SQL.
    """
    if isinstance(node, exp.Neg):
        return True, node.this.sql(dialect="trino")

    if isinstance(node, exp.Mul):
        for side, other in ((node.this, node.expression), (node.expression, node.this)):
            if _is_minus_one(side):
                return True, other.sql(dialect="trino")

    # Descend the left spine of an arithmetic chain, where the sign lives.
    if isinstance(node, exp.Div | exp.Mul) and node.this is not None:
        negated, inner = _split_sign(node.this)
        if negated:
            rebuilt = node.copy()
            rebuilt.set("this", exp.maybe_parse(inner, dialect="trino"))
            return True, rebuilt.sql(dialect="trino")

    return False, node.sql(dialect="trino")


def _has_opposing_signs(case: exp.Case) -> bool:
    """Whether a CASE returns the same quantity with opposite signs across branches.

    Requiring the *same* underlying expression is what separates a debit/credit sign
    convention from a CASE that merely happens to contain a negative number.
    """
    outcomes: list[exp.Expression] = []
    for branch in case.args.get("ifs") or []:
        value = branch.args.get("true")
        if value is not None:
            outcomes.append(value)
    default = case.args.get("default")
    if default is not None:
        outcomes.append(default)
    if len(outcomes) < 2:
        return False

    split = [_split_sign(o) for o in outcomes]
    bodies = {body for _, body in split}
    flags = {negated for negated, _ in split}
    return len(bodies) == 1 and flags == {True, False}


@dataclass
class SignConventionChangedRule(Rule):
    """The condition deciding a monetary sign changed — every amount inverts."""

    rule_id: str = field(init=False, default="F3003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.CRITICAL)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        before = _sign_assigning_cases(before_sql, ctx.dialect, ctx.vocabulary)
        after = _sign_assigning_cases(after_sql, ctx.dialect, ctx.vocabulary)

        findings: list[Finding] = []
        for shape, condition in sorted(after.items()):
            previous = before.get(shape)
            if previous is None or previous == condition:
                continue
            via = f" via the `{ctx.via_macro}` macro" if ctx.via_macro else ""
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title="Sign convention changed on a monetary expression",
                    severity=self.severity,
                    confidence=Confidence.PROVEN,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        sql_after=condition[:300],
                        note=f"branch condition {previous} -> {condition}{via}",
                    ),
                    consequence=(
                        "The condition deciding which branch is negated has changed, "
                        "so amounts that were positive are now negative and vice "
                        "versa. Every total built on this column inverts, and a "
                        "reversed figure of the right magnitude is far harder to spot "
                        "than a missing one."
                    ),
                    suggestion=(
                        "Confirm the debit/credit convention against the source "
                        "ledger, and check any downstream model that nets or "
                        "reconciles this column."
                    ),
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings


def _currency_grouped(select: exp.Select, vocabulary: Vocabulary) -> bool:
    """Whether this select's own GROUP BY carries the currency."""
    group = select.args.get("group")
    if group is None:
        return False
    for expression in group.expressions:
        if any(
            vocabulary.is_currency_column(column.name) for column in expression.find_all(exp.Column)
        ):
            return True
        if isinstance(expression, exp.Literal):
            # `group by 3` — positional. Resolve it against the projection.
            try:
                position = int(expression.name) - 1
            except ValueError:
                continue
            if 0 <= position < len(select.expressions):
                projected = select.expressions[position]
                if any(
                    vocabulary.is_currency_column(column.name)
                    for column in projected.find_all(exp.Column)
                ):
                    return True
    return False


def _currency_pinned(select: exp.Select, vocabulary: Vocabulary) -> bool:
    """Whether this select restricts itself to a single currency.

    `where currency_code = 'USD'` makes a sum across rows perfectly sound, and a rule that
    flagged it would be flagging the correct way to write the thing it asks for.
    """
    where = select.args.get("where")
    if where is None:
        return False
    for equality in where.find_all(exp.EQ):
        left, right = equality.left, equality.right
        for column, other in ((left, right), (right, left)):
            if (
                isinstance(column, exp.Column)
                and vocabulary.is_currency_column(column.name)
                and isinstance(other, exp.Literal)
            ):
                return True
    return False


def _mixed_currency_sums(sql: str, dialect: str, vocabulary: Vocabulary) -> dict[str, str]:
    """Aggregates over a transaction-currency amount with no currency in the grain.

    Returns the alias (or column) of each, with the column it aggregates.
    """
    try:
        parsed = parse_sql(sql, dialect=dialect)
    except ParseError:
        return {}
    found: dict[str, str] = {}
    for select in parsed.find_all(exp.Select):
        # Read the select's own args. A subtree search proves the wrong scope: the GROUP BY
        # of an inner CTE says nothing about an aggregate in an outer one.
        if _currency_grouped(select, vocabulary) or _currency_pinned(select, vocabulary):
            continue
        for projection in select.expressions:
            for aggregate in projection.find_all(exp.Sum):
                for column in aggregate.find_all(exp.Column):
                    if vocabulary.is_transaction_currency_amount(column.name):
                        name = projection.alias_or_name or column.name
                        found[name] = column.name
    return found


@dataclass
class MixedCurrencyTotalRule(Rule):
    """An amount in the row's own currency, summed without the currency in the grain.

    The total that results has no unit. Ten euros and ten dollars make twenty of nothing,
    and nothing about the number looks wrong: it is the right magnitude, it is positive, it
    has two decimal places, and it reconciles to nothing at all. No test in a project
    without tests catches it, and execution cannot either — the row count holds and the
    total moves only in the way summing more rows always moves a total.

    Named, not typed, because dbt projects rarely declare types and never declare units.
    A reporting-currency name always wins, so `revenue_usd` — monetary, denominated, and
    perfectly summable — is never called suspect. A project that spells its own columns
    differently sets `THEMIS_TRANSACTION_CURRENCY_HINTS`.
    """

    rule_id: str = field(init=False, default="F3004")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        after_sql = ctx.after.analysable_sql if ctx.after else None
        if after_sql is None:
            return []
        mixed = _mixed_currency_sums(after_sql, ctx.dialect, ctx.vocabulary)
        if not mixed:
            return []
        # Diff-aware, like the rest of this family: a model that has always summed this way
        # is a fact about the project, and reporting it on an unrelated edit is how a
        # reviewer learns to skip the family.
        before_sql = ctx.before.analysable_sql if ctx.before else None
        if before_sql is not None:
            already = _mixed_currency_sums(before_sql, ctx.dialect, ctx.vocabulary)
            mixed = {name: column for name, column in mixed.items() if name not in already}
        if not mixed:
            return []

        listed = ", ".join(f"{name} = sum({column})" for name, column in sorted(mixed.items()))
        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="An amount in its own currency is summed across currencies",
                severity=self.severity,
                # The names are a heuristic; whether these rows really span currencies is
                # a question about the data, which this rule cannot answer alone.
                confidence=Confidence.LIKELY,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path if ctx.after else None,
                    note=f"{listed} — no currency column in the GROUP BY, and no single "
                    "currency in the WHERE",
                ),
                consequence=(
                    "Amounts denominated in different currencies are added together, so "
                    "the total has no unit. It will look entirely reasonable — right "
                    "magnitude, right sign, two decimal places — and reconcile to nothing."
                ),
                suggestion=(
                    "Group by the currency as well, restrict the model to one currency, or "
                    "sum the converted amount instead."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


RULES: tuple[Rule, ...] = (
    MoneyAsFloatRule(),
    DecimalScaleReducedRule(),
    SignConventionChangedRule(),
    MixedCurrencyTotalRule(),
)
