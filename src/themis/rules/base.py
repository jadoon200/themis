"""The rule contract.

Rules are written for **recall**. A rule that fires on a change which turns out to be
fine costs a triage step; a rule that stays silent on a fan-out costs a restatement.
So rules over-flag deliberately, carry an honest ``confidence``, and let Stage 4
suppress — never the other way round.

Each rule declares the grounding it needs. Where that grounding is absent the rule is
skipped *visibly*, because a rule that quietly does nothing looks exactly like a rule
that found nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from themis.models import Finding, Grain, Severity
from themis.snapshot import ModelNode, ProjectSnapshot


@dataclass
class RuleContext:
    """Everything a rule may look at for one changed model.

    Deliberately narrow. A rule that needs something absent from here is a rule whose
    grounding requirement is wrong, not a reason to reach into the filesystem.
    """

    model_name: str
    before: ModelNode | None
    after: ModelNode | None
    before_snapshot: ProjectSnapshot
    after_snapshot: ProjectSnapshot
    grains: dict[str, Grain]
    dialect: str = "trino"
    # Populated when a macro edit is what pulled this model into the review.
    via_macro: str | None = None
    # Populated when a schema YAML edit is what pulled it in.
    via_yaml: str | None = None

    @property
    def is_new(self) -> bool:
        return self.before is None and self.after is not None

    @property
    def is_deleted(self) -> bool:
        return self.after is None

    @property
    def blast_radius(self) -> tuple[str, ...]:
        return self.after_snapshot.downstream_of(self.model_name)

    @property
    def reaches_exposure(self) -> bool:
        """Whether this change lands in a dashboard or regulatory submission."""
        affected = {self.model_name, *self.blast_radius}
        for exposure in self.after_snapshot.exposures.values():
            if any(dep.split(".")[-1] in affected for dep in exposure.depends_on):
                return True
        return False

    @property
    def is_governed(self) -> bool:
        """Models tagged for reconciliation or regulatory use escalate automatically."""
        model = self.after or self.before
        if model is None:
            return False
        return bool({"regulatory", "recon", "control"} & set(model.tags))


@dataclass
class Rule(ABC):
    """One check. Small, single-purpose, independently testable."""

    rule_id: str = field(init=False)
    family: str = field(init=False)
    severity: Severity = field(init=False)
    # Most rules read the macro-expanded SQL; the few that read configs do not. This is
    # the only grounding requirement that does real work — every snapshot comes from a
    # compiled manifest, so there is no weaker backend left to gate against.
    requires_compiled_sql: bool = field(init=False, default=True)

    @abstractmethod
    def check(self, ctx: RuleContext) -> list[Finding]:
        """Return every finding this rule sees. Empty is a normal answer."""

    def applies_to(self, ctx: RuleContext) -> bool:
        """Whether the available grounding supports running this rule at all."""
        if self.requires_compiled_sql:
            model = ctx.after or ctx.before
            if model is None or model.analysable_sql is None:
                return False
        return True


@dataclass(frozen=True)
class SkippedRule:
    """A rule that could not run, and why.

    Surfaced in the report. Silent skips are how a reviewer ends up trusting a clean
    result that was never actually checked.
    """

    rule_id: str
    model_name: str
    reason: str
