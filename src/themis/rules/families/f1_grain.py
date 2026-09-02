"""F1 — grain and fan-out. The silent-wrong-number family.

These are the rules the whole design exists for. A join whose right-hand side is not
unique on the join key multiplies every row it matches, and in a ledger that means
revenue is overstated with nothing about the output looking wrong. No error, no failed
test, no obviously odd number — just a total that is too big.

Everything here is grounded on the derived grain lattice rather than declared tests,
because a project that declares none still needs these checks — and those are exactly
the projects where a fan-out is most likely to reach production unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.grain import structural_grain
from themis.analyze.parse import (
    ParseError,
    find_joins,
    join_kind,
    parse_sql,
    resolve_relation,
)
from themis.models import (
    Confidence,
    Evidence,
    Finding,
    GrainSource,
    Severity,
)
from themis.rules.base import Rule, RuleContext

FAMILY = "F1"


def _join_key_columns(join: exp.Join) -> tuple[str, ...]:
    """Column names appearing in a join's ON condition."""
    on = join.args.get("on")
    if on is None:
        return ()
    return tuple(dict.fromkeys(col.name for col in on.find_all(exp.Column)))


def _joined_relation_name(join: exp.Join) -> str | None:
    """The table or CTE on the right-hand side of a join."""
    target = join.this
    if isinstance(target, exp.Table):
        return target.name
    if isinstance(target, exp.Alias):
        return target.alias
    return None


def _severity_for(ctx: RuleContext, base: Severity) -> Severity:
    """Escalate when the change reaches a regulatory figure or an exposure."""
    if base is Severity.CRITICAL:
        return base
    if ctx.is_governed or ctx.reaches_exposure:
        return Severity.CRITICAL if base is Severity.HIGH else Severity.HIGH
    return base


