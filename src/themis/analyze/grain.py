"""Derive each model's grain — its unique key — without relying on declared tests.

Many projects declare no uniqueness tests, so reading grain off ``schema.yml`` is
not an option. Everything downstream that matters (does this join fan out? does
this SUM double-count?) needs a grain to reason against, so THEMIS derives one.

Five sources, in descending confidence:

1. STRUCTURAL   -- proven from the model's own AST. ``GROUP BY a, b`` means the output
                   is unique on (a, b); so does ``SELECT DISTINCT``, and the
                   ``ROW_NUMBER() OVER (PARTITION BY k ...) = 1`` dedup idiom. This is
                   derivation, not inference, and it covers more than it first appears
                   because dbt DAGs are largely built from these three shapes.
2. CONFIG       -- incremental ``unique_key`` survives with zero tests; it is a config.
3. DECLARED_TEST-- read if present. Expected to be rare here.
4. PROPAGATED   -- inherited through the DAG: joining on a proven-unique key preserves
                   grain, joining on a non-unique key multiplies it, GROUP BY resets it.
5. HEURISTIC    -- naming only. Raises a question, never asserts.

Anything unresolved is UNKNOWN, and UNKNOWN escalates to a human rather than being
assumed safe. On a project with nothing declared, an over-confident default would be
the single most dangerous thing this tool could do.
"""

from __future__ import annotations

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql, select_from, select_joins
from themis.logging import get_logger
from themis.models import Grain, GrainSource
from themis.snapshot import ModelNode, ProjectSnapshot

log = get_logger(__name__)

# Suffixes that *suggest* a key. Only ever used to raise a question.
_KEY_SUFFIXES = ("_id", "_key", "_sk", "_pk", "_code")


def _column_names(expressions: list[exp.Expression]) -> tuple[str, ...]:
    """Best-effort column names from a GROUP BY or DISTINCT ON list.

    Positional group-by (``GROUP BY 1, 2``) resolves against the select list; anything
    that is a computed expression rather than a plain column is skipped, because a
    grain we cannot name is not one we can check a join against.
    """
    names: list[str] = []
    for expression in expressions:
        if isinstance(expression, exp.Column):
            names.append(expression.name)
        elif isinstance(expression, exp.Alias):
            names.append(expression.alias)
    return tuple(names)


def _resolve_positional_group_by(select: exp.Select, group: exp.Group) -> tuple[str, ...]:
    """Turn ``GROUP BY 1, 2`` into column names using the select list."""
    projections = select.expressions
    names: list[str] = []
    for expression in group.expressions:
        if isinstance(expression, exp.Literal) and expression.is_int:
            index = int(expression.name) - 1
            if 0 <= index < len(projections):
                target = projections[index]
                names.append(target.alias if isinstance(target, exp.Alias) else target.name)
    return tuple(names)


def _grain_of_select(
    select: exp.Select, ctes: dict[str, exp.Expression], depth: int = 0
) -> tuple[tuple[str, ...], str] | None:
    """Grain of one SELECT, following pass-through CTEs down to where it is set.

    dbt models are overwhelmingly written as ``with ... as (...) select * from ...``,
    so the grain-setting construct is almost never in the final projection — it is in
    the last CTE, and the outer select just passes it through. Reading only the
    outermost SELECT would report ``unknown`` for most of a real project.
    """
    if depth > 10:  # cyclic or pathological CTE nesting
        return None

    group = select.args.get("group")
    if isinstance(group, exp.Group) and group.expressions:
        names = _column_names(group.expressions) or _resolve_positional_group_by(select, group)
        if names:
            return names, "GROUP BY"

    if select.args.get("distinct"):
        names = _column_names(select.expressions)
        if names:
            return names, "SELECT DISTINCT"

    dedup = _row_number_dedup(select)
    if dedup is not None:
        return dedup, "ROW_NUMBER() dedup filtered to one row per partition"

    # Nothing here sets a grain. If this select merely passes a CTE through, the grain
    # is whatever that CTE established.
    inner = _passthrough_target(select, ctes)
    if inner is None:
        return None

    name, inner_select = inner
    resolved = _grain_of_select(inner_select, ctes, depth + 1)
    if resolved is None:
        return None

    columns, note = resolved
    if not _projection_covers(select, columns):
        # The outer projection drops part of the key, so the inner grain does not
        # survive. Claiming it anyway would assert uniqueness the rows do not have.
        return None
    return columns, f"{note} in CTE `{name}`"


