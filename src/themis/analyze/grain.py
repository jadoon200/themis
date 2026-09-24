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

A dbt snapshot has two grains, and confusing them is the mistake to avoid. Its *query*
should be unique on the snapshot's ``unique_key`` — ``query_grain`` answers whether it
is. Its *table* keeps a row per version, so it is unique on the key plus
``dbt_valid_from``, never on the key alone. A model reading it gets the key back only
by taking one version (``where dbt_valid_to is null``); see ``analyze.history``.
"""

from __future__ import annotations

from sqlglot import exp

from themis.analyze import history as snapshot_history
from themis.analyze.parse import ParseError, parse_sql, select_from, select_joins
from themis.logging import get_logger
from themis.models import Grain, GrainSource
from themis.snapshot import ModelNode, ProjectSnapshot

log = get_logger(__name__)

# Suffixes that *suggest* a key. Only ever used to raise a question.
_KEY_SUFFIXES = ("_id", "_key", "_sk", "_pk", "_code")


def _as_select(node: exp.Expression | None) -> exp.Select | None:
    """The SELECT a node *is*, looking through parentheses — never one it merely contains.

    ``node.find(exp.Select)`` answered a different question, and it was the wrong one for
    a set operation: the first branch of a ``UNION ALL`` is a select, and its grain was
    reported as the grain of the union that duplicates its rows.
    """
    while isinstance(node, (exp.Subquery, exp.Paren)):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def _output_name(select: exp.Select, expression: exp.Expression) -> str | None:
    """The name a grouped or distinct expression has in this select's output, if any.

    A key has to be something the model emits. ``group by account_id`` projected as
    ``account_id as acct`` is unique on ``acct``; ``group by date_trunc('month', d)``
    projected as ``period`` is unique on ``period``; an expression the projection never
    names cannot be checked against a join key at all.
    """
    if isinstance(expression, exp.Literal) and expression.is_int:
        index = int(expression.name) - 1
        if 0 <= index < len(select.expressions):
            target = select.expressions[index]
            return target.alias_or_name or None
        return None
    for projection in select.expressions:
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        same_column = (
            isinstance(inner, exp.Column)
            and isinstance(expression, exp.Column)
            and inner.name == expression.name
        )
        if same_column or inner == expression:
            return projection.alias_or_name or None
    return None


def _group_by_grain(select: exp.Select) -> tuple[str, ...] | None:
    """The key a GROUP BY proves, or None unless every grouped expression is named.

    Skipping what cannot be named used to shrink the key instead: ``group by a,
    date_trunc('month', d)`` became unique on ``(a)``, which it is not. A key that is a
    strict subset of the real one is a false proof, and F1 trusts proofs.

    ``ROLLUP``, ``CUBE`` and ``GROUPING SETS`` emit subtotal rows alongside the detail,
    so no list of grouped columns is a key for them.
    """
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return None
    if any(group.args.get(arg) for arg in ("rollup", "cube", "grouping_sets")):
        return None
    if not group.expressions:
        return None
    names: list[str] = []
    for expression in group.expressions:
        name = _output_name(select, expression)
        if name is None:
            return None
        names.append(name)
    return tuple(dict.fromkeys(names))


def _distinct_grain(select: exp.Select) -> tuple[str, ...] | None:
    """The key a SELECT DISTINCT proves: every projected column, or nothing.

    Dropping an unnamed expression from the list narrowed the key exactly as it did for
    GROUP BY. A star cannot be enumerated here either.
    """
    if not select.args.get("distinct"):
        return None
    names: list[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            return None
        name = projection.alias_or_name
        if not name or not isinstance(projection, (exp.Alias, exp.Column)):
            return None
        names.append(name)
    return tuple(names) or None


def _conjuncts(expression: exp.Expression) -> list[exp.Expression]:
    """The top-level AND-ed terms of a predicate. A term under OR or NOT is not one."""
    while isinstance(expression, exp.Paren):
        expression = expression.this
    if isinstance(expression, exp.And):
        return [*_conjuncts(expression.this), *_conjuncts(expression.expression)]
    return [expression]


def _pinned_to_one(select: exp.Select) -> set[str]:
    """Columns this select's own WHERE requires to equal 1 (or be at most 1).

    Only this select's WHERE, and only its top-level conjuncts. A predicate inside a
    subquery filters a different relation, and ``rn = 1 or flag`` keeps rows the rank
    does not.
    """
    where = select.args.get("where")
    if not isinstance(where, exp.Where):
        return set()
    pinned: set[str] = set()
    for term in _conjuncts(where.this):
        if not isinstance(term, (exp.EQ, exp.LTE, exp.LT)):
            continue
        left, right = term.this, term.expression
        if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            left, right = right, left
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Literal)):
            continue
        limit = "2" if isinstance(term, exp.LT) else "1"
        if right.name == limit:
            pinned.add(left.name)
    return pinned


def _rank_partitions(select: exp.Select) -> dict[str, tuple[str, ...]]:
    """``row_number() over (partition by k ...) as rn`` projections of this select alone."""
    ranked: dict[str, tuple[str, ...]] = {}
    for projection in select.expressions:
        if not isinstance(projection, exp.Alias):
            continue
        window = projection.this
        if not (isinstance(window, exp.Window) and isinstance(window.this, exp.RowNumber)):
            continue
        partition = window.args.get("partition_by") or []
        names = tuple(col.name for col in partition if isinstance(col, exp.Column))
        # A partition on an expression cannot be named as a key, and dropping it would
        # shrink the key the same way an unnamed GROUP BY expression did.
        if names and len(names) == len(partition):
            ranked[projection.alias] = names
    return ranked


def _grain_of_select(
    select: exp.Select, ctes: dict[str, exp.Expression], depth: int = 0
) -> tuple[tuple[str, ...], str] | None:
    """Grain of one SELECT, following pass-through CTEs down to where it is set.

    dbt models are overwhelmingly written as ``with ... as (...) select * from ...``,
    so the grain-setting construct is almost never in the final projection — it is in
    the last CTE, and the outer select just passes it through. Reading only the
    outermost SELECT would report ``unknown`` for most of a real project.

    Every construct is read from this select's own clauses. The dedup idiom used to be
    found by searching the whole subtree, so a ``row_number() ... = 1`` in a CTE proved
    the grain of an outer select that joined afterwards and fanned the rows back out.
    """
    if depth > 10:  # cyclic or pathological CTE nesting
        return None

    grouped = _group_by_grain(select)
    if grouped is not None:
        return grouped, "GROUP BY"

    distinct = _distinct_grain(select)
    if distinct is not None:
        return distinct, "SELECT DISTINCT"

    # Nothing here sets a grain by itself. If this select passes a relation through —
    # no join — its grain is that relation's, or, when this select's WHERE pins a rank
    # the relation computes, one row per the rank's partition.
    inner = _passthrough_target(select, ctes)
    if inner is None:
        return None
    name, inner_select = inner

    for column in sorted(_pinned_to_one(select)):
        partition = _rank_partitions(inner_select).get(column)
        if partition is not None and _projection_covers(select, partition):
            return partition, f"ROW_NUMBER() in `{name}` filtered to one row per partition"

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
    filtering removes rows but cannot make a unique key non-unique. A set operation is
    disqualifying too: a relation that is a ``UNION ALL`` is not a select at all, and
    reading its first branch as though it were reports a key the union duplicates.

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
        inner = _as_select(table)
        return (table.alias_or_name or "subquery", inner) if inner is not None else None

    if not isinstance(table, exp.Table):
        return None
    body = ctes.get(table.name)
    if body is None:
        return None
    inner = _as_select(body)
    return (table.name, inner) if inner is not None else None


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

    # The statement itself, never a select found somewhere inside it: a model whose
    # final statement is a UNION ALL has no key, whatever its first branch has.
    select = _as_select(tree)
    if select is None:
        return None

    ctes = {cte.alias_or_name: cte.this for cte in tree.find_all(exp.CTE) if cte.alias_or_name}
    return _grain_of_select(select, ctes)


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
    if model.is_snapshot:
        return _snapshot_table_grain(model, snapshot, {}, dialect)
    # A seed's grain is counted, not derived: the data is in the repository, so the
    # columns that identify a row can be read off the CSV. It ranks with a warehouse
    # measurement because it is one — over the whole file, or it is not reported at all.
    if model.seed_key:
        return Grain(
            model_name=model.name,
            columns=model.seed_key,
            source=GrainSource.MEASURED,
            note="counted unique across every row of the seed CSV",
        )

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
        return (
            "seed data — no column, or pair of columns, is unique across the CSV "
            "(measurements are never taken as identifiers)"
        )
    if model.analysable_sql is None:
        if snapshot.has_compiled_sql:
            return (
                "no compiled SQL for this model, though others have it — this manifest "
                "came from a selected build; recompile the whole project"
            )
        return "no compiled SQL available — run `dbt compile`, not `dbt parse`"
    return "no GROUP BY, DISTINCT, dedup pattern, unique_key or test to derive from"


def query_grain(
    model: ModelNode,
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    *,
    dialect: str = "trino",
) -> Grain:
    """The grain of a snapshot's query: what its ``unique_key`` has to identify.

    Derived as a model's would be, less the two sources that would answer the question
    with itself: the ``unique_key`` is the claim under test, and a test declared on a
    snapshot is about its table, not its query.
    """
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
    inherited = _propagate(model, grains, dialect, snapshot)
    if inherited is not None:
        return inherited
    return Grain(
        model_name=model.name,
        columns=(),
        source=GrainSource.UNKNOWN,
        note=_unknown_reason(model, snapshot),
    )


def _snapshot_table_grain(
    model: ModelNode, snapshot: ProjectSnapshot, grains: dict[str, Grain], dialect: str
) -> Grain:
    """A snapshot's table: one row per version of each key."""
    history = model.history
    key_columns = snapshot_history.key_columns(model, dialect=dialect)
    if history is None or not key_columns:
        return Grain(
            model_name=model.name,
            columns=(),
            source=GrainSource.UNKNOWN,
            note="a snapshot with no unique_key a column can be read from, so nothing says "
            "what a version is of",
        )
    columns = (*key_columns, history.valid_from)
    key = ", ".join(key_columns)
    query = query_grain(model, snapshot, grains, dialect=dialect)
    if query.is_proven and set(query.columns) <= set(key_columns):
        return Grain(
            model_name=model.name,
            columns=columns,
            source=GrainSource.PROPAGATED,
            note=(
                f"a snapshot keeps a row per version: its query is unique on ({key}) "
                f"[{query.source.value}], and each change adds a version from "
                f"{history.valid_from}"
            ),
        )
    if query.is_proven:
        # The query is provably unique on something the key does not cover, so the key
        # does not identify a row and neither does the key plus a version time. Claiming
        # either would tell every join onto this table that it is safe.
        return Grain(
            model_name=model.name,
            columns=(),
            source=GrainSource.UNKNOWN,
            note=(
                f"the snapshot's unique_key ({key}) does not identify a row of its query, "
                f"which is unique on ({', '.join(query.columns)})"
            ),
        )
    return Grain(
        model_name=model.name,
        columns=columns,
        source=GrainSource.CONFIG,
        note=(
            f"a snapshot keeps a row per version of its unique_key ({key}); that its "
            "query is unique on that key is not established"
        ),
    )


