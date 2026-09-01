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

FAMILY = "F3"

# Substrings that mark a column as monetary. Deliberately broad: a false positive here
# costs a reviewer one glance, a false negative costs a restatement.
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
    "rate",
    "usd",
    "eur",
    "gbp",
    "sgd",
)

# Binary floating point types. Exact-decimal types are fine.
# Trino's REAL parses to FLOAT, so the two are covered by one entry.
_INEXACT_TYPES = {
    exp.DataType.Type.DOUBLE,
    exp.DataType.Type.FLOAT,
    exp.DataType.Type.UDOUBLE,
}


def _is_monetary(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _MONEY_HINTS)


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
            if cast.to.this not in _INEXACT_TYPES or not _is_monetary(name):
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
            if not _is_monetary(name):
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


RULES: tuple[Rule, ...] = (MoneyAsFloatRule(), DecimalScaleReducedRule())
