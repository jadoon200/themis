"""Reports that can leave the building.

A review of a financial institution's dbt project is made of that project: its SQL, its
model and column names, and — with execution — its row counts and totals. None of it can
be shared outside the team that owns it, yet the only way to calibrate this tool on real
work is to see how it behaved there.

So a redacted report is *built* from an allowlist of fields known to carry no project
detail, never produced by deleting fields from the full one. Deletion fails open: a field
added to the report next month would travel by default. Construction fails closed.

What survives: rule ids, families, severity, confidence, verdicts, counts, triage scores,
whether something was measured, built, suppressed or fixed. What does not: SQL, titles and
prose (they name models and columns), file paths, measured values, model-layer text.
Names that must stay joinable across a report — a model a finding and a delta both refer
to — become short hashes. Without a salt those are guessable from a list of likely names,
so ``THEMIS_REDACT_SALT`` should be set to something the recipient does not know.
"""

from __future__ import annotations

import hashlib
from typing import Any

from themis.models import ExecutionDelta, Finding, Grain, sum_moved


def token(name: str, salt: str) -> str:
    """A stable, opaque stand-in for a model, column or file name."""
    digest = hashlib.sha256(f"{salt}\x1f{name}".encode()).hexdigest()[:10]
    return f"n_{digest}"


def finding(item: Finding, *, salt: str, score: float, subsumed_by: str | None) -> dict[str, Any]:
    delta = item.execution_delta
    return {
        "rule_id": item.rule_id,
        "family": item.family,
        "severity": item.severity.value,
        "confidence": item.confidence.value,
        "verdict": item.verdict.value,
        "model": token(item.evidence.model_name, salt),
        "blast_radius_count": len(item.blast_radius),
        "triage": {"score": round(score, 1), "subsumed_by": subsumed_by},
        "measured": delta is not None,
        "suppressed": item.suppressed_reason is not None,
        "has_llm_rationale": item.llm_rationale is not None,
        "has_suggested_fix": item.suggested_fix is not None,
    }


def delta(item: ExecutionDelta, *, salt: str) -> dict[str, Any]:
    row_delta = item.row_delta
    return {
        "model": token(item.model_name, salt),
        "is_material": item.is_material,
        # The direction of a movement is a property of the change; its size is data.
        "rows": None
        if row_delta is None
        else ("up" if row_delta > 0 else "down" if row_delta < 0 else "same"),
        "sums_moved": sum(
            1 for before, after in item.sum_deltas.values() if sum_moved(before, after)
        ),
        "columns_added": len(item.columns_added),
        "columns_removed": len(item.columns_removed),
        "columns_retyped": len(item.columns_retyped),
        "failed_revision": item.failed_revision,
        "build_skipped": item.build_skipped,
    }


def grain(item: Grain, *, salt: str) -> dict[str, Any]:
    return {
        "model": token(item.model_name, salt),
        "source": item.source.value,
        "is_proven": item.is_proven,
        "key_columns": len(item.columns),
        "duplicated": None if item.rows_per_key is None else item.rows_per_key > 1.0,
    }


def skip_reason(reason: str) -> str:
    """A skip reason, minus anything an exception message might have carried with it."""
    return "rule raised an error" if reason.startswith("rule raised") else reason