def _propagate(
    model: ModelNode,
    grains: dict[str, Grain],
    dialect: str,
    project: ProjectSnapshot | None = None,
) -> Grain | None:
    """Inherit grain from a single upstream model when this one only passes it through.

    A pass-through is a model with exactly one upstream and no grain-changing
    construct of its own. Anything with a join is excluded: a join is precisely where
    grain changes, and inheriting across one would assert the fan-out away.

    Reading a snapshot is the one pass-through that can narrow its parent's key: taking
    one version (``where dbt_valid_to is null``, or an as-of predicate) leaves a row per
    key, so the key alone is inherited. Without that, the key plus the version time is.
    """
    upstreams = [u.split(".")[-1] for u in model.depends_on_models]
    if len(upstreams) != 1:
        return None
    parent = grains.get(upstreams[0])
    if parent is None or not parent.is_proven:
        return None
    parent_node = project.models.get(upstreams[0]) if project is not None else None

    sql = model.analysable_sql
    if sql is None:
        return None
    try:
        tree = parse_sql(sql, dialect=dialect)
    except ParseError:
        return None
    if list(tree.find_all(exp.Join)):
        return None

    # A set operation is the other way grain changes without a join. `UNION ALL` of one
    # upstream has a single dependency, no join, and a projection that carries the key
    # straight through -- and it still duplicates every row, so the parent's key is not
    # this model's key. Inheriting anyway is worse than admitting we do not know: since
    # PROPAGATED counts as proven, F1 reads the inherited key as "the join key covers a
    # proven unique key: safe" and a real fan-out is never reported. Refused for EXCEPT
    # and INTERSECT too, which change the population rather than passing it through.
    if list(tree.find_all(exp.SetOperation)):
        return None

    # The key must still be emitted. A pass-through that projects a subset can drop
    # part of the parent's key, and the rows are then no longer unique on what is
    # left — inheriting it anyway would assert uniqueness the data does not have.
    # The structural path has always checked this; propagation did not, which is the
    # difference between a grain that can be trusted and one that merely usually holds.
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not isinstance(select, exp.Select):
        return None

    if (
        parent_node is not None
        and parent_node.is_snapshot
        and snapshot_history.key_columns(parent_node, dialect=dialect)
        and snapshot_history.restricted_to_one_version(tree, parent_node)
    ):
        key = snapshot_history.key_columns(parent_node, dialect=dialect)
        if not _projection_covers(select, key):
            return None
        return Grain(
            model_name=model.name,
            columns=key,
            source=GrainSource.PROPAGATED,
            note=(
                f"takes one version of each key from snapshot {parent.model_name} "
                f"(single upstream, no join), whose rows are {parent.source.value}"
            ),
        )

    if not _projection_covers(select, parent.columns):
        return None

    return Grain(
        model_name=model.name,
        columns=parent.columns,
        source=GrainSource.PROPAGATED,
        note=(
            f"passes through {parent.model_name} unchanged (single upstream, no join), "
            f"whose key is {parent.source.value}"
        ),
    )


