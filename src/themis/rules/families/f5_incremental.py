"""F5 — incremental models and materialization.

Incremental models are where a correct-looking change quietly stops loading data. The
guard, the strategy, the key and the lookback window all decide which rows arrive, and
none of them is visible in the output: the table still populates, the job still passes,
and rows are simply missing or duplicated.

dbt-trino offers three strategies with materially different behaviour. ``append`` never
deduplicates. ``delete+insert`` removes the matched window first. ``merge`` needs a key
that is genuinely unique *and* a connector that supports MERGE, which many Trino
connectors do not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from themis.models import Confidence, Evidence, Finding, Severity
from themis.rules.base import Rule, RuleContext

FAMILY = "F5"

# `is_incremental()` survives into compiled SQL only as its expansion, so the guard is
# detected from the raw model source instead.
_IS_INCREMENTAL = re.compile(r"is_incremental\s*\(\s*\)")

# Interval literals in a lookback window: `interval '3' day`, `interval 3 day`.
_INTERVAL = re.compile(r"interval\s+'?(\d+)'?\s*(day|hour|month|week|year)s?", re.IGNORECASE)

_STRATEGY_BEHAVIOUR = {
    "append": "inserts without deduplicating, so re-processing a window duplicates rows",
    "delete+insert": "deletes the matched rows before inserting, so the window must "
    "cover every row that could change",
    "merge": "updates matched rows in place, which requires a genuinely unique key and "
    "a connector that supports MERGE",
}


def _severity_for(ctx: RuleContext, base: Severity) -> Severity:
    if ctx.is_governed or ctx.reaches_exposure:
        return Severity.CRITICAL if base is Severity.HIGH else Severity.HIGH
    return base


@dataclass
class IncrementalGuardRemovedRule(Rule):
    """The ``is_incremental()`` guard disappeared from an incremental model.

    Without it every run processes the full history, which is either a silent cost
    blow-up or — with ``append`` — a duplicate of the entire table.
    """

    rule_id: str = field(init=False, default="F5001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    # Reads raw source: the guard is a Jinja construct, gone by compile time.
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        if ctx.after.materialization != "incremental":
            return []

        before_guards = len(_IS_INCREMENTAL.findall(ctx.before.raw_sql))
        after_guards = len(_IS_INCREMENTAL.findall(ctx.after.raw_sql))
        if after_guards >= before_guards or before_guards == 0:
            return []

        strategy = ctx.after.incremental_strategy or "append"
        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="is_incremental() guard removed from an incremental model",
                severity=_severity_for(ctx, self.severity),
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"{before_guards} guard(s) -> {after_guards}",
                ),
                consequence=(
                    "Every run now reads the full history rather than the incremental "
                    f"window. With the `{strategy}` strategy that "
                    f"{_STRATEGY_BEHAVIOUR.get(strategy, 'changes what is written')}. "
                    "The build still succeeds; the table is simply wrong."
                ),
                suggestion=(
                    "Restore the `{% if is_incremental() %}` block, or make the model "
                    "a table if a full rebuild is genuinely intended."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class IncrementalStrategyChangedRule(Rule):
    """The incremental strategy changed — different duplication behaviour."""

    rule_id: str = field(init=False, default="F5002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before = ctx.before.incremental_strategy or (
            "append" if ctx.before.materialization == "incremental" else None
        )
        after = ctx.after.incremental_strategy or (
            "append" if ctx.after.materialization == "incremental" else None
        )
        if before is None or after is None or before == after:
            return []

        extra = ""
        if after == "merge":
            key = ", ".join(ctx.after.unique_key) or "no unique_key is configured"
            extra = (
                f" `merge` requires a genuinely unique key ({key}) and a connector "
                "that supports MERGE; several Trino connectors do not."
            )

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=f"Incremental strategy changed from `{before}` to `{after}`",
                severity=_severity_for(ctx, self.severity),
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"incremental_strategy {before} -> {after}",
                ),
                consequence=(
                    f"`{before}` {_STRATEGY_BEHAVIOUR.get(before, 'behaves differently')}; "
                    f"`{after}` {_STRATEGY_BEHAVIOUR.get(after, 'behaves differently')}."
                    + extra
                    + " Existing rows are affected differently from the day this ships, "
                    "and the table's history will not match its future."
                ),
                suggestion=(
                    "Confirm the new strategy against the model's key, and consider a "
                    "full refresh so history and future rows are built the same way."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class IncrementalKeyChangedRule(Rule):
    """``unique_key`` changed, so a different set of rows is treated as the same row."""

    rule_id: str = field(init=False, default="F5003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before, after = ctx.before.unique_key, ctx.after.unique_key
        if before == after or (not before and not after):
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Incremental unique_key changed",
                severity=_severity_for(ctx, self.severity),
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=(
                        f"unique_key ({', '.join(before) or 'none'}) -> "
                        f"({', '.join(after) or 'none'})"
                    ),
                ),
                consequence=(
                    "Rows already in the table were matched on the old key. Under the "
                    "new one, rows that used to be updated in place will be inserted "
                    "alongside their predecessors — or rows that were distinct will "
                    "start overwriting each other. Existing data is not corrected "
                    "retrospectively, so the table holds both conventions at once."
                ),
                suggestion=(
                    "A full refresh is usually required after a key change, otherwise "
                    "history keeps the old semantics."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class MaterializationChangedRule(Rule):
    """The materialization changed — cost, freshness and refresh behaviour with it."""

    rule_id: str = field(init=False, default="F5004")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.MEDIUM)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before, after = ctx.before.materialization, ctx.after.materialization
        if before == after:
            return []

        severity = self.severity
        note = ""
        if before == "incremental":
            severity = Severity.HIGH
            note = (
                " Moving off `incremental` means every run rebuilds the model in full: "
                "the incremental window and its lookback stop applying, and the cost "
                "profile changes from a slice to the whole history."
            )
        elif after == "incremental":
            severity = Severity.HIGH
            note = (
                " Moving to `incremental` means the model stops rebuilding from source "
                "each run, so any correction to historical rows will no longer be "
                "picked up unless it falls inside the incremental window."
            )
        elif after == "view":
            note = (
                " A view is recomputed on every read, so downstream query cost rises "
                "and results can change between two reads within the same run."
            )

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=f"Materialization changed from `{before}` to `{after}`",
                severity=_severity_for(ctx, severity),
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"materialized {before} -> {after}",
                ),
                consequence=f"How and when this model is built has changed.{note}",
                suggestion=(
                    "Confirm the cost and freshness implications with whoever owns the schedule."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class LookbackWindowNarrowedRule(Rule):
    """The late-arriving-data window shrank, so late rows are silently never loaded."""

    rule_id: str = field(init=False, default="F5005")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        if ctx.after.materialization != "incremental":
            return []

        before = _largest_interval(ctx.before.raw_sql)
        after = _largest_interval(ctx.after.raw_sql)
        if before is None or after is None:
            return []

        before_amount, before_unit = before
        after_amount, after_unit = after
        if before_unit != after_unit or after_amount >= before_amount:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=(
                    f"Incremental lookback narrowed from {before_amount} to "
                    f"{after_amount} {after_unit}(s)"
                ),
                severity=_severity_for(ctx, self.severity),
                confidence=Confidence.LIKELY,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=(
                        f"interval '{before_amount}' {before_unit} -> '{after_amount}' {after_unit}"
                    ),
                ),
                consequence=(
                    "Rows arriving later than the new window are never picked up. "
                    "Nothing errors and no run fails — the data is simply absent, and "
                    "the gap is only visible by reconciling against the source."
                ),
                suggestion=(
                    "Check the actual arrival lag of the source before narrowing the "
                    "window; a backfill is needed for anything already missed."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


def _largest_interval(sql: str) -> tuple[int, str] | None:
    """The largest interval literal in a model, which is the lookback in practice."""
    matches = _INTERVAL.findall(sql)
    if not matches:
        return None
    parsed = [(int(amount), unit.lower()) for amount, unit in matches]
    return max(parsed, key=lambda pair: pair[0])


RULES: tuple[Rule, ...] = (
    IncrementalGuardRemovedRule(),
    IncrementalStrategyChangedRule(),
    IncrementalKeyChangedRule(),
    MaterializationChangedRule(),
    LookbackWindowNarrowedRule(),
)
