"""F6 — contracts, lineage and blast radius.

These are the changes that break something else. A column removed from a mart is
harmless in isolation and fatal to the four models selecting it, and the diff shows
only the deletion.

The other half is lineage integrity. Replacing a ``ref()`` with a literal table name
compiles and runs, so nothing complains — but the model drops out of the DAG, dbt no
longer knows to build it in order, and in the wrong environment it reads production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql, select_from
from themis.models import Confidence, Evidence, Finding, Severity
from themis.rules.base import Rule, RuleContext

FAMILY = "F6"

# A three-part or two-part literal table reference in raw model source. dbt models
# address other models through ref(); a literal name here means the edge is gone.
_LITERAL_TABLE = re.compile(
    r"\bfrom\s+([`\"']?\w+[`\"']?\.[`\"']?\w+[`\"']?(?:\.[`\"']?\w+[`\"']?)?)",
    re.IGNORECASE,
)
_JINJA_CALL = re.compile(r"\{\{[^}]*\}\}")


def _severity_for(ctx: RuleContext, base: Severity) -> Severity:
    if ctx.is_governed or ctx.reaches_exposure:
        return Severity.CRITICAL if base is Severity.HIGH else Severity.HIGH
    return base


def _output_columns(sql: str, dialect: str) -> set[str] | None:
    """Column names a model exposes, or None when the AST alone cannot say.

    ``SELECT *`` returns **None**, not an empty set. The distinction is the whole
    rule: an empty set is a claim that the model emits nothing, and subtracting it
    from the previous column list reports every column as removed. That is exactly
    the confident false positive this guard was written to prevent — it was simply
    applied on one side of the subtraction and not the other, so adding a star to a
    projection produced six critical findings about columns that had not moved.
    """
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return None

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None

    names: set[str] = set()
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            return None
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return None
        if isinstance(projection, exp.Alias):
            names.add(projection.alias)
        elif isinstance(projection, exp.Column):
            names.add(projection.name)
    return names


def _resolved_outputs(ctx: RuleContext, *, before: bool) -> set[str] | None:
    """A model's real column list, with stars expanded against the project schema.

    Consulted only when the AST could not answer, because building the graph costs a
    project-wide pass and a star in a final projection is uncommon. When it can answer,
    it answers better than the AST ever could: it knows what `select *` actually
    resolves to.
    """
    index = ctx.lineage
    if index is None:
        return None
    graph = index.before if before else index.after
    columns = graph.outputs.get(ctx.model_name)
    return set(columns) if columns else None


def _name_search(ctx: RuleContext, column: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
    """Downstream models whose SQL contains the column's name.

    The weaker answer, kept for models column lineage could not resolve. It is wrong in
    both directions -- blind to a consumer reading through ``select *``, and fooled by a
    same-named column arriving from somewhere else -- so it never claims to be proof.
    """
    pattern = re.compile(rf"\b{re.escape(column)}\b")
    found: list[str] = []
    for name in candidates:
        model = ctx.after_snapshot.models.get(name)
        sql = model.analysable_sql if model else None
        if sql and pattern.search(sql):
            found.append(name)
    return tuple(found)


@dataclass(frozen=True)
class _ColumnImpact:
    """Who reads a column, and how confident we are that the list is complete."""

    models: tuple[str, ...]
    # Downstream columns computed from this one, named as model.column.
    columns: tuple[str, ...] = ()
    # Models that name the column without carrying it forward — join keys, predicates.
    referenced_by: tuple[str, ...] = ()
    confidence: Confidence = Confidence.LIKELY
    source: str = "name search"

    def note(self, column: str) -> str:
        parts: list[str] = []
        if self.columns:
            shown = ", ".join(self.columns[:8])
            more = f" (+{len(self.columns) - 8} more)" if len(self.columns) > 8 else ""
            parts.append(f"feeds {shown}{more}")
        if self.referenced_by:
            parts.append(f"joined or filtered on by {', '.join(self.referenced_by)}")
        if not parts:
            parts.append(f"referenced by {', '.join(self.models)}")
        return f"`{column}` " + "; ".join(parts) + f" — resolved by {self.source}"


@dataclass
class ColumnRemovedWithConsumersRule(Rule):
    """A column disappeared from a model that other models read.

    Only reported when something downstream actually selects it — a column removed
    from a leaf model breaks nothing, and flagging it would be noise.
    """

    rule_id: str = field(init=False, default="F6001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        before_columns = _output_columns(before_sql, ctx.dialect)
        after_columns = _output_columns(after_sql, ctx.dialect)
        # A star on either side means the AST cannot name the columns. Column lineage
        # can, because it derives each model's schema in dependency order — so ask it
        # before giving up, and give up rather than guess if it cannot say either.
        if before_columns is None:
            before_columns = _resolved_outputs(ctx, before=True)
        if after_columns is None:
            after_columns = _resolved_outputs(ctx, before=False)
        if before_columns is None or after_columns is None:
            # Reported by F6004, which flags the star itself, so the reviewer is not
            # left with silence here.
            return []

        removed = before_columns - after_columns
        if not removed:
            return []

        findings: list[Finding] = []
        for column in sorted(removed):
            impact = self._consumers(ctx, column)
            if not impact.models:
                continue
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title=f"Column `{column}` removed but still selected downstream",
                    severity=_severity_for(ctx, self.severity),
                    confidence=impact.confidence,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        column_name=column,
                        note=impact.note(column),
                    ),
                    consequence=(
                        f"{len(impact.models)} downstream model(s) read `{column}` from "
                        "this model. The ones that name it stop compiling. The ones "
                        "that select through a star keep building and quietly produce "
                        "one column fewer, which is the harder half to notice — "
                        "nothing errors, and a report loses a field."
                    ),
                    suggestion=(
                        "Remove or repoint the downstream references in the same "
                        "change, or keep the column and deprecate it separately."
                    ),
                    blast_radius=impact.models,
                )
            )
        return findings

    @staticmethod
    def _consumers(ctx: RuleContext, column: str) -> _ColumnImpact:
        """Downstream models that read the column, resolved as precisely as possible.

        Column lineage is asked first, and asked of the **before** revision: the column
        exists there, which is the only revision in which "who reads it" has an answer.

        The fallback is a word search over downstream SQL, kept because a model whose
        lineage could not be resolved must still be checked. It is materially worse in
        both directions -- it cannot see a consumer that reads the column through
        ``select *``, and it counts a same-named column from an unrelated upstream as a
        consumer -- so a finding that rests on it says so and carries lower confidence.
        """
        downstream = ctx.after_snapshot.downstream_of(ctx.model_name)
        index = ctx.lineage
        graph = index.before if index is not None else None

        if graph is not None and graph.is_traced(ctx.model_name):
            refs = graph.consumers_of(ctx.model_name, column)
            referenced = graph.referencing_models(ctx.model_name, column)
            models = set(graph.consumer_models(ctx.model_name, column))
            # Only the downstream models lineage could not resolve need searching, and
            # only those cost the finding its confidence.
            blind = tuple(name for name in downstream if not graph.is_traced(name))
            source = "column lineage"
            confidence = Confidence.PROVEN
            if blind:
                models |= set(_name_search(ctx, column, blind))
                confidence = Confidence.LIKELY
                source = f"column lineage, with {len(blind)} unresolved model(s) name-searched"
            return _ColumnImpact(
                models=tuple(sorted(models)),
                columns=tuple(str(ref) for ref in refs),
                referenced_by=referenced,
                confidence=confidence,
                source=source,
            )

        searched = _name_search(ctx, column, downstream)
        return _ColumnImpact(searched, confidence=Confidence.LIKELY, source="name search")


@dataclass
class HardcodedTableReferenceRule(Rule):
    """A literal table name where a ``ref()`` belongs.

    The model still compiles, so nothing complains. But the DAG edge is gone: dbt no
    longer knows to build the upstream first, `state:modified+` no longer reaches this
    model, and the literal name is environment-specific — in dev it reads production.
    """

    rule_id: str = field(init=False, default="F6002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None:
            return []

        after_refs = self._literal_refs(ctx.after.raw_sql)
        if not after_refs:
            return []
        before_refs = self._literal_refs(ctx.before.raw_sql) if ctx.before else set()

        findings: list[Finding] = []
        for reference in sorted(after_refs - before_refs):
            findings.append(
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title=f"Hardcoded table reference `{reference}`",
                    severity=_severity_for(ctx, self.severity),
                    confidence=Confidence.LIKELY,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        sql_after=reference,
                        note="literal table name where a ref() or source() is expected",
                    ),
                    consequence=(
                        "This bypasses the DAG. dbt no longer knows to build the "
                        "upstream first, so ordering is not guaranteed; the model drops "
                        "out of lineage and impact analysis; and because the name is "
                        "environment-specific, a development run can read production "
                        "data without any indication that it did."
                    ),
                    suggestion=(
                        "Replace with `{{ ref('...') }}` or `{{ source(...) }}` "
                        "so the edge is restored."
                    ),
                    blast_radius=ctx.blast_radius,
                )
            )
        return findings

    @staticmethod
    def _literal_refs(raw_sql: str) -> set[str]:
        # Strip Jinja first: `from {{ ref('x') }}` must not read as a literal.
        stripped = _JINJA_CALL.sub(" __jinja__ ", raw_sql)
        return {match.strip("`\"'") for match in _LITERAL_TABLE.findall(stripped)}


@dataclass
class ContractViolatedRule(Rule):
    """A model with an enforced contract no longer emits its declared columns."""

    rule_id: str = field(init=False, default="F6003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.CRITICAL)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None or not ctx.after.contract_enforced:
            return []
        sql = ctx.after.analysable_sql
        if sql is None:
            return []

        produced = _output_columns(sql, ctx.dialect)
        if not produced:
            return []  # SELECT * — cannot tell, and must not guess

        declared = {c.name for c in ctx.after.columns}
        missing = declared - produced
        if not missing:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title=f"Contract violated: {len(missing)} declared column(s) not produced",
                severity=self.severity,
                confidence=Confidence.LIKELY,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"declared but not selected: {', '.join(sorted(missing))}",
                ),
                consequence=(
                    "The model declares an enforced contract, which is a promise to "
                    "its consumers about the columns it emits. The build will fail on "
                    "the contract check — and consumers were written against the "
                    "promise, not against this SQL."
                ),
                suggestion=(
                    "Either produce the declared columns or update the contract "
                    "deliberately, treating it as a breaking change for consumers."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


@dataclass
class SelectStarIntroducedRule(Rule):
    """``SELECT *`` in a model that previously named its columns.

    It makes the model's output depend on whatever upstream happens to emit, so an
    unrelated upstream change silently alters this model's schema.
    """

    rule_id: str = field(init=False, default="F6004")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.MEDIUM)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.before is None or ctx.after is None:
            return []
        before_sql = ctx.before.analysable_sql
        after_sql = ctx.after.analysable_sql
        if before_sql is None or after_sql is None:
            return []

        # An empty column set is how _output_columns reports a star projection.
        before_star = not _output_columns(before_sql, ctx.dialect)
        after_star = not _output_columns(after_sql, ctx.dialect)
        if after_star and not before_star and _final_select_is_star(after_sql, ctx.dialect):
            return [
                Finding(
                    rule_id=self.rule_id,
                    family=self.family,
                    title="Final projection changed to SELECT *",
                    severity=_severity_for(ctx, self.severity),
                    confidence=Confidence.PROVEN,
                    evidence=Evidence(
                        model_name=ctx.model_name,
                        file_path=ctx.after.file_path,
                        note="explicit column list replaced by a star projection",
                    ),
                    consequence=(
                        "This model's schema now follows whatever its upstream emits. "
                        "An unrelated change upstream will add, remove or reorder "
                        "columns here without touching this file, and any downstream "
                        "contract or consumer sees the drift instead."
                    ),
                    suggestion="Name the columns explicitly so the schema is stated here.",
                    blast_radius=ctx.blast_radius,
                )
            ]
        return []


def _final_select_is_star(sql: str, dialect: str) -> bool:
    """Whether the outermost projection is literally a star.

    A pass-through `select * from final` is idiomatic dbt and not the problem this
    rule is about, so it only fires when the star reads directly from a relation.
    """
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return False
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not isinstance(select, exp.Select):
        return False
    if not any(isinstance(e, exp.Star) for e in select.expressions):
        return False
    source = select_from(select)
    if source is None or not isinstance(source.this, exp.Table):
        return False
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    return source.this.name not in ctes


@dataclass
class DataDependentSqlRule(Rule):
    """This model's compiled SQL is assembled from the result of a query.

    A macro that runs a query during compilation and builds SQL from its rows produces
    compiled code that changes whenever the *data* changes. Two compilations of
    identical code differ, so the semantic diff attributes those differences to the
    change under review — and every finding on the model inherits that doubt.

    Reporting it is the honest response. Silently diffing such a model produces
    confident findings about edits nobody made.
    """

    rule_id: str = field(init=False, default="F6005")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.MEDIUM)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        if ctx.after is None:
            return []
        affected = ctx.after_snapshot.data_dependent_models()
        macros = affected.get(ctx.model_name)
        if not macros:
            return []

        return [
            Finding(
                rule_id=self.rule_id,
                family=self.family,
                title="Compiled SQL is generated from query results",
                severity=self.severity,
                confidence=Confidence.PROVEN,
                evidence=Evidence(
                    model_name=ctx.model_name,
                    file_path=ctx.after.file_path,
                    note=f"built by macro(s) that query at compile time: {', '.join(macros)}",
                ),
                consequence=(
                    "This model's SQL is assembled at compile time from the rows of a "
                    "query, so it changes when that data changes even if nobody edits "
                    "the code. Structural findings on this model — including any "
                    "reported above — may describe differences the author did not "
                    "make, and a clean result does not mean the model is unchanged."
                ),
                suggestion=(
                    "Review this model against its source data as well as its diff. "
                    "Running with --execute compares actual results, which is not "
                    "affected by the generated SQL differing."
                ),
                blast_radius=ctx.blast_radius,
            )
        ]


RULES: tuple[Rule, ...] = (
    ColumnRemovedWithConsumersRule(),
    HardcodedTableReferenceRule(),
    ContractViolatedRule(),
    SelectStarIntroducedRule(),
    DataDependentSqlRule(),
)
