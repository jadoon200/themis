"""Machine-readable output, for anything that is not a person reading Markdown.

SARIF is JSON, but it is a *findings* format: it carries what a viewer needs to draw
an annotation on a line and drops everything else. A CI job deciding whether to block,
a dashboard trending false positives, or a script diffing two runs needs the parts
SARIF has no place for — the measured row counts and totals, the grain THEMIS derived
and how, what it could not check and why.

The triage decision travels with each finding rather than being applied by dropping
things, so a consumer can render the same report a reviewer sees or ignore the ranking
entirely and read everything. A format that silently omitted the demoted findings would
make "why didn't you flag X" unanswerable from the artifact.
"""

from __future__ import annotations

import json
from typing import Any

from themis.models import ExecutionDelta, Finding, Grain
from themis.report import redact as redaction
from themis.review.supervisor import ReviewSummary
from themis.rules.base import SkippedRule
from themis.triage.rubric import triage


def _finding(finding: Finding, *, score: float, subsumed_by: str | None) -> dict[str, Any]:
    evidence = finding.evidence
    return {
        "rule_id": finding.rule_id,
        "family": finding.family,
        "title": finding.title,
        "severity": finding.severity.value,
        "confidence": finding.confidence.value,
        "verdict": finding.verdict.value,
        "consequence": finding.consequence,
        "suggestion": finding.suggestion,
        "model": evidence.model_name,
        "file": evidence.file_path,
        "line": evidence.line,
        "note": evidence.note,
        "blast_radius": list(finding.blast_radius),
        "triage": {"score": round(score, 1), "subsumed_by": subsumed_by},
        # Counts only. A reviewer's note is free text about a specific change, so it
        # stays out of a machine-readable artifact that redaction is expected to make
        # safe to send elsewhere.
        "history": (
            None
            if finding.history is None
            else {
                "occurrences": finding.history.occurrences,
                "dismissed": finding.history.dismissed,
                "accepted": finding.history.accepted,
                "fixed": finding.history.fixed,
                "deferred": finding.history.deferred,
            }
        ),
        # Both reasons a finding may be set aside, kept distinct: a specialist refuted
        # it, or a more precise rule already said it.
        "suppressed_reason": finding.suppressed_reason,
        "llm_rationale": finding.llm_rationale,
        "suggested_fix": finding.suggested_fix,
    }


def _delta(delta: ExecutionDelta) -> dict[str, Any]:
    return {
        "model": delta.model_name,
        "rows_before": delta.rows_before,
        "rows_after": delta.rows_after,
        "row_delta": delta.row_delta,
        "sum_deltas": {k: list(v) for k, v in sorted(delta.sum_deltas.items())},
        "columns_added": list(delta.columns_added),
        "columns_removed": list(delta.columns_removed),
        "columns_retyped": {k: list(v) for k, v in sorted(delta.columns_retyped.items())},
        "is_material": delta.is_material,
        "build_error": delta.build_error,
        "failed_revision": delta.failed_revision,
        "build_skipped": delta.build_skipped,
    }


def _grain(grain: Grain) -> dict[str, Any]:
    return {
        "model": grain.model_name,
        "columns": list(grain.columns),
        "source": grain.source.value,
        "is_proven": grain.is_proven,
        "rows_per_key": grain.rows_per_key,
        "note": grain.note,
    }


def _model_layer(llm: ReviewSummary) -> dict[str, Any]:
    """What the model layer contributed, and what it cost.

    Carried because it is the part of a review a reader is most entitled to distrust.
    A consumer trending false positives needs to know whether a finding was adjudicated
    or settled without a model call, and `suppressed` is the number that would show this
    layer starting to hide things.
    """
    return {
        "adjudicated": llm.adjudicated,
        "settled_without_llm": llm.settled_without_llm,
        "suppressed": llm.suppressed,
        "rejected_by_selfcheck": llm.rejected_by_selfcheck,
        "explained": llm.explained,
        # The intent pass: what the author's description does not account for. It has no
        # rule behind it and so no finding to attach to, which is why it went missing
        # from this format on the first pass — and it is the one output here that a rule
        # could not have produced.
        "undisclosed_changes": list(llm.undisclosed),
        "calls": llm.usage.calls,
        "tokens": llm.usage.prompt_tokens + llm.usage.completion_tokens,
    }