def infer_grains(snapshot: ProjectSnapshot, *, dialect: str = "trino") -> dict[str, Grain]:
    """Derive grain for every model, then propagate through pass-through models."""
    grains: dict[str, Grain] = {
        name: infer_model_grain(model, snapshot, dialect=dialect)
        for name, model in snapshot.models.items()
    }

    # What each model derived on its own, before inheriting anything.
    initial = dict(grains)

    # Propagation is iterative: a chain of pass-throughs resolves one link per pass.
    # Bounded by depth so a cyclic manifest cannot spin here.
    for _ in range(10):
        changed = False
        for name, grain in grains.items():
            model = snapshot.models[name]
            if model.is_snapshot:
                # Its query's grain can arrive from upstream on any pass, and the table's
                # grain follows from it — so it is recomputed, never frozen at first sight.
                table = _snapshot_table_grain(model, snapshot, grains, dialect)
                if table != grain:
                    grains[name] = table
                    changed = True
                continue
            # A naming heuristic is replaceable too. It says "column naming only — not
            # asserted", and a key carried unchanged from a parent that was *counted* is a
            # stronger statement than a name. It matters on exactly the models it sounds
            # like it would not: the FX reference table was guessed as (currency_code)
            # when the seed it passes through is measured (currency_code, rate_date), and
            # the difference between those two is the whole of the fan-out this tool
            # exists to catch. Propagation still refuses a join, a set operation, or a key
            # the projection drops.
            #
            # A model reading a snapshot is re-derived on every pass, because the
            # snapshot's grain can still change under it — and a key inherited from a
            # snapshot whose key turned out not to identify a row must not outlive that.
            inheritable = initial[name].source in (GrainSource.UNKNOWN, GrainSource.HEURISTIC)
            if not inheritable:
                continue
            if grain.source not in (GrainSource.UNKNOWN, GrainSource.HEURISTIC) and not (
                snapshot_history.snapshot_parents(model, snapshot)
            ):
                continue
            inherited = _propagate(model, grains, dialect, snapshot) or initial[name]
            if inherited != grain:
                grains[name] = inherited
                changed = True
        if not changed:
            break

    unknown = sum(1 for g in grains.values() if g.source is GrainSource.UNKNOWN)
    log.info("grain.inferred", models=len(grains), unknown=unknown)
    return grains
