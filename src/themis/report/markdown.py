"""Render a review as Markdown.

Written for someone deciding whether to approve a PR, so: ranked hardest-first, the
money consequence stated in prose rather than implied, and measured evidence shown as
numbers wherever Stage 3 produced any. Skipped checks are listed too — a clean report
that quietly ran half the rules is worse than no report.
"""

from __future__ import annotations

from themis.models import Confidence, Finding, Severity, sum_moved
from themis.rules.base import SkippedRule
from themis.triage.rubric import triage

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
        if not sum_moved(before, after):
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

    # A column that starts or stops being NULL is the signature of a join-semantics
    # change — an outer join tightened, or a COALESCE removed. The aggregate was
    # already being computed; not showing it was pure waste.
    for column, (was, now) in sorted(delta.null_rate_deltas.items()):
        if abs(now - was) < 0.001:
            continue
        lines.append(f"- `{column}` null rate: {was:.1%} → {now:.1%}")
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
    macro_affected: dict[str, tuple[str, ...]] | None = None,
    degraded_reason: str | None = None,
    # Models reviewed here whose grain is derivable and which nothing asserts.
    untested_grains: tuple[str, ...] = (),
    # Models tagged for reconciliation or reporting, so triage can weight what lands
    # in one. Empty means the caller did not say, not that none exist.
    governed_models: frozenset[str] = frozenset(),
) -> str:
    """Render the full report."""
    # Stage 4. Recall-first rules over-flag on purpose; this is the half that pays for
    # it, by ranking rather than by silence. A demoted finding is still in the report.
    triaged = triage(findings, governed_models=governed_models)
    ranked = [t.finding for t in triaged if not t.demoted]
    demoted = [t for t in triaged if t.demoted]
    counts = {severity: 0 for severity in Severity}
    for finding in ranked:
        counts[finding.severity] += 1

    lines = ["## THEMIS review", ""]

    # State the macro expansion before anything else. Otherwise a reviewer who changed
    # one macro file sees findings against models they never touched and reasonably
    # concludes the tool is confused.
    for macro, models in sorted((macro_affected or {}).items()):
        if not models:
            continue
        shown = ", ".join(f"`{m}`" for m in models[:10])
        more = f" and {len(models) - 10} more" if len(models) > 10 else ""
        lines += [
            f"Macro `{macro}` changed — its compiled SQL reaches "
            f"**{len(models)} model(s)**: {shown}{more}. Those models are reviewed here "
            "even though their own files are unchanged.",
            "",
        ]

    if degraded_reason:
        # Only claim checks were skipped when some were. Not every degradation costs a
        # check -- a base read from a different place than asked for costs none -- and
        # sending the reader to an empty list at the end reads as the tool guessing.
        pointer = ""
        if skipped:
            pointer = " Some checks below could not run — see the end of this report."
        lines += [f"**Grounding is degraded:** {degraded_reason}.{pointer}", ""]

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

    if ranked and not executed:
        lines += [
            "",
            "_Findings above are inferred from the SQL. Re-run with `--execute` to "
            "build both revisions and measure the actual row-count and total impact._",
        ]

    if demoted:
        lines += [
            "",
            "<details><summary>",
            f"{len(demoted)} finding(s) a more specific check already covers",
            "</summary>",
            "",
            "_Not dismissed — the same fact, stated with less information than the "
            "finding named beside it. Kept so nothing the rules saw is lost._",
            "",
        ]
        for item in demoted:
            lines.append(
                f"- `{item.finding.rule_id}` on `{item.finding.evidence.model_name}`: "
                f"{item.finding.title} — covered by `{item.subsumed_by}`"
            )
        lines += ["", "</details>"]

    if untested_grains:
        # One line, not a finding per model. On a project with no test coverage a
        # per-model finding would fire on everything and bury the real ones.
        shown = ", ".join(f"`{m}`" for m in untested_grains[:8])
        more = f" and {len(untested_grains) - 8} more" if len(untested_grains) > 8 else ""
        lines += [
            "",
            f"_{len(untested_grains)} model(s) reviewed here have a grain THEMIS can "
            f"derive and nothing asserts: {shown}{more}. "
            "`themis suggest-tests --yaml` prints the assertions._",
        ]

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