def _passthrough_target(
    select: exp.Select, ctes: dict[str, exp.Expression]
) -> tuple[str, exp.Select] | None:
    """The CTE or subquery this select reads through without changing its grain.

    A join is disqualifying — a join is exactly where grain changes. A WHERE is not:
    filtering removes rows but cannot make a unique key non-unique.

    Inline subqueries count as well as named CTEs. ``select * from (select ...) as x``
    is the same pass-through written differently, and handling only the named form
    means a routine refactor makes a model's grain unprovable.
    """
    if select_joins(select):
        return None
    source = select_from(select)
    if source is None:
        return None

    table = source.this

    # Inline subquery: `from (select ...) as alias`.
    if isinstance(table, exp.Subquery):
        inner = table.this if isinstance(table.this, exp.Select) else table.find(exp.Select)
        if isinstance(inner, exp.Select):
            return (table.alias_or_name or "subquery", inner)
        return None

    if not isinstance(table, exp.Table):
        return None
    body = ctes.get(table.name)
    if body is None:
        return None
    inner = body if isinstance(body, exp.Select) else body.find(exp.Select)
    return (table.name, inner) if isinstance(inner, exp.Select) else None


def _projection_covers(select: exp.Select, columns: tuple[str, ...]) -> bool:
    """Whether every grain column survives this select's projection."""
    projected: set[str] = set()
    for expression in select.expressions:
        if isinstance(expression, exp.Star):
            return True
        if isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star):
            return True
        if isinstance(expression, exp.Alias):
            projected.add(expression.alias)
        elif isinstance(expression, exp.Column):
            projected.add(expression.name)
    return set(columns) <= projected


def structural_grain(sql: str, dialect: str = "trino") -> tuple[tuple[str, ...], str] | None:
    """Public entry point for structural derivation.

    Rules that need to compare a model's grain across revisions must go through this
    rather than re-reading the AST themselves. The CTE resolution below is the whole
    reason it works on real dbt models, and a rule that reimplements the easy version
    silently never fires.
    """
    return _structural_grain(sql, dialect)