def render(
    findings: list[Finding],
    *,
    skipped: list[SkippedRule] | None = None,
    grains: dict[str, Grain] | None = None,
    deltas: dict[str, ExecutionDelta] | None = None,
    models_reviewed: tuple[str, ...] = (),
    executed: bool = False,
    degraded_reason: str | None = None,
    governed_models: frozenset[str] = frozenset(),
    untested_grains: tuple[str, ...] = (),
    llm: ReviewSummary | None = None,
    seed_affected: dict[str, tuple[str, ...]] | None = None,
    # (kind, reason) for every way the review checked less than it was asked to.
    incomplete: tuple[tuple[str, str], ...] = (),
    # A salt to redact with, or None for the full report. See `report.redact`.
    redact: str | None = None,
) -> str:
    """One review as JSON, including what it could not check."""
    triaged = triage(findings, governed_models=governed_models)
    if redact is not None:
        salt = redact
        return json.dumps(
            {
                "schema_version": 1,
                "redacted": True,
                "models_reviewed": len(models_reviewed),
                "seeds_changed": len(seed_affected or {}),
                "executed": executed,
                "incomplete": sorted({kind for kind, _ in incomplete}),
                "findings": [
                    redaction.finding(
                        t.finding, salt=salt, score=t.score, subsumed_by=t.subsumed_by
                    )
                    for t in triaged
                ],
                "skipped_checks": [
                    {
                        "rule_id": s.rule_id,
                        "model": redaction.token(s.model_name, salt),
                        "reason": redaction.skip_reason(s.reason),
                    }
                    for s in (skipped or [])
                ],
                "grains": [
                    redaction.grain(g, salt=salt) for _, g in sorted((grains or {}).items())
                ],
                "execution_deltas": [
                    redaction.delta(d, salt=salt) for _, d in sorted((deltas or {}).items())
                ],
                "untested_grains": len(untested_grains),
                "model_layer": (
                    {
                        "adjudicated": llm.adjudicated,
                        "settled_without_llm": llm.settled_without_llm,
                        "suppressed": llm.suppressed,
                        "rejected_by_selfcheck": llm.rejected_by_selfcheck,
                        "explained": llm.explained,
                        "undisclosed_changes": len(llm.undisclosed),
                        "calls": llm.usage.calls,
                        "tokens": llm.usage.prompt_tokens + llm.usage.completion_tokens,
                    }
                    if llm is not None
                    else None
                ),
            },
            indent=2,
        )
    return json.dumps(
        {
            "schema_version": 1,
            "models_reviewed": list(models_reviewed),
            "seeds_changed": {
                seed: list(models) for seed, models in sorted((seed_affected or {}).items())
            },
            "executed": executed,
            # Never omitted. A report that hides its own blind spots reads exactly like
            # one that had none.
            "degraded_reason": degraded_reason,
            "incomplete": [{"kind": kind, "reason": reason} for kind, reason in incomplete],
            "findings": [
                _finding(t.finding, score=t.score, subsumed_by=t.subsumed_by) for t in triaged
            ],
            "skipped_checks": [
                {"rule_id": s.rule_id, "model": s.model_name, "reason": s.reason}
                for s in (skipped or [])
            ],
            "grains": [_grain(g) for _, g in sorted((grains or {}).items())],
            "execution_deltas": [_delta(d) for _, d in sorted((deltas or {}).items())],
            "untested_grains": list(untested_grains),
            # None rather than an empty object on a --no-llm run: a reader can tell a
            # review that had no model layer from one whose model layer did nothing.
            "model_layer": _model_layer(llm) if llm is not None else None,
        },
        indent=2,
    )