@dataclass
class JoinFanOutRule(Rule):
    """A join was added whose right-hand key is not proven unique.

    This is the rule that catches revenue being multiplied. It fires on *possible*
    fan-out rather than certain fan-out, because certainty needs either a declared
    test (which does not exist here) or an actual count (which is Stage 3's job).
    Stage 3 upgrades the survivors to MEASURED and settles them.
    """

    rule_id: str = field(init=False, default="F1001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or ctx.after.analysable_sql is None:
            return []
        try:
            after_tree = parse_sql(ctx.after.analysable_sql, dialect=ctx.dialect)
        except ParseError:
            return []

        # Compare joins by the model they resolve to, never by the CTE alias. Renaming
        # a CTE is a routine refactor, and comparing aliases makes every join in a
        # tidied-up model look new -- which is how a control set of behaviour-preserving
        # refactors turns into a wall of false positives and the rule gets switched off.
        before_joins: set[tuple[str, tuple[str, ...]]] = set()
        if ctx.before is not None and ctx.before.analysable_sql is not None:
            try:
                before_tree = parse_sql(ctx.before.analysable_sql, dialect=ctx.dialect)
                before_joins = {
                    (resolve_relation(before_tree, _joined_relation_name(j) or ""), keys)
                    for j in find_joins(before_tree)
                    if (keys := _join_key_columns(j))
                }
            except ParseError:
                before_joins = set()

        findings: list[Finding] = []
        for join in find_joins(after_tree):
            alias = _joined_relation_name(join)
            keys = _join_key_columns(join)
            if alias is None or not keys:
                continue
            # The join names a CTE; the grain is recorded against the model it reads.
            relation = resolve_relation(after_tree, alias)
            if (relation, keys) in before_joins:
                continue  # unchanged join; not this PR's problem

            grain = ctx.grains.get(relation)
            if grain is not None and grain.is_proven and set(grain.columns) <= set(keys):
                continue  # the join key covers a proven unique key: safe

            findings.append(self._finding(ctx, join, relation, keys, grain))
        return findings

    def _finding(
        self,
        ctx: RuleContext,
        join: exp.Join,
        relation: str,
        keys: tuple[str, ...],
        grain: object,
    ) -> Finding:
        from themis.models import Grain  # local import keeps the annotation honest

        assert ctx.after is not None
        if not isinstance(grain, Grain) or grain.source is GrainSource.UNKNOWN:
            detail = (
                f"the grain of {relation} could not be derived, so whether this join "
                "multiplies rows is unknown"
            )
            confidence = Confidence.POSSIBLE
        elif not grain.is_proven:
            # A weak grain must never read as a confident claim. It is exactly the
            # case where the derived key is most likely to be incomplete -- and an
            # incomplete key is what a fan-out hides behind.
            detail = (
                f"{relation} looks unique on ({', '.join(grain.columns)}), but that is "
                f"{grain.source.value}, not proven — if the real key has more columns, "
                "this join multiplies rows"
            )
            confidence = Confidence.POSSIBLE
        else:
            detail = (
                f"{relation} is unique on ({', '.join(grain.columns)}) "
                f"[{grain.source.value}], which the join key ({', '.join(keys)}) "
                "does not cover"
            )
            confidence = Confidence.LIKELY

        return Finding(
            rule_id=self.rule_id,
            family=self.family,
            title=f"New join to {relation} may fan out",
            severity=_severity_for(ctx, self.severity),
            confidence=confidence,
            evidence=Evidence(
                model_name=ctx.model_name,
                file_path=ctx.after.file_path,
                sql_after=join.sql(dialect=ctx.dialect),
                note=detail,
                related_model=relation,
            ),
            consequence=(
                f"Any row matching more than once in {relation} on "
                f"({', '.join(keys)}) is duplicated, and every amount summed "
                "downstream is overstated by that multiple. Nothing errors and no "
                "individual row looks malformed — the total is simply too large."
            ),
            suggestion=(
                f"Confirm the grain of {relation}. If it is not unique on "
                f"({', '.join(keys)}), either add the missing key columns to the ON "
                "clause or pre-aggregate before joining."
            ),
            blast_radius=ctx.blast_radius,
        )


@dataclass
class JoinTypeChangedRule(Rule):
    """A join's type changed — LEFT to INNER drops rows, INNER to LEFT introduces NULLs.

    Both directions are wrong in different ways, and both are near-invisible in a text
    diff when the surrounding query was reformatted in the same commit.
    """

    rule_id: str = field(init=False, default="F1002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []
        try:
            before_tree = parse_sql(before_sql, dialect=ctx.dialect)
            after_tree = parse_sql(after_sql, dialect=ctx.dialect)
            before_joins = {
                resolve_relation(before_tree, _joined_relation_name(j) or ""): join_kind(j)
                for j in find_joins(before_tree)
            }
            after_joins = {
                resolve_relation(after_tree, _joined_relation_name(j) or ""): join_kind(j)
                for j in find_joins(after_tree)
            }
        except ParseError:
            return []

        findings: list[Finding] = []
        for relation, after_kind in after_joins.items():
            before_kind = before_joins.get(relation)
            if before_kind is None or before_kind == after_kind:
                continue
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title=f"Join to {relation} changed from {before_kind} to {after_kind}",
                    severity=_severity_for(ctx, self.severity),
                    confidence=Confidence.PROVEN,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        note=f"{before_kind} -> {after_kind} on {relation}",
                    ),
                    consequence=self._consequence(before_kind, after_kind, relation),
                    suggestion="Confirm this was intended.",
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings

    @staticmethod
    def _consequence(before: str, after: str, relation: str) -> str:
        if "LEFT" in before and after == "INNER":
            return (
                f"Rows with no match in {relation} are now dropped entirely. Revenue "
                "belonging to unmatched entries silently disappears from the total, "
                "and the result still looks like a complete dataset."
            )
        if before == "INNER" and "LEFT" in after:
            return (
                f"Unmatched rows from {relation} now arrive as NULLs. Any SUM over "
                "them skips those rows, and any arithmetic involving them yields NULL "
                "rather than an error."
            )
        return (
            f"Join semantics against {relation} changed, so both the row set and any "
            "aggregate computed over it change with it."
        )


@dataclass
class GroupByGrainChangedRule(Rule):
    """The model's output grain changed.

    Everything downstream was written against the old grain. A mart that was one row
    per (period, entity) becoming one row per (period, entity, currency) does not
    break — it starts answering a different question, and every total computed from it
    moves without anything failing.

    Grain comes from the shared derivation, which resolves through pass-through CTEs.
    Reading the outermost SELECT directly does not work on real dbt models, where the
    GROUP BY is almost always inside the final CTE — a rule written that way never
    fires at all.
    """

    rule_id: str = field(init=False, default="F1003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        before_grain = structural_grain(before_sql, ctx.dialect)
        after_grain = structural_grain(after_sql, ctx.dialect)
        if before_grain is None or after_grain is None:
            return []

        before_keys, _ = before_grain
        after_keys, _ = after_grain
        if set(before_keys) == set(after_keys):
            return []

        added = tuple(sorted(set(after_keys) - set(before_keys)))
        removed = tuple(sorted(set(before_keys) - set(after_keys)))
        if not added and not removed:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Output grain changed",
                severity=_severity_for(ctx, self.severity),
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=(f"grain ({', '.join(before_keys)}) -> ({', '.join(after_keys)})"),
                ),
                consequence=(
                    (f"Added to the grain: {', '.join(added)}. " if added else "")
                    + (f"Removed from the grain: {', '.join(removed)}. " if removed else "")
                    + "Every downstream model was written against the previous grain. "
                    "Removing a key aggregates across a dimension that used to be "
                    "separate; adding one splits rows that used to be combined. Either "
                    "way downstream totals move without anything failing."
                ),
                suggestion=(
                    f"Check the {len(ctx.blast_radius)} downstream model(s) for "
                    "assumptions about the old grain, particularly further aggregation "
                    "or joins on the previous key."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


RULES: tuple[Rule, ...] = (
    JoinFanOutRule(),
    JoinTypeChangedRule(),
    GroupByGrainChangedRule(),
)
