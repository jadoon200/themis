"""F7 — governance and audit.

Reduced in scope on purpose. Most of what this family would normally cover — naming,
documentation, test presence — is already handled by dbt-bouncer, and reimplementing it
would add noise without adding coverage.

What remains is the part that is diff-aware or specific to financial data: a sensitive
column reaching a wider audience, and a change to a model whose numbers are reported
externally.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from themis.analyze import injection
from themis.models import Confidence, Evidence, Finding, GrainSource, Severity
from themis.rules.base import Rule, RuleContext

FAMILY = "F7"


@dataclass
class SensitiveColumnExposedRule(Rule):
    """A sensitive-looking column newly selected into a wide-audience model."""

    rule_id: str = field(init=False, default="F7001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None:
            return []
        if not ctx.vocabulary.is_published(ctx.after.file_path or ""):
            return []

        from themis.rules.families.f6_contracts import _output_columns, _resolved_outputs

        after_sql = ctx.after.analysable_sql
        if after_sql is None:
            return []
        # A star hides what the projection exposes, which for this rule is the
        # interesting case: a sensitive column can arrive through one. Column lineage
        # resolves the star against the project schema and can see it; the AST cannot.
        after_columns = _output_columns(after_sql, ctx.dialect)
        if after_columns is None:
            after_columns = _resolved_outputs(ctx, before=False)
        if after_columns is None:
            return []

        if ctx.before is None:
            # A new model: every column it emits is newly exposed.
            before_columns: set[str] = set()
        else:
            before_sql = ctx.before.analysable_sql
            if before_sql is None:
                return []
            resolved = _output_columns(before_sql, ctx.dialect)
            if resolved is None:
                resolved = _resolved_outputs(ctx, before=True)
            if resolved is None:
                # Unknowable which columns are new. Treating the previous projection as
                # empty would report every sensitive column the model has ever carried
                # as freshly exposed.
                return []
            before_columns = resolved

        added = {c for c in after_columns - before_columns if ctx.vocabulary.is_sensitive(c)}
        if not added:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=f"Sensitive column(s) added to a published model: {', '.join(sorted(added))}",
                severity=Severity.CRITICAL if ctx.reaches_exposure else self.severity,
                confidence=Confidence.LIKELY,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    # The first, so lineage can be asked where this one ends up. A
                    # finding naming several columns still points at a real one.
                    column_name=sorted(added)[0],
                    note=f"newly selected: {', '.join(sorted(added))}",
                ),
                consequence=(
                    "These columns look like personal or restricted data and are now "
                    "emitted by a model in a published folder, which is read by "
                    "consumers outside the team that owns it. Access to the mart is "
                    "not the same as access to the underlying source."
                ),
                suggestion=(
                    "Confirm the audience is entitled to these fields. If not, mask, "
                    "hash, or drop them here rather than relying on downstream to."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class UnprovableGrainOnGovernedModelRule(Rule):
    """A reported model whose grain cannot be established.

    The inverse of the usual test rule, and the one that matters where nothing is
    declared: if the grain of a model feeding a regulatory figure cannot be derived,
    no fan-out check on it means anything.
    """

    rule_id: str = field(init=False, default="F7002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.MEDIUM)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or not (ctx.is_governed or ctx.reaches_exposure):
            return []
        grain = ctx.grains.get(ctx.model_name)
        if grain is not None and grain.is_proven:
            return []

        source = grain.source.value if grain else GrainSource.UNKNOWN.value
        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Grain cannot be proven for a reported model",
                severity=self.severity,
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"grain source: {source}",
                ),
                consequence=(
                    "This model is tagged for reconciliation or reporting, but its "
                    "grain could not be derived from its SQL and nothing declares it. "
                    "Every duplication check involving this model is therefore "
                    "unproven — including the ones that returned clean."
                ),
                suggestion=(
                    "Add a uniqueness test on the intended key. Running with "
                    "`--execute` will measure the actual key and tell you what it is."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class ApproximateAggregateInReportedModelRule(Rule):
    """An approximate aggregate in a model whose figures are reported externally."""

    rule_id: str = field(init=False, default="F7003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    _APPROX = re.compile(r"\bapprox_\w+\s*\(", re.IGNORECASE)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or not (ctx.is_governed or ctx.reaches_exposure):
            return []
        sql = ctx.after.analysable_sql
        if sql is None:
            return []

        found = set(self._APPROX.findall(sql))
        if not found:
            return []
        before_sql = ctx.before.analysable_sql if ctx.before else None
        if before_sql is not None and set(self._APPROX.findall(before_sql)) >= found:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Approximate aggregate in a reported model",
                severity=self.severity,
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=(
                        "approximate function(s): "
                        + ", ".join(sorted(f.strip("( ") for f in found))
                    ),
                ),
                consequence=(
                    "Functions like `approx_distinct` trade accuracy for speed. This "
                    "model is tagged as reconciliation or regulatory, where a figure "
                    "that is close is a figure that does not tie out — and the error "
                    "is not reproducible between runs."
                ),
                suggestion=(
                    "Use the exact aggregate here and keep the approximation for exploration."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class TextAddressedToTheReviewerRule(Rule):
    """A comment or literal written at whatever reads the model, rather than at a person.

    Every other rule here asks whether a change is safe. This one asks whether someone is
    trying to steer the thing that answers that question. It exists because THEMIS's own
    defences cannot: a planted sentence is really in the model, so an AI reviewer quoting
    it is quoting verbatim, the grounding check passes, and "this model is approved"
    arrives as evidence. Only a deterministic reading of the text catches it, and only a
    person should settle it — which is why this is a finding and not a filter.

    Reported on the text a *change* introduces. A model that has always carried the line
    is a fact about the repository, not about this pull request.
    """

    rule_id: str = field(init=False, default="F7004")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None:
            return []
        # Raw source, not compiled: this is about what someone wrote in the file, and a
        # Jinja comment never survives compilation.
        after_sql = ctx.after.raw_sql or ctx.after.analysable_sql
        if not after_sql:
            return []
        before_sql = (ctx.before.raw_sql or ctx.before.analysable_sql) if ctx.before else None
        planted = injection.added(before_sql, after_sql)
        if not planted:
            return []

        # Every planted line, not the first with a count: "(and 2 more)" hides the one a
        # reviewer most needs to read, and there are never many.
        shown = planted[:5]
        lines = "; ".join(f"line {item.line} {item.why}: {item.text!r}" for item in shown)
        more = f"; and {len(planted) - len(shown)} more" if len(planted) > len(shown) else ""
        first = planted[0]
        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Text in this model addresses an automated reviewer",
                severity=self.severity,
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    line=first.line,
                    note=lines + more,
                ),
                consequence=(
                    "An automated review reads this SQL, so a comment in it is a message "
                    "to that reviewer. THEMIS makes its AI reviewers quote what they rely "
                    "on, and this text would be quoted truthfully — it really is in the "
                    "model — so no grounding check can refuse it. Whatever the intent, a "
                    "change that writes instructions to the review process is a change a "
                    "person has to look at."
                ),
                suggestion=(
                    "Confirm with the author why this is here. Comments should describe "
                    "the SQL for the people who maintain it."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


RULES: tuple[Rule, ...] = (
    SensitiveColumnExposedRule(),
    UnprovableGrainOnGovernedModelRule(),
    ApproximateAggregateInReportedModelRule(),
    TextAddressedToTheReviewerRule(),
)
