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
from themis.execute.runner import ExecutionResult, execute
from themis.logging import get_logger
from themis.models import Confidence, ExecutionDelta, Finding, Grain, GrainSource
from themis.review.supervisor import ReviewSummary
from themis.rules.base import RuleContext, SkippedRule
from themis.rules.registry import run_rules
from themis.snapshot import ProjectSnapshot

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
    execution: ExecutionResult | None = None
    llm: ReviewSummary | None = None


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
    for macro_file in result.changed_macro_files:
        names = result.after.macros_in_file(macro_file) or (Path(macro_file).stem,)
        label = ", ".join(names)
        for model in result.after.models_using_macro_file(macro_file):
            if model not in directly_changed:
                via_macro.setdefault(model, label)

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


def attach_execution(findings: list[Finding], result: ExecutionResult) -> list[Finding]:
    """Attach measured evidence to the findings it settles.

    A finding whose model actually moved is no longer a hypothesis, so its confidence
    is raised to MEASURED and it bypasses the model review entirely — there is nothing
    left to adjudicate once the row count and the total have both changed.
    """
    attached: list[Finding] = []
    for finding in findings:
        delta = result.deltas.get(finding.evidence.model_name)
        if delta is None or not delta.is_material:
            attached.append(finding)
            continue
        attached.append(
            finding.model_copy(
                update={
                    "execution_delta": delta,
                    "confidence": Confidence.MEASURED,
                }
            )
        )
    return attached


def unexplained_change_findings(
    result: ExecutionResult,
    findings: list[Finding],
    before: ProjectSnapshot,
    after: ProjectSnapshot,
) -> list[Finding]:
    """Report models whose results moved with no rule explaining why.

    This is the safety net under the entire rule catalogue. Rules only catch defect
    classes somebody anticipated, so a change outside all of them produces a clean
    report while the money moves. That happened: inverting an FX conversion shifted
    revenue by 1.8 million across six models and the review said "no findings", because
    multiplying instead of dividing is arithmetically ordinary and structurally
    invisible.

    Changes are attributed to their **root** — the model whose own SQL changed — rather
    than reported once per affected model. A single edit propagates through the DAG, so
    per-model reporting turns one defect into six findings, five of which can only say
    that nothing changed in their own SQL. That is noise for the reviewer and, when the
    model layer runs, five wasted calls producing five "unclear" answers.
    """

    explained = {f.evidence.model_name for f in findings}

    moved_models = {
        name
        for name, delta in result.deltas.items()
        if delta.is_material and not delta.build_error and name not in explained
    }
    if not moved_models:
        return []

    # A root is a model whose own compiled SQL differs. Everything else inherited its
    # change from upstream.
    roots: set[str] = set()
    for name in moved_models:
        before_sql = (before.models.get(name) or after.models.get(name)) and (
            before.models[name].analysable_sql if name in before.models else None
        )
        after_sql = after.models[name].analysable_sql if name in after.models else None
        if before_sql != after_sql:
            roots.add(name)

    # Anything downstream of a root is accounted for by that root.
    attributed: dict[str, list[str]] = {root: [] for root in roots}
    unattributed: set[str] = set()
    for name in sorted(moved_models - roots):
        owner = next((root for root in sorted(roots) if name in after.downstream_of(root)), None)
        if owner is not None:
            attributed[owner].append(name)
        else:
            # Moved, own SQL unchanged, and downstream of nothing that changed. That is
            # genuinely unexplained and must not be folded into someone else's finding.
            unattributed.add(name)

    out: list[Finding] = []
    for name in sorted(roots | unattributed):
        delta = result.deltas[name]
        consequences = tuple(sorted(attributed.get(name, [])))
        out.append(
            _unexplained_finding(
                name,
                delta,
                after=after,
                consequences=consequences,
                is_root=name in roots,
            )
        )
    return out


def _unexplained_finding(
    name: str,
    delta: ExecutionDelta,
    *,
    after: ProjectSnapshot,
    consequences: tuple[str, ...],
    is_root: bool,
) -> Finding:
    from themis.models import Evidence, Severity

    moved = [
        (column, was, now) for column, (was, now) in sorted(delta.sum_deltas.items()) if was != now
    ]
    rows_moved = delta.row_delta not in (0, None)

    model = after.models.get(name)
    governed = bool(model and {"regulatory", "recon", "control"} & set(model.tags))
    severity = Severity.CRITICAL if (moved and governed) else Severity.HIGH

    detail: list[str] = []
    if rows_moved and delta.rows_before is not None and delta.rows_after is not None:
        detail.append(f"rows {delta.rows_before:,} -> {delta.rows_after:,}")
    for column, was, now in moved:
        shift = ((now - was) / was * 100) if was else 0.0
        detail.append(f"sum({column}) {was:,.2f} -> {now:,.2f} ({shift:+.1f}%)")

    if consequences:
        detail.append(f"same change reaches {len(consequences)} downstream model(s)")

    origin = (
        "Building both revisions produced different results for this model, and none "
        "of the checks accounts for the difference."
        if is_root
        else "This model's results changed although its own SQL did not, and nothing "
        "upstream that changed accounts for it."
    )
    reach = f" The same movement carries into {', '.join(consequences)}." if consequences else ""

    return Finding(
        rule_id="X0001",
        family="X",
        title=f"`{name}` changed and no rule explains why",
        severity=severity,
        confidence=Confidence.MEASURED,
        evidence=Evidence(model_name=name, note="; ".join(detail)),
        consequence=(
            origin + " That means the change is outside every defect class this tool knows "
            "about — so it has not been assessed, only observed."
            + (" A reported figure moved." if governed and moved else "")
            + reach
            + " It needs a human explanation before merging."
        ),
        suggestion=(
            "Confirm the movement is intended and expected at this magnitude. If it is "
            "a defect class worth catching automatically, it is a candidate for a new "
            "rule."
        ),
        blast_radius=after.downstream_of(name),
        execution_delta=delta,
    )


