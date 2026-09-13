"""Rule registry and the runner that applies them to a change set."""

from __future__ import annotations

from themis.logging import get_logger
from themis.models import Finding
from themis.rules.base import Rule, RuleContext, SkippedRule
from themis.rules.families import (
    f1_grain,
    f2_filters,
    f3_money,
    f4_periods,
    f5_incremental,
    f6_contracts,
    f7_governance,
    f8_engine,
)

log = get_logger(__name__)

# Families register here as they land. Order is presentation-neutral: findings are
# ranked by severity and blast radius at report time, never by declaration order.
ALL_RULES: tuple[Rule, ...] = (
    *f1_grain.RULES,
    *f2_filters.RULES,
    *f3_money.RULES,
    *f4_periods.RULES,
    *f5_incremental.RULES,
    *f6_contracts.RULES,
    *f7_governance.RULES,
    *f8_engine.RULES,
)


def run_rules(
    contexts: list[RuleContext], *, rules: tuple[Rule, ...] = ALL_RULES
) -> tuple[list[Finding], list[SkippedRule]]:
    """Apply every rule to every changed model.

    Skips are collected and returned rather than dropped. A reviewer reading a clean
    report needs to be able to tell "nothing was wrong" from "half the checks could
    not run because the manifest was not compiled".
    """
    findings: list[Finding] = []
    skipped: list[SkippedRule] = []

    for ctx in contexts:
        for rule in rules:
            if not rule.applies_to(ctx):
                skipped.append(
                    SkippedRule(
                        rule_id=rule.rule_id,
                        model_name=ctx.model_name,
                        reason=_skip_reason(rule, ctx),
                    )
                )
                continue
            try:
                findings.extend(rule.check(ctx))
            except Exception as exc:  # one broken rule must not sink the review
                log.warning("rule.error", rule=rule.rule_id, model=ctx.model_name, error=str(exc))
                skipped.append(
                    SkippedRule(
                        rule_id=rule.rule_id,
                        model_name=ctx.model_name,
                        reason=f"rule raised {type(exc).__name__}: {exc}",
                    )
                )

    log.info("rules.complete", findings=len(findings), skipped=len(skipped))
    return findings, skipped


def _skip_reason(rule: Rule, ctx: RuleContext) -> str:
    if rule.requires_compiled_sql:
        head_missing = ctx.after is not None and ctx.after.analysable_sql is None
        base_missing = ctx.before is not None and ctx.before.analysable_sql is None
        # Say which revision, and do not guess at why: `dbt parse` is one cause, a
        # compile that aborted part-way is another, and the degraded-grounding line at the
        # top of the report names the actual one.
        if head_missing and base_missing:
            return "no compiled SQL for this model in either revision"
        if head_missing:
            return "no compiled SQL for this model in the head revision"
        if base_missing:
            return "no compiled SQL for this model in the base revision"
    return "the rule's grounding requirement was not met"
