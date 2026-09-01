"""Wire the stages together.

Kept deliberately linear and explicit. This is a merge gate, so being able to read the
whole flow in one screen matters more than any abstraction it might be factored into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from themis.acquire.snapshot_builder import AcquireResult, acquire
from themis.analyze.grain import infer_grains
from themis.config import Settings
from themis.logging import get_logger
from themis.models import Finding, Grain
from themis.rules.base import RuleContext, SkippedRule
from themis.rules.registry import run_rules

log = get_logger(__name__)


@dataclass
class ReviewResult:
    """Everything one review produced, including what it could not check."""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[SkippedRule] = field(default_factory=list)
    grains: dict[str, Grain] = field(default_factory=dict)
    models_reviewed: tuple[str, ...] = ()
    macro_affected: dict[str, tuple[str, ...]] = field(default_factory=dict)
    degraded_reason: str | None = None
    executed: bool = False


def build_contexts(
    result: AcquireResult, grains: dict[str, Grain], *, dialect: str
) -> list[RuleContext]:
    """One context per model the change actually affects.

    Two sources feed this: models whose own file changed, and models reached through a
    changed macro. The second is why a one-line macro edit is reviewed as the N-model
    change it is rather than the one-file change it looks like.
    """
    directly_changed = set(result.changed_models)

    via_macro: dict[str, str] = {}
    for macro in result.changed_macros:
        for model in result.after.models_using_macro(macro):
            if model not in directly_changed:
                via_macro.setdefault(model, macro)

    contexts: list[RuleContext] = []
    for name in sorted(directly_changed | set(via_macro)):
        before = result.before.models.get(name)
        after = result.after.models.get(name)
        if before is None and after is None:
            continue  # a changed file that is not a model in either revision
        contexts.append(
            RuleContext(
                model_name=name,
                before=before,
                after=after,
                before_snapshot=result.before,
                after_snapshot=result.after,
                grains=grains,
                dialect=dialect,
                via_macro=via_macro.get(name),
            )
        )
    return contexts


def review(
    project_dir: Path,
    *,
    base: str,
    head: str,
    settings: Settings,
    target: str = "dev",
    prod_manifest: Path | None = None,
) -> ReviewResult:
    """Run stages 0 through 2 and return the result.

    Execution (stage 3) and the model review (stages 4-5) layer on top of this; the
    deterministic core stands alone and is useful without either.
    """
    acquired = acquire(
        project_dir,
        base=base,
        head=head,
        target=target,
        allowed_targets=settings.execute_allowed_targets,
        timeout_s=settings.execute_timeout_s,
        prod_manifest=prod_manifest,
    )

    grains = infer_grains(acquired.after, dialect=settings.dialect)
    contexts = build_contexts(acquired, grains, dialect=settings.dialect)
    findings, skipped = run_rules(contexts)

    macro_affected = {
        macro: acquired.after.models_using_macro(macro)
        for macro in acquired.changed_macros
    }

    log.info(
        "review.complete",
        models=len(contexts),
        findings=len(findings),
        skipped=len(skipped),
    )
    return ReviewResult(
        findings=findings,
        skipped=skipped,
        grains=grains,
        models_reviewed=tuple(c.model_name for c in contexts),
        macro_affected=macro_affected,
        degraded_reason=acquired.degraded_reason,
    )
