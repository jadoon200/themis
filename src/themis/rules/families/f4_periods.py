"""F4 — time, periods, and point-in-time correctness.

Financial data is reported by period, so anything that moves a period boundary moves
money between reporting periods. A month-end booking landing in the wrong month is not
a rounding issue; it is a restatement.

The other half of this family is reproducibility. A model containing ``current_date``
returns different numbers depending on when it ran, which means a figure cannot be
reproduced from the code and the data — and in an audited environment a figure that
cannot be reproduced cannot be defended.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql
from themis.models import Confidence, Evidence, Finding, Severity
from themis.rules.base import Rule, RuleContext

FAMILY = "F4"

# Functions whose value depends on when the query ran.
_NONDETERMINISTIC = (
    exp.CurrentDate,
    exp.CurrentTimestamp,
    exp.CurrentTime,
)

# `date_trunc` lands on different nodes depending on the argument type and dialect —
# Trino parses it to TimestampTrunc. Matching only one silently misses the other, which
# is how a period-shift rule ends up never firing.
_TRUNC_NODES = (exp.DateTrunc, exp.TimestampTrunc)


def _severity_for(ctx: RuleContext, base: Severity) -> Severity:
    if ctx.is_governed or ctx.reaches_exposure:
        return Severity.CRITICAL if base is Severity.HIGH else Severity.HIGH
    return base


def _date_truncations(sql: str, dialect: str) -> dict[str, str]:
    """``date_trunc`` calls, keyed by the column they truncate.

    Keying by target rather than by full text is what makes a *changed* granularity
    visible: 'month' becoming 'year' on the same column is a period shift, whereas a
    new truncation on a different column is an unrelated change.
    """
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return {}

    found: dict[str, str] = {}
    for node in tree.find_all(*_TRUNC_NODES):
        unit = node.args.get("unit")
        target = node.this
        if unit is None or target is None:
            continue
        columns = [c.name for c in target.find_all(exp.Column)]
        key = columns[0] if columns else target.sql(dialect=dialect)
        # Lowercased: sqlglot normalises the unit to upper case, and "from MONTH to
        # YEAR" reads like shouting in a report a human is meant to read.
        raw = unit.name if isinstance(unit, exp.Expression) else str(unit)
        found[key] = raw.lower()
    return found


@dataclass
class PeriodGranularityChangedRule(Rule):
    """A date truncation changed granularity, moving every row's period."""

    rule_id: str = field(init=False, default="F4001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        before = _date_truncations(before_sql, ctx.dialect)
        after = _date_truncations(after_sql, ctx.dialect)

        findings: list[Finding] = []
        for column, unit in sorted(after.items()):
            previous = before.get(column)
            if previous is None or previous.lower() == unit.lower():
                continue
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title=f"Period granularity on `{column}` changed from {previous} to {unit}",
                    severity=_severity_for(ctx, self.severity),
                    confidence=Confidence.PROVEN,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        note=(
                            f"date_trunc('{previous}', {column}) -> date_trunc('{unit}', {column})"
                        ),
                    ),
                    consequence=(
                        f"Every row's period is now derived at {unit} rather than "
                        f"{previous} granularity. Rows move between reporting periods, "
                        "and any as-of join keyed on this column matches a different "
                        "record — so amounts are attributed to the wrong period and "
                        "any rate looked up by it is the wrong rate."
                    ),
                    suggestion=(
                        "Confirm the reporting grain intended here, and check every "
                        "downstream join that keys on this column."
                    ),
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings


@dataclass
class NonDeterministicTimeRule(Rule):
    """``current_date`` and friends introduced — the output stops being reproducible."""

    rule_id: str = field(init=False, default="F4002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []

        after_count = self._count(ctx.after.analysable_sql, ctx.dialect)
        if after_count == 0:
            return []

        before_count = 0
        if ctx.before is not None and ctx.before.analysable_sql is not None:
            before_count = self._count(ctx.before.analysable_sql, ctx.dialect)
        if after_count <= before_count:
            return []  # pre-existing, not introduced by this change

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Non-deterministic time function introduced",
                severity=_severity_for(ctx, self.severity),
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note="current_date / current_timestamp appears in the model",
                ),
                consequence=(
                    "The model's output now depends on when it ran. The same code "
                    "against the same data produces different figures on different "
                    "days, so a published number cannot be reproduced from the code "
                    "and the data — which is what an audit asks for."
                ),
                suggestion=(
                    "Pass the reporting date in as a variable, or read it from a "
                    "date-spine model, so a run is reproducible."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]

    @staticmethod
    def _count(sql: str, dialect: str) -> int:
        try:
            tree = parse_sql(sql, dialect=dialect)
        except ParseError:
            return 0
        return len(list(tree.find_all(*_NONDETERMINISTIC)))


RULES: tuple[Rule, ...] = (PeriodGranularityChangedRule(), NonDeterministicTimeRule())
