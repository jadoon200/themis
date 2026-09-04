"""Stage 4 — deciding what a reviewer should read first, and what can wait.

The rules are written for recall: they over-flag on purpose, because a rule that stays
quiet on a fan-out costs a restatement while one that fires on a safe change costs a
triage step. That trade is only honest if the second half exists. Without it the
reviewer gets the over-flagging and none of the suppression, which is how a change with
one real problem arrives as seven findings and the one that matters ranks last.

Two mechanisms, both deterministic and both explainable. Nothing here is learned, and
nothing is deleted.

**Subsumption.** `F2001` says a predicate changed. That is true of a removed
`is_incremental()` guard, a narrowed lookback, a `current_date` filter and a partition
column wrapped in a function — and in each of those a more specific rule already says
*which* predicate and *why it matters*. The general finding is not wrong; it is the
same fact stated with less information. It gets demoted beneath the finding that
subsumes it, with the relationship named.

**Scoring.** A transparent weighted sum over things earlier stages already computed.
The point is not the number but that its parts are visible: a reviewer who disagrees
with the ranking can see exactly which component produced it. That is also why there is
no model here. An opaque score gating a merge is not a reviewable statement, and in a
regulated environment "the ranker put it seventh" is not an answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from themis.models import Confidence, Finding, Severity

# A general rule, the specific rules that say the same thing better, and why. Kept as
# an explicit table rather than inferred from rule metadata: each entry is a claim
# about two rules describing one edit, and that claim should be written down and
# arguable rather than emerging from a heuristic nobody can check.
_SUBSUMED_BY: dict[str, tuple[frozenset[str], str]] = {
    "F2001": (
        frozenset({"F4002", "F5001", "F5004", "F5005", "F8003"}),
        "the predicate that changed is the one this finding already names",
    ),
    "F1001": (
        frozenset({"F8002"}),
        "an always-true join condition is why the join fans out",
    ),
}

_SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.CRITICAL: 100.0,
    Severity.HIGH: 60.0,
    Severity.MEDIUM: 30.0,
    Severity.LOW: 10.0,
    Severity.INFO: 2.0,
}

# Confidence multiplies rather than adds. A critical nobody can substantiate should not
# outrank a high that execution demonstrated.
_CONFIDENCE_WEIGHT: dict[Confidence, float] = {
    Confidence.MEASURED: 1.0,
    Confidence.PROVEN: 0.9,
    Confidence.LIKELY: 0.7,
    Confidence.POSSIBLE: 0.4,
}

# Reach matters, but with sharply diminishing returns: the step from one downstream
# model to four is the interesting one. Forty rather than thirty changes nothing about
# what a reviewer should do.
_REACH_STEPS: tuple[tuple[int, float], ...] = ((20, 16.0), (5, 13.0), (1, 8.0))

_GOVERNED_BONUS = 25.0


def calibrate(findings: list[Finding]) -> list[Finding]:
    """Cap severity at what the evidence supports.

    Every family escalates a high to critical when the change reaches a governed or
    exposure-facing model, and several rules start at critical. On a regulatory mart
    that makes most of what fires critical, and a level almost everything reaches
    stops telling a reviewer anything — least of all which one to open first.

    So critical means one thing: a wrong number in a reported figure, *demonstrated*.
    Anything still inferred caps at high, however bad the class would be if real. An
    unmeasured critical is a prediction, and predictions and measurements should not
    share a word in a report someone signs.

    Applied centrally rather than by editing twenty-nine rules, because the rules
    encode how bad a defect class is — which is right — and this encodes how much of
    that the evidence currently supports, which is a different question and one that
    changes as execution runs.
    """
    out: list[Finding] = []
    for finding in findings:
        if finding.severity is Severity.CRITICAL and finding.confidence is not Confidence.MEASURED:
            out.append(
                finding.model_copy(
                    update={
                        "severity": Severity.HIGH,
                        "suggestion": finding.suggestion,
                    }
                )
            )
        else:
            out.append(finding)
    return out


@dataclass(frozen=True)
class Triaged:
    """One finding, ranked, with the ranking's reasoning attached."""

    finding: Finding
    score: float
    components: tuple[str, ...]
    subsumed_by: str | None = None

    @property
    def demoted(self) -> bool:
        return self.subsumed_by is not None

    @property
    def reason(self) -> str:
        return "; ".join(self.components)


def _reach_points(count: int) -> float:
    for threshold, points in _REACH_STEPS:
        if count >= threshold:
            return points
    return 0.0


def _score(finding: Finding, *, governed: bool) -> tuple[float, tuple[str, ...]]:
    severity = _SEVERITY_WEIGHT.get(finding.severity, 10.0)
    confidence = _CONFIDENCE_WEIGHT.get(finding.confidence, 0.5)
    reach = _reach_points(len(finding.blast_radius))

    total = severity * confidence + reach + (_GOVERNED_BONUS if governed else 0.0)
    components = [
        f"{finding.severity.value} ({severity:.0f}) "
        f"x {finding.confidence.value} ({confidence:.1f})",
    ]
    if reach:
        components.append(f"reaches {len(finding.blast_radius)} model(s) (+{reach:.0f})")
    if governed:
        components.append(f"lands in a governed model (+{_GOVERNED_BONUS:.0f})")
    return total, tuple(components)


def triage(
    findings: list[Finding], *, governed_models: frozenset[str] = frozenset()
) -> list[Triaged]:
    """Rank findings, and demote those a more specific finding already covers.

    Demoted, never dropped. The recall-first bargain is that over-flagging is paid for
    in ranking rather than in silence, and a finding deleted here would be a finding
    the reviewer could not have asked about later.
    """
    fired_per_model: dict[str, set[str]] = {}
    for finding in findings:
        fired_per_model.setdefault(finding.evidence.model_name, set()).add(finding.rule_id)

    out: list[Triaged] = []
    for finding in findings:
        model = finding.evidence.model_name
        governed = model in governed_models or any(
            name in governed_models for name in finding.blast_radius
        )
        score, components = _score(finding, governed=governed)

        subsumed_by: str | None = None
        entry = _SUBSUMED_BY.get(finding.rule_id)
        if entry is not None:
            specific, why = entry
            covering = sorted(specific & fired_per_model.get(model, set()))
            if covering:
                subsumed_by = covering[0]
                components = (*components, f"covered by {subsumed_by}: {why}")

        out.append(
            Triaged(
                finding=finding,
                score=score,
                components=components,
                subsumed_by=subsumed_by,
            )
        )

    # Demoted findings sort below everything else regardless of score: their whole
    # point is that a better statement of the same fact is already in the report.
    out.sort(key=lambda t: (t.demoted, -t.score, t.finding.rule_id))
    return out
