"""Building the evidence a specialist is allowed to see.

The pack is small on purpose, and the reason is not only cost. A model given a whole
file will reason about the whole file, and then report things the rule did not ask
about — which is how a focused reviewer turns into a generator of plausible commentary.
Narrowing the input is the cheapest available form of grounding.

Everything in a pack is a fact some deterministic stage established. Nothing is
inferred here, and the model is never handed the repository.
"""

from __future__ import annotations

from dataclasses import dataclass

from themis.models import Finding, Grain
from themis.snapshot import ProjectSnapshot

# Roughly four characters per token. Deliberately crude — the budget exists to stop a
# pack becoming a file, and precision would imply a control we do not have.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class ContextPack:
    """What one specialist call is given.

    ``finding`` is None for the intent pass, which is the one specialist with no rule
    behind it — it judges the change as a whole rather than adjudicating a flag.
    """

    text: str
    finding: Finding | None = None

    @property
    def approx_tokens(self) -> int:
        return len(self.text) // _CHARS_PER_TOKEN


def _sql_excerpt(sql: str | None, *, max_lines: int = 40) -> str | None:
    """A bounded excerpt of a model's SQL.

    Compiled dbt models run to hundreds of lines; a specialist judging one join does
    not need all of it, and including it would crowd out the evidence that matters.
    """
    if not sql:
        return None
    lines = sql.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = lines[: max_lines - 10]
    tail = lines[-10:]
    return "\n".join([*head, f"-- ... {len(lines) - max_lines} lines omitted ...", *tail])


def build_pack(
    finding: Finding,
    *,
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    pr_description: str | None = None,
) -> ContextPack:
    """Assemble the evidence pack for one finding."""
    model_name = finding.evidence.model_name
    model = snapshot.models.get(model_name)

    sections: list[str] = [
        "## The finding",
        f"rule: {finding.rule_id} ({finding.family})",
        f"model: {model_name}",
        f"title: {finding.title}",
        f"severity as flagged: {finding.severity}",
        f"why it was flagged: {finding.evidence.note or 'no note'}",
        "",
        "## What this would mean",
        finding.consequence,
    ]

    if finding.evidence.sql_after:
        sections += [
            "",
            "## The SQL that triggered it",
            "```sql",
            finding.evidence.sql_after,
            "```",
        ]

    grain = grains.get(model_name)
    if grain is not None:
        columns = ", ".join(grain.columns) or "unknown"
        line = f"grain of {model_name}: ({columns}) — established by {grain.source}"
        if grain.rows_per_key is not None:
            line += f", measured at {grain.rows_per_key:.2f} rows per key"
        sections += ["", "## Grain", line]

    # Upstream grains matter for fan-out judgements: whether a join multiplies rows
    # depends on the key of the table being joined, not of this model.
    if model is not None and model.depends_on_models:
        upstream_lines: list[str] = []
        for dependency in model.depends_on_models[:6]:
            name = dependency.split(".")[-1]
            upstream = grains.get(name)
            if upstream is None:
                continue
            columns = ", ".join(upstream.columns) or "could not be established"
            upstream_lines.append(f"- {name}: ({columns}) via {upstream.source}")
        if upstream_lines:
            sections += ["", "## Upstream grains", *upstream_lines]

    delta = finding.execution_delta
    if delta is not None and delta.is_material:
        measured = ["", "## Measured by building both revisions"]
        if delta.rows_before is not None and delta.rows_after is not None:
            measured.append(f"- rows: {delta.rows_before:,} -> {delta.rows_after:,}")
        for column, (before, after) in sorted(delta.sum_deltas.items()):
            if before != after:
                measured.append(f"- sum({column}): {before:,.2f} -> {after:,.2f}")
        sections += measured

    if finding.blast_radius:
        shown = ", ".join(finding.blast_radius[:8])
        sections += ["", f"## Downstream models ({len(finding.blast_radius)})", shown]

    if model is not None and model.tags:
        sections += ["", "## Tags", ", ".join(model.tags)]

    if pr_description:
        sections += ["", "## What the author said this change does", pr_description[:600]]

    return ContextPack(finding=finding, text="\n".join(sections))


def build_intent_pack(
    findings: list[Finding],
    *,
    changed_models: tuple[str, ...],
    pr_description: str,
    snapshot: ProjectSnapshot,
) -> ContextPack | None:
    """A single pack describing the whole change, for the intent pass.

    This is the one specialist without a rule behind it, so it gets breadth instead of
    depth: what changed, and what the author said they were doing.
    """
    if not pr_description.strip():
        return None

    lines = [
        "## What the author said this change does",
        pr_description[:1200],
        "",
        f"## Models this change affects ({len(changed_models)})",
        ", ".join(changed_models[:20]),
        "",
        "## What the automated checks found",
    ]
    if findings:
        for finding in findings[:12]:
            lines.append(f"- {finding.rule_id} on {finding.evidence.model_name}: {finding.title}")
    else:
        lines.append("- nothing")

    # Materialization and tag changes are the kind of thing a description routinely
    # omits and a reviewer would want stated.
    notable: list[str] = []
    for name in changed_models[:20]:
        model = snapshot.models.get(name)
        if model is None:
            continue
        if model.materialization == "incremental":
            notable.append(f"- {name} is incremental (strategy: {model.incremental_strategy})")
        if {"regulatory", "recon", "control"} & set(model.tags):
            notable.append(f"- {name} is tagged {', '.join(model.tags)}")
    if notable:
        lines += ["", "## Worth knowing about these models", *notable]

    return ContextPack(text="\n".join(lines))
