"""Running the specialists and merging what they say.

The supervisor decides three things: which findings are worth a model call at all,
what a specialist's answer is allowed to change, and how the result is ranked.

The second is the constrained one. A specialist may lower a finding's severity, add a
rationale, or mark it suppressed — it may never raise severity above what the rules
assigned, and it may never create a finding. Both restrictions exist because the model
is the least reliable component in the pipeline, so it is given the job with the
smallest blast radius.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from themis.analyze import injection
from themis.analyze.lineage import ColumnGraph
from themis.config import Settings
from themis.conventions import Convention
from themis.llm.context_pack import ContextPack, Section, build_intent_pack, build_pack
from themis.llm.provider import Provider, Usage
from themis.logging import get_logger
from themis.models import Confidence, Finding, Grain, ModelCall, Severity, Verdict
from themis.review import selfcheck
from themis.review.explain import explain
from themis.review.fix import propose
from themis.review.specialists import (
    INTENT,
    Adjudication,
    adjudicate,
    specialist_for,
)
from themis.snapshot import ProjectSnapshot
from themis.triage.rubric import review_order
from themis.vocabulary import from_settings as vocabulary_from_settings

log = get_logger(__name__)

_SEVERITY_ORDER = [
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
]


@dataclass
class ReviewSummary:
    """What the model layer contributed, kept separate so it can be measured."""

    findings: list[Finding] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    adjudicated: int = 0
    settled_without_llm: int = 0
    suppressed: int = 0
    rejected_by_selfcheck: int = 0
    # Measured changes the model proposed a cause for. Counted separately from
    # adjudications because it is a different job: not deciding whether a finding is
    # real, only suggesting why an already-certain number moved.
    explained: int = 0
    # Findings whose model writes to the reviewer, kept away from every seat that
    # could act on what it says. Counted so the skip is visible, like every other.
    withheld_for_planted_text: int = 0
    # Findings the model layer had no budget left for. Counted, like every other skip.
    not_reviewed_for_budget: int = 0
    undisclosed: list[str] = field(default_factory=list)
    # Every call made, with what it was shown and what it answered. Held in memory and
    # written only if the run is saved: the pipeline has no database, and a training
    # set that cannot be assembled is the thing this closes.
    calls: list[ModelCall] = field(default_factory=list)

    @property
    def skipped_as_settled(self) -> int:
        return self.settled_without_llm


def _addresses_the_reviewer(snapshot: ProjectSnapshot, model_name: str | None) -> bool:
    """Whether this model contains text written at whatever reviews it.

    F7004 reports such text to a person. This decides something else: whether the model
    layer is allowed to touch findings about it. It is not — a specialist can refute a
    finding or lower its severity, so a model that can steer one has a way to switch off
    the very check it tripped, and the self-check cannot object because the planted line
    is genuinely in the model.
    """
    if model_name is None:
        return False
    model = snapshot.models.get(model_name)
    if model is None:
        return False
    return bool(injection.planted_text(model.raw_sql or model.compiled_sql or ""))


def _needs_adjudication(finding: Finding) -> bool:
    """Whether a finding is worth spending a model call on.

    Anything execution demonstrated is settled — there is nothing left to judge once
    the row count and the total have both moved, and asking anyway invites the model to
    argue with a measurement. Anything derived with certainty from the AST is settled
    too.
    """
    if finding.is_settled:
        return False
    return finding.confidence in (Confidence.LIKELY, Confidence.POSSIBLE)


def _apply(finding: Finding, adjudication: Adjudication) -> Finding:
    """Apply an adjudication, within the limits the model is trusted with."""
    if adjudication.refuted:
        return finding.model_copy(
            update={
                "verdict": Verdict.SAFE,
                "llm_rationale": adjudication.rationale,
                "suppressed_reason": f"refuted by the {adjudication.specialist} reviewer",
            }
        )

    if adjudication.verdict == "uncertain":
        # Undecidable escalates to a human rather than defaulting to safe. On a project
        # that declares nothing, an over-confident default is the most dangerous thing
        # this tool could do.
        return finding.model_copy(
            update={
                "verdict": Verdict.UNDECIDABLE,
                "llm_rationale": adjudication.rationale or "the reviewer could not decide",
            }
        )

    severity = finding.severity
    try:
        proposed = Severity(adjudication.severity)
    except ValueError:
        proposed = severity
    # Lowering is allowed, raising is not. The rules encode the domain reasoning about
    # how bad each class of defect is; a model that has seen one finding in isolation
    # is not better placed to escalate it.
    if _SEVERITY_ORDER.index(proposed) > _SEVERITY_ORDER.index(severity):
        severity = proposed

    return finding.model_copy(
        update={
            "verdict": Verdict.BREAKING,
            "severity": severity,
            "llm_rationale": adjudication.rationale,
        }
    )


def review(
    findings: list[Finding],
    *,
    provider: Provider,
    settings: Settings,
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    changed_models: tuple[str, ...] = (),
    pr_description: str | None = None,
    before_snapshot: ProjectSnapshot | None = None,
    # Column lineage, so a specialist judging a removed column is told what reads it
    # rather than being handed a model-granular blast radius and left to guess.
    lineage: ColumnGraph | None = None,
    # What the project's reviewers have written down, shown to specialists as context.
    conventions: tuple[Convention, ...] = (),
) -> ReviewSummary:
    """Adjudicate the findings that warrant it, and run the intent pass."""
    summary = ReviewSummary()
    reviewed: list[Finding] = []

    # Conventions are read at the reviewed revision, so the same change can add one, and
    # they reach every specialist as context. One written at the reviewer is dropped.
    kept_conventions = tuple(
        convention
        for convention in conventions
        if not injection.addresses_a_reader(
            f"{convention.condition} {convention.guidance} {convention.implication}"
        )
    )
    if len(kept_conventions) != len(conventions):
        log.warning("supervisor.convention_addresses_the_reviewer")
        summary.withheld_for_planted_text += len(conventions) - len(kept_conventions)
        conventions = kept_conventions

    if pr_description and injection.addresses_a_reader(pr_description):
        # The description is written by whoever opened the change, and intent is the one
        # seat with no rule behind it. A description telling it what to conclude is not
        # shown to anything; the deterministic findings stand on their own.
        log.warning("supervisor.description_addresses_the_reviewer")
        summary.withheld_for_planted_text += 1
        pr_description = None

    # What the budget is spent on: the worst findings, by the rubric the report ranks with.
    # A reviewer told that twelve were not shown to the model layer can then take for
    # granted they are the twelve at the bottom of the report in front of them.
    budget = max(0, settings.llm_max_findings_reviewed)
    within_budget: set[int] = set()
    if len(findings) > budget:
        vocabulary = vocabulary_from_settings(settings)
        governed = frozenset(
            name for name, model in snapshot.models.items() if vocabulary.is_governed(model.tags)
        )
        ordered = review_order(findings, governed_models=governed)
        within_budget = {id(finding) for finding in ordered[:budget]}
        summary.not_reviewed_for_budget = len(findings) - budget
        log.warning(
            "supervisor.budget",
            findings=len(findings),
            reviewed=budget,
            not_reviewed=summary.not_reviewed_for_budget,
        )
    else:
        within_budget = {id(finding) for finding in findings}

    for finding in findings:
        if id(finding) not in within_budget:
            reviewed.append(finding)
            continue

        # The related model as well: a pack carries "the SQL of the model being joined
        # to", so text planted in a neighbour reaches the same seat by another door.
        if finding.rule_id != "F7004" and any(
            _addresses_the_reviewer(snapshot, name)
            for name in (finding.evidence.model_name, finding.evidence.related_model)
        ):
            log.warning(
                "supervisor.withheld",
                model=finding.evidence.model_name,
                rule=finding.rule_id,
            )
            summary.withheld_for_planted_text += 1
            reviewed.append(finding)
            continue

        if not _needs_adjudication(finding):
            # X0001 means "the numbers moved and nothing accounts for it". The
            # measurement is settled, so there is nothing to adjudicate — but the cause
            # is genuinely unknown, and proposing one is the single job here that a
            # rule cannot be written for. If it could be, the rule would exist.
            if finding.rule_id == "X0001" and before_snapshot is not None:
                hypothesis = explain(
                    finding,
                    provider=provider,
                    settings=settings,
                    before=before_snapshot,
                    after=snapshot,
                    usage=summary.usage,
                    record=summary.calls,
                )
                if hypothesis:
                    summary.explained += 1
                    reviewed.append(finding.model_copy(update={"llm_rationale": hypothesis}))
                    continue
            summary.settled_without_llm += 1
            reviewed.append(finding)
            continue

        specialist = specialist_for(finding.family)
        if specialist is None:
            reviewed.append(finding)
            continue

        pack = build_pack(
            finding,
            snapshot=snapshot,
            grains=grains,
            pr_description=pr_description,
            # Each reviewer reads only what its question turns on. A shared pack has to
            # carry everything anyone might need, and then nobody's evidence is narrow.
            needs=specialist.needs,
            lineage=lineage,
            conventions=conventions,
        )
        raw = adjudicate(provider, specialist, pack, model=settings.llm_specialist_model)
        if raw is not None:
            summary.usage.add(raw.usage)

        adjudication, rejection = selfcheck.verified(raw, pack)
        if raw is not None:
            summary.calls.append(
                ModelCall(
                    seat=specialist.name,
                    model=settings.llm_specialist_model,
                    context=pack.text,
                    system=specialist.system_prompt,
                    response={
                        "verdict": raw.verdict,
                        "severity": raw.severity,
                        "rationale": raw.rationale,
                        "evidence_quote": raw.evidence_quote,
                    },
                    accepted=adjudication is not None,
                    rejected_reason=rejection,
                    finding=finding,
                )
            )
        if adjudication is None:
            if rejection and raw is not None:
                summary.rejected_by_selfcheck += 1
            reviewed.append(finding)
            continue

        summary.adjudicated += 1
        updated = _apply(finding, adjudication)
        if updated.suppressed_reason:
            summary.suppressed += 1
        reviewed.append(updated)

    # After adjudication, so nothing is written for a finding a specialist just
    # refuted, and only for what a reviewer will actually be shown.
    reviewed = _propose_fixes(
        reviewed,
        withheld=frozenset(
            finding.evidence.model_name
            for finding in reviewed
            if _addresses_the_reviewer(snapshot, finding.evidence.model_name)
        ),
        provider=provider,
        settings=settings,
        snapshot=snapshot,
        grains=grains,
        lineage=lineage,
        usage=summary.usage,
        record=summary.calls,
        conventions=conventions,
    )

    if pr_description:
        summary.undisclosed = _intent_pass(
            provider,
            settings=settings,
            findings=findings,
            changed_models=changed_models,
            pr_description=pr_description,
            snapshot=snapshot,
            usage=summary.usage,
            record=summary.calls,
        )

    summary.findings = reviewed
    log.info(
        "supervisor.complete",
        adjudicated=summary.adjudicated,
        settled=summary.settled_without_llm,
        suppressed=summary.suppressed,
        rejected=summary.rejected_by_selfcheck,
        explained=summary.explained,
        calls=summary.usage.calls,
        tokens=summary.usage.prompt_tokens + summary.usage.completion_tokens,
    )
    return summary


def _propose_fixes(
    findings: list[Finding],
    *,
    provider: Provider,
    settings: Settings,
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    lineage: ColumnGraph | None,
    usage: Usage,
    record: list[ModelCall] | None = None,
    conventions: tuple[Convention, ...] = (),
    withheld: frozenset[str | None] = frozenset(),
) -> list[Finding]:
    """Attach corrected SQL where a model can write it, and nothing where it cannot.

    Only for findings that still stand and that name a fragment of SQL. A finding a
    specialist refuted needs no fix, and one about a config rather than a statement has
    no fragment to rewrite.
    """
    out: list[Finding] = []
    for finding in findings:
        if finding.suppressed_reason or not finding.evidence.sql_after:
            out.append(finding)
            continue
        # A model that writes to the reviewer does not get a rewrite proposed from the
        # same text: the fix seat reads the SQL, and the suggestion lands in a report a
        # person may paste back into the model.
        if finding.evidence.model_name in withheld:
            out.append(finding)
            continue
        pack = build_pack(
            finding,
            snapshot=snapshot,
            grains=grains,
            pr_description=None,
            needs=frozenset({Section.RELATED_SQL, Section.GRAIN}),
            lineage=lineage,
            conventions=conventions,
        )
        fixed = propose(
            finding, pack, provider=provider, settings=settings, usage=usage, record=record
        )
        out.append(finding.model_copy(update={"suggested_fix": fixed}) if fixed else finding)
    return out


def _intent_pass(
    provider: Provider,
    *,
    settings: Settings,
    findings: list[Finding],
    changed_models: tuple[str, ...],
    pr_description: str,
    snapshot: ProjectSnapshot,
    usage: Usage,
    record: list[ModelCall] | None = None,
) -> list[str]:
    """The one pass with no rule behind it — what the description does not mention.

    Uses the larger model: this is the call that needs judgement rather than a narrow
    check, and it happens once per review rather than once per finding.
    """
    from themis.llm.provider import LLMError
    from themis.review.specialists import INTENT_SCHEMA

    pack: ContextPack | None = build_intent_pack(
        findings,
        changed_models=changed_models,
        pr_description=pr_description,
        snapshot=snapshot,
        governed_tags=settings.governed_tags,
    )
    if pack is None:
        return []

    try:
        response = provider.complete(
            system=INTENT.system_prompt,
            prompt=pack.text,
            schema=INTENT_SCHEMA,
            model=settings.llm_supervisor_model,
        )
    except LLMError as exc:
        log.warning("intent.failed", error=str(exc)[:200])
        return []

    usage.add(response.usage)
    if record is not None:
        record.append(
            ModelCall(
                seat="intent",
                model=settings.llm_supervisor_model,
                context=pack.text,
                system=INTENT.system_prompt,
                response=dict(response.payload),
            )
        )
    raw = response.payload.get("undisclosed_changes")
    items: list[str] = []
    if isinstance(raw, list):
        items = [str(item).strip() for item in raw if str(item).strip()]

    if response.payload.get("description_covers_change"):
        # Said plainly rather than inferred from an empty list, so a model that answers
        # in prose does not have its "nothing was omitted" read as an omission.
        #
        # The boolean wins over the list, and it has to: the artefact this exists to
        # absorb *is* a non-empty list — "nothing was omitted" written as an item — so
        # a guard that kept the list whenever it had contents would let the false alarm
        # straight back in. The two cases are indistinguishable by shape.
        #
        # What is not acceptable is doing it silently. A model that sets the boolean and
        # then lists something substantive has contradicted itself, and that is either a
        # real catch being dropped or the artefact being absorbed as designed — which of
        # the two is not knowable from the counters, only from the text. So it is logged
        # in full, the way a rejected self-check verdict is.
        if items:
            log.warning("intent.discarded_by_boolean", items=items[:8])
        return []
    return items[:8]