def _structural_grain(sql: str, dialect: str) -> tuple[tuple[str, ...], str] | None:
    """Derive grain from the model's SQL, resolving through pass-through CTEs."""
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return None

    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None

    ctes = {cte.alias_or_name: cte.this for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
    return _grain_of_select(select, ctes)


def _row_number_dedup(scope: exp.Expression) -> tuple[str, ...] | None:
    """Detect ``row_number() over (partition by k ...)`` filtered to one row.

    Requires both halves: the window function *and* a predicate pinning it to 1.
    A ranked column that is never filtered does not deduplicate anything, and
    treating it as if it did would assert a grain the data does not have.
    """
    ranked: dict[str, tuple[str, ...]] = {}
    for window in scope.find_all(exp.Window):
        fn = window.this
        if not isinstance(fn, exp.RowNumber):
            continue
        partition = _column_names(list(window.args.get("partition_by") or []))
        if not partition:
            continue
        parent = window.parent
        alias = parent.alias if isinstance(parent, exp.Alias) else None
        if alias:
            ranked[alias] = partition

    if not ranked:
        return None

    for predicate in scope.find_all(exp.EQ, exp.LTE):
        left, right = predicate.this, predicate.expression
        if (
            isinstance(left, exp.Column)
            and left.name in ranked
            and isinstance(right, exp.Literal)
            and right.name == "1"
        ):
            return ranked[left.name]
    return None


def _declared_grain(model_name: str, snapshot: ProjectSnapshot) -> tuple[str, ...] | None:
    """Grain from a declared uniqueness test, if the project happens to have one."""
    columns: list[str] = []
    for test in snapshot.tests:
        if test.model_name != model_name:
            continue
        if test.test_name in ("unique", "unique_combination_of_columns"):
            columns.extend(test.columns)
    return tuple(dict.fromkeys(columns)) or None


def _heuristic_grain(model: ModelNode) -> tuple[str, ...] | None:
    """A single obvious-looking key column. Weak by construction."""
    candidates = [
        column.name for column in model.columns if column.name.lower().endswith(_KEY_SUFFIXES)
    ]
    # Exactly one candidate is a question worth asking; several is a guess, and a
    # guessed composite key is worse than admitting we do not know.
    return (candidates[0],) if len(candidates) == 1 else None


def infer_model_grain(
    model: ModelNode, snapshot: ProjectSnapshot, *, dialect: str = "trino"
) -> Grain:
    """Derive one model's grain from the highest-confidence source available."""
    sql = model.analysable_sql
    if sql is not None:
        structural = _structural_grain(sql, dialect)
        if structural is not None:
            columns, note = structural
            return Grain(
                model_name=model.name,
                columns=columns,
                source=GrainSource.STRUCTURAL,
                note=note,
            )

    if model.unique_key:
        return Grain(
            model_name=model.name,
            columns=model.unique_key,
            source=GrainSource.CONFIG,
            note="incremental unique_key config",
        )

    declared = _declared_grain(model.name, snapshot)
    if declared is not None:
        return Grain(
            model_name=model.name,
            columns=declared,
            source=GrainSource.DECLARED_TEST,
            note="declared uniqueness test in schema.yml",
        )

    heuristic = _heuristic_grain(model)
    if heuristic is not None:
        return Grain(
            model_name=model.name,
            columns=heuristic,
            source=GrainSource.HEURISTIC,
            note="column naming only — not asserted, needs confirmation",
        )

    return Grain(
        model_name=model.name,
        columns=(),
        source=GrainSource.UNKNOWN,
        note=_unknown_reason(model, snapshot),
    )


def _unknown_reason(model: ModelNode, snapshot: ProjectSnapshot) -> str:
    """Say precisely why a grain could not be derived.

    The cases need different actions from the reader, so collapsing them into one
    message would send people to fix the wrong thing. The partial-build case is the
    one that misleads hardest: a manifest left behind by `dbt build --select ...` has
    compiled SQL for the selected nodes only, and telling someone to run `dbt compile`
    when they already did reads as the tool being broken.
    """
    if model.is_seed:
        return "seed data, not SQL — grain cannot be derived, only measured"
    if model.analysable_sql is None:
        if snapshot.has_compiled_sql:
            return (
                "no compiled SQL for this model, though others have it — this manifest "
                "came from a selected build; recompile the whole project"
            )
        return "no compiled SQL available — run `dbt compile`, not `dbt parse`"
    return "no GROUP BY, DISTINCT, dedup pattern, unique_key or test to derive from"


def _propagate(model: ModelNode, grains: dict[str, Grain], dialect: str) -> Grain | None:
    """Inherit grain from a single upstream model when this one only passes it through.

    A pass-through is a model with exactly one upstream and no grain-changing
    construct of its own. Anything with a join is excluded: a join is precisely where
    grain changes, and inheriting across one would assert the fan-out away.
    """
    upstreams = [u.split(".")[-1] for u in model.depends_on_models]
    if len(upstreams) != 1:
        return None
    parent = grains.get(upstreams[0])
    if parent is None or not parent.is_proven:
        return None

    sql = model.analysable_sql
    if sql is None:
        return None
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return None
    if list(tree.find_all(exp.Join)):
        return None

    return Grain(
        model_name=model.name,
        columns=parent.columns,
        source=GrainSource.PROPAGATED,
        note=f"passes through {parent.model_name} unchanged (single upstream, no join)",
    )


def infer_grains(snapshot: ProjectSnapshot, *, dialect: str = "trino") -> dict[str, Grain]:
    """Derive grain for every model, then propagate through pass-through models."""
    grains: dict[str, Grain] = {
        name: infer_model_grain(model, snapshot, dialect=dialect)
        for name, model in snapshot.models.items()
    }

    # Propagation is iterative: a chain of pass-throughs resolves one link per pass.
    # Bounded by depth so a cyclic manifest cannot spin here.
    for _ in range(10):
        changed = False
        for name, grain in grains.items():
            if grain.source is not GrainSource.UNKNOWN:
                continue
            inherited = _propagate(snapshot.models[name], grains, dialect)
            if inherited is not None:
                grains[name] = inherited
                changed = True
        if not changed:
            break

    unknown = sum(1 for g in grains.values() if g.source is GrainSource.UNKNOWN)
    log.info("grain.inferred", models=len(grains), unknown=unknown)
    return grains
