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
from enum import StrEnum

from sqlglot import exp

from themis.analyze.lineage import ColumnGraph
from themis.analyze.parse import ParseError, parse_sql
from themis.models import Finding, Grain
from themis.snapshot import ModelNode, ProjectSnapshot

# Roughly four characters per token. Deliberately crude — the budget exists to stop a
# pack becoming a file, and precision would imply a control we do not have.
_CHARS_PER_TOKEN = 4


class Section(StrEnum):
    """A kind of evidence a pack can carry.

    Specialists ask for what their question actually turns on. A pack built the same
    way for everyone has to include everything anyone might need, and then the money
    reviewer reads upstream grains it has no use for while the incremental reviewer
    cannot see the config it is being asked about. Narrow input is the cheapest
    grounding available, and it stops being narrow the moment it is shared.
    """

    # The SQL of the other model the finding turns on — the one being joined to.
    RELATED_SQL = "related_sql"
    # This model's derived key.
    GRAIN = "grain"
    # The keys of what it reads. Whether a join multiplies rows depends on these.
    UPSTREAM_GRAINS = "upstream_grains"
    # Materialization, incremental strategy, unique key, partitioning, hooks.
    CONFIG = "config"
    # Declared column types. Whether money is decimal or binary floating point.
    COLUMN_TYPES = "column_types"
    # Which downstream columns actually read the one that changed.
    COLUMN_CONSUMERS = "column_consumers"
    # Models downstream, and any exposure the change reaches.
    BLAST_RADIUS = "blast_radius"
    # regulatory / recon / control.
    TAGS = "tags"
    # Catalogs and joins, for cost and federation questions.
    ENGINE_SHAPE = "engine_shape"


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


def _config_section(model: ModelNode) -> list[str]:
    """The configuration an incremental judgement actually turns on.

    Absent until now, which meant the incremental specialist was asked whether a
    strategy change was safe while being shown no strategy, no unique key and no
    schema-change policy. It could only restate the rule it was given.
    """
    lines = [f"- materialized: {model.materialization}"]
    if model.incremental_strategy:
        lines.append(f"- incremental_strategy: {model.incremental_strategy}")
    if model.unique_key:
        lines.append(f"- unique_key: {', '.join(model.unique_key)}")
    else:
        lines.append("- unique_key: none configured")
    if model.on_schema_change:
        lines.append(f"- on_schema_change: {model.on_schema_change}")
    if model.partitioned_by:
        lines.append(f"- partitioned_by: {model.partitioned_by}")
    return lines


def _column_type_section(model: ModelNode) -> list[str]:
    """Declared column types, for judging whether money is exact.

    Only what the project declares. An undeclared type is reported as unknown rather
    than guessed, because "this column is a DOUBLE" is precisely the claim that must
    not be invented.
    """
    typed = [c for c in model.columns if c.data_type]
    if not typed:
        return ["- no column types are declared for this model"]
    return [f"- {column.name}: {column.data_type}" for column in typed[:25]]


def _engine_shape_section(sql: str | None) -> list[str]:
    """Catalogs and joins, counted rather than described.

    A federated join is the one Trino cannot push down, and whether a query crosses
    catalogs is a fact about its table references — so it is counted here rather than
    left for the model to infer from SQL it may only partly see.
    """
    if not sql:
        return []
    try:
        tree = parse_sql(sql)
    except ParseError:
        return []
    catalogs = {
        table.args["catalog"].name
        for table in tree.find_all(exp.Table)
        if isinstance(table.args.get("catalog"), exp.Identifier)
    }
    joins = len(list(tree.find_all(exp.Join)))
    lines = [f"- joins in this model: {joins}"]
    if catalogs:
        lines.append(f"- catalogs referenced: {', '.join(sorted(catalogs))}")
        if len(catalogs) > 1:
            lines.append("- this query spans more than one catalog, so it cannot be pushed down")
    return lines


def _column_consumer_section(
    finding: Finding, lineage: ColumnGraph | None, model_name: str
) -> list[str]:
    """Who actually reads the column this finding is about.

    Traced through the SQL rather than found by searching for the name, so a model
    with a same-named column of its own is not counted and one reading through
    ``select *`` is not missed.
    """
    if lineage is None:
        return []
    column = finding.evidence.column_name
    if not column:
        return []
    if not lineage.is_traced(model_name):
        reason = lineage.unresolved.get(model_name, "not traced")
        return [f"- lineage for {model_name} is unresolved ({reason}); nobody knows what reads it"]

    feeds = lineage.consumers_of(model_name, column)
    referenced = lineage.referencing_models(model_name, column)
    lines: list[str] = []
    if feeds:
        lines.append(f"- carried forward into: {', '.join(str(ref) for ref in feeds[:10])}")
    if referenced:
        lines.append(f"- joined on or filtered by (produces no column): {', '.join(referenced)}")
    if not lines:
        lines.append(f"- nothing downstream reads `{column}`; lineage resolved and found no reader")
    return lines


def build_pack(
    finding: Finding,
    *,
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    pr_description: str | None = None,
    needs: frozenset[Section] | None = None,
    lineage: ColumnGraph | None = None,
) -> ContextPack:
    """Assemble the evidence pack for one finding.

    ``needs`` is what the specialist asked for. Passing None gives everything, which is
    what a caller with no specialist in hand should get — but the point of the argument
    is that a reviewer reads only what its question turns on.
    """
    wanted = frozenset(Section) if needs is None else needs
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

    # The model the finding turns on. For a fan-out this is the joined table, and its
    # SQL is what settles whether the join key is actually unique — without it a
    # specialist can only repeat that the derived grain is unproven, which is what it
    # was already told.
    related = finding.evidence.related_model
    if Section.RELATED_SQL in wanted and related and related != model_name:
        related_model = snapshot.models.get(related)
        related_sql = _sql_excerpt(related_model.analysable_sql if related_model else None)
        if related_sql:
            sections += [
                "",
                f"## The SQL of `{related}`, the model being joined to",
                "```sql",
                related_sql,
                "```",
            ]

    grain = grains.get(model_name)
    if Section.GRAIN in wanted and grain is not None:
        columns = ", ".join(grain.columns) or "unknown"
        line = f"grain of {model_name}: ({columns}) — established by {grain.source}"
        if grain.rows_per_key is not None:
            line += f", measured at {grain.rows_per_key:.2f} rows per key"
        sections += ["", "## Grain", line]

    # Upstream grains matter for fan-out judgements: whether a join multiplies rows
    # depends on the key of the table being joined, not of this model.
    if Section.UPSTREAM_GRAINS in wanted and model is not None and model.depends_on_models:
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

    if Section.BLAST_RADIUS in wanted and finding.blast_radius:
        shown = ", ".join(finding.blast_radius[:8])
        sections += ["", f"## Downstream models ({len(finding.blast_radius)})", shown]

    if Section.TAGS in wanted and model is not None and model.tags:
        sections += ["", "## Tags", ", ".join(model.tags)]

    if model is not None:
        if Section.CONFIG in wanted:
            sections += ["", "## How this model is built", *_config_section(model)]
        if Section.COLUMN_TYPES in wanted:
            sections += ["", "## Declared column types", *_column_type_section(model)]
        if Section.ENGINE_SHAPE in wanted:
            shape = _engine_shape_section(model.analysable_sql)
            if shape:
                sections += ["", "## How the engine will run this", *shape]

    if Section.COLUMN_CONSUMERS in wanted:
        consumers = _column_consumer_section(finding, lineage, model_name)
        if consumers:
            sections += ["", "## What reads this column, traced through the SQL", *consumers]

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
