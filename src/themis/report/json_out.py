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
) -> str:
    """One review as JSON, including what it could not check."""
    triaged = triage(findings, governed_models=governed_models)
    return json.dumps(
        {
            "schema_version": 1,
            "models_reviewed": list(models_reviewed),
            "executed": executed,
            # Never omitted. A report that hides its own blind spots reads exactly like
            # one that had none.
            "degraded_reason": degraded_reason,
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
        },
        indent=2,
    )
