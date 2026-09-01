"""Render a review as Markdown.

Written for someone deciding whether to approve a PR, so: ranked hardest-first, the
money consequence stated in prose rather than implied, and measured evidence shown as
numbers wherever Stage 3 produced any. Skipped checks are listed too — a clean report
that quietly ran half the rules is worse than no report.
"""

from __future__ import annotations

from themis.models import Confidence, Finding, Severity
from themis.rules.base import SkippedRule

_SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.HIGH: 1,
    Severity.MEDIUM: 2,
    Severity.LOW: 3,
    Severity.INFO: 4,
}

_SEVERITY_LABEL = {
    Severity.CRITICAL: "Critical",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
    Severity.INFO: "Info",
}

_CONFIDENCE_NOTE = {
    Confidence.MEASURED: "measured by running both revisions",
    Confidence.PROVEN: "derived from the SQL structure",
    Confidence.LIKELY: "strong static signal",
    Confidence.POSSIBLE: "worth confirming",
}


def rank_key(finding: Finding) -> tuple[int, int, int]:
    """Severity first, then reach, then how sure we are.

    Blast radius is negated so a wider reach sorts earlier: two findings of equal
    severity are not equally urgent if one touches forty models and the other one.
    """
    confidence_rank = {
        Confidence.MEASURED: 0,
        Confidence.PROVEN: 1,
        Confidence.LIKELY: 2,
        Confidence.POSSIBLE: 3,
    }
    return (
        _SEVERITY_ORDER[finding.severity],
        -len(finding.blast_radius),
        confidence_rank[finding.confidence],
    )


def _format_delta(finding: Finding) -> list[str]:
    """Measured evidence, when Stage 3 produced any."""
    delta = finding.execution_delta
    if delta is None:
        return []
    lines: list[str] = ["", "**Measured by running both revisions:**", ""]
    if delta.build_error:
        lines.append(f"- Build failed: `{delta.build_error.strip()[:300]}`")
        return lines
    if delta.rows_before is not None and delta.rows_after is not None:
        change = delta.row_delta or 0
        pct = (change / delta.rows_before * 100) if delta.rows_before else 0.0
        lines.append(
            f"- Row count: {delta.rows_before:,} → {delta.rows_after:,} ({change:+,}, {pct:+.1f}%)"
        )
    for column, (before, after) in sorted(delta.sum_deltas.items()):
        if before == after:
            continue
        amount_change = after - before
        amount_pct = (amount_change / before * 100) if before else 0.0
        lines.append(
            f"- `sum({column})`: {before:,.2f} → {after:,.2f} "
            f"({amount_change:+,.2f}, {amount_pct:+.1f}%)"
        )
    if delta.columns_removed:
        lines.append(f"- Columns removed: {', '.join(f'`{c}`' for c in delta.columns_removed)}")
    if delta.columns_added:
        lines.append(f"- Columns added: {', '.join(f'`{c}`' for c in delta.columns_added)}")
    for column, (before_type, after_type) in sorted(delta.columns_retyped.items()):
        lines.append(f"- `{column}` retyped: `{before_type}` → `{after_type}`")
    return lines


def _render_finding(index: int, finding: Finding) -> str:
    lines = [
        f"### {index}. {finding.title}",
        "",
        f"**{_SEVERITY_LABEL[finding.severity]}** · `{finding.rule_id}` · "
        f"`{finding.evidence.model_name}` · {_CONFIDENCE_NOTE[finding.confidence]}",
        "",
        finding.consequence,
    ]
    lines.extend(_format_delta(finding))

    if finding.blast_radius:
        shown = ", ".join(f"`{m}`" for m in finding.blast_radius[:8])
        more = f" and {len(finding.blast_radius) - 8} more" if len(finding.blast_radius) > 8 else ""
        lines += ["", f"**Downstream:** {shown}{more}"]

    if finding.evidence.sql_after or finding.evidence.note:
        lines += ["", "<details><summary>Evidence</summary>", ""]
        if finding.evidence.note:
            lines += [finding.evidence.note, ""]
        if finding.evidence.file_path:
            location = finding.evidence.file_path
            if finding.evidence.line:
                location += f":{finding.evidence.line}"
            lines += [f"`{location}`", ""]
        if finding.evidence.sql_after:
            lines += ["```sql", finding.evidence.sql_after, "```"]
        lines += ["", "</details>"]

    if finding.suggestion:
        lines += ["", f"**Suggested:** {finding.suggestion}"]
    if finding.llm_rationale:
        lines += ["", f"**Review note:** {finding.llm_rationale}"]
    return "\n".join(lines)


def render(
    findings: list[Finding],
    *,
    skipped: list[SkippedRule] | None = None,
    models_reviewed: int = 0,
    executed: bool = False,
) -> str:
    """Render the full report."""
    ranked = sorted(findings, key=rank_key)
    counts = {severity: 0 for severity in Severity}
    for finding in ranked:
        counts[finding.severity] += 1

    lines = ["## THEMIS review", ""]

    if not ranked:
        lines += [
            f"No findings across {models_reviewed} changed model(s)."
            + (
                "" if executed else " Static analysis only — pass `--execute` to verify by running."
            ),
        ]
    else:
        summary = ", ".join(
            f"{counts[s]} {_SEVERITY_LABEL[s].lower()}" for s in Severity if counts[s]
        )
        lines += [
            f"{len(ranked)} finding(s) across {models_reviewed} changed model(s): {summary}.",
            "",
        ]
        measured = sum(1 for f in ranked if f.confidence is Confidence.MEASURED)
        if measured:
            lines += [
                f"{measured} of these were confirmed by running both revisions, not inferred.",
                "",
            ]
        lines += ["---", ""]
        for index, finding in enumerate(ranked, start=1):
            lines += [_render_finding(index, finding), "", "---", ""]

    if skipped:
        by_reason: dict[str, list[str]] = {}
        for skip in skipped:
            by_reason.setdefault(skip.reason, []).append(f"{skip.rule_id}/{skip.model_name}")
        lines += [
            "",
            "<details><summary>",
            f"{len(skipped)} check(s) could not run",
            "</summary>",
            "",
        ]
        for reason, items in sorted(by_reason.items()):
            lines += [f"- **{reason}** — {len(items)} check(s)"]
        lines += ["", "</details>"]

    return "\n".join(lines).rstrip() + "\n"