def measured_grain_findings(result: ExecutionResult, inferred: dict[str, Grain]) -> list[Finding]:
    """Report where measurement and inference disagree.

    That disagreement is itself worth surfacing: it is the only direct evidence of
    whether the derivation lattice can be trusted on a given project.
    """
    from themis.models import Evidence, Severity

    findings: list[Finding] = []
    for name, measured in result.measured_grains.items():
        if measured.rows_per_key is None or measured.rows_per_key <= 1.0:
            continue
        derived = inferred.get(name)
        source = derived.source.value if derived else GrainSource.UNKNOWN.value
        findings.append(
            Finding(
                rule_id="F1004",
                family="F1",
                title=f"`{name}` is not unique on its derived key",
                severity=Severity.HIGH,
                confidence=Confidence.MEASURED,
                evidence=Evidence(model_name=name, note=measured.note),
                consequence=(
                    f"The key ({', '.join(measured.columns)}) was derived as this "
                    f"model's grain [{source}], but the built table has "
                    f"{measured.rows_per_key:.2f} rows per key. Any join onto that key "
                    "multiplies rows, and any amount summed after it is overstated by "
                    "roughly that factor."
                ),
                suggestion=(
                    "Either the join keys are incomplete or the model genuinely has a "
                    "finer grain than assumed. Adding a uniqueness test on the real key "
                    "would make this checkable without a build."
                ),
            )
        )
    return findings


def review(
    project_dir: Path,
    *,
    base: str,
    head: str,
    settings: Settings,
    target: str = "dev",
    prod_manifest: Path | None = None,
    run_execution: bool = False,
    run_llm: bool = False,
    pr_description: str | None = None,
) -> ReviewResult:
    """Run the deterministic stages, optionally including execution.

    The model review (stages 4-5) layers on top of this; the deterministic core stands
    alone and is useful without it.
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
        macro: acquired.after.models_using_macro(macro) for macro in acquired.changed_macros
    }

    execution: ExecutionResult | None = None
    if run_execution:
        # Measure descendants as well as the changed models themselves. A fan-out in an
        # intermediate model is invisible in its own row count when the join is the last
        # step, but shows up unmistakably as an inflated SUM in the mart below it.
        changed = {c.model_name for c in contexts}
        targets = set(changed)
        for name in changed:
            targets.update(acquired.after.downstream_of(name))
        # Two-pass building is only needed when something in the selection is
        # incremental; for everything else the second pass is wasted time.
        has_incremental = any(
            (acquired.after.models.get(name) or acquired.before.models.get(name))
            and (acquired.after.models.get(name) or acquired.before.models[name]).materialization
            == "incremental"
            for name in targets
        )
        execution = execute(
            project_dir,
            base=base,
            head=head,
            models=tuple(sorted(targets)),
            settings=settings,
            target=target,
            grain_candidates=grains,
            has_incremental=has_incremental,
        )
        if execution.ran:
            findings = attach_execution(findings, execution)
            findings.extend(measured_grain_findings(execution, grains))
            # Runs after the others so "explained" reflects everything already found.
            findings.extend(
                unexplained_change_findings(execution, findings, acquired.before, acquired.after)
            )
            grains = {**grains, **execution.measured_grains}
        else:
            log.warning("execute.skipped", reason=execution.skipped_reason)

    llm_summary: ReviewSummary | None = None
    if run_llm and findings:
        # Runs last on purpose. Execution settles what it can first, so the model is
        # only asked about findings that are still genuinely open.
        from themis.llm.provider import LLMError, build_provider
        from themis.review import supervisor

        try:
            provider = build_provider(settings)
            llm_summary = supervisor.review(
                findings,
                provider=provider,
                settings=settings,
                snapshot=acquired.after,
                grains=grains,
                changed_models=tuple(c.model_name for c in contexts),
                pr_description=pr_description,
                before_snapshot=acquired.before,
            )
            findings = llm_summary.findings
        except LLMError as exc:
            # A model that cannot be reached must not fail the review; the
            # deterministic findings stand on their own.
            log.warning("review.llm_unavailable", error=str(exc)[:200])

    log.info(
        "review.complete",
        models=len(contexts),
        findings=len(findings),
        skipped=len(skipped),
        executed=bool(execution and execution.ran),
        llm=bool(llm_summary),
    )
    return ReviewResult(
        findings=findings,
        skipped=skipped,
        grains=grains,
        models_reviewed=tuple(c.model_name for c in contexts),
        macro_affected=macro_affected,
        degraded_reason=acquired.degraded_reason,
        executed=bool(execution and execution.ran),
        execution=execution,
        llm=llm_summary,
    )
