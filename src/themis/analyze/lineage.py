"""Column-level lineage — which column feeds which, across the whole project.

Impact analysis one level up is model-granular: "this model changed, fourteen models
are downstream". That over-states almost every change, because thirteen of those
fourteen never touch the column that moved. The question a reviewer actually asks is
*"I removed `term_months` — who reads it?"*, and answering it by searching downstream
SQL for the word gets both halves wrong. It misses the consumer that reads the column
through ``select *`` — which, in a dbt project built out of pass-through CTEs, is most
of them — and it invents consumers that happen to have a same-named column of their
own from somewhere else entirely.

So THEMIS resolves it properly, in two passes:

1. **Schema derivation.** Walk the DAG in dependency order, qualifying each model's
   compiled SQL against the schema built so far. That expands ``select *`` into real
   column names, which is what makes the next pass possible at all. Roots need no
   declared columns: a staging model that names its columns explicitly in one CTE
   supplies the schema for everything above it.
2. **Edge extraction.** For each output column, ``sqlglot.lineage`` traces back through
   CTEs, subqueries and renames to the base relations it reads. Mapping those relations
   to model names turns per-model traces into one project-wide column graph.

Renames are followed, so ``revenue_usd`` in a regulatory mart is known to be
``fct_revenue.amount_usd`` under a different name — the case that matters most, since
the name a reviewer greps for stops existing halfway down the DAG.

Where a model cannot be resolved — unparseable SQL, or a star over a relation whose
columns nothing in the project declares — it is recorded in ``unresolved`` rather than
treated as having no columns. A model missing from the graph must read as *unknown*,
never as *not a consumer*: silence from a lineage tool is exactly how a breaking change
gets approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage as sqlglot_lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope
from sqlglot.schema import MappingSchema

from themis.analyze.parse import ParseError, parse_sql
from themis.logging import get_logger
from themis.snapshot import ModelNode, ProjectSnapshot

log = get_logger(__name__)

# Tracing every column of every model is quadratic in a wide DAG. The graph is built
# over the models a review actually asks about, so this only bounds pathological
# models -- a 400-column mart traced column by column adds seconds for no new insight.
_MAX_COLUMNS_PER_MODEL = 200


@dataclass(frozen=True, order=True)
class ColumnRef:
    """One column of one model. The node type of the graph."""

    model: str
    column: str

    def __str__(self) -> str:
        return f"{self.model}.{self.column}"


@dataclass
class ColumnGraph:
    """Project-wide column dependencies, in both directions.

    ``outputs`` is useful on its own: it is the derived column list for every model,
    including the ones whose projection is a star, which no other part of THEMIS can
    produce without running the project.
    """

    outputs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # A column -> the columns it reads from upstream models.
    reads: dict[ColumnRef, frozenset[ColumnRef]] = field(default_factory=dict)
    # The reverse: a column -> the downstream columns computed from it.
    feeds: dict[ColumnRef, frozenset[ColumnRef]] = field(default_factory=dict)
    # A model -> every upstream column it names, projected or not. Join keys and
    # filter predicates live here and nowhere else: they break the model when removed
    # while contributing no column to its output, so projection edges never see them.
    uses: dict[str, frozenset[ColumnRef]] = field(default_factory=dict)
    # The reverse: a column -> the models that name it.
    used_by: dict[ColumnRef, frozenset[str]] = field(default_factory=dict)
    # Model -> why its columns could not be traced. Read as "unknown", not "none".
    unresolved: dict[str, str] = field(default_factory=dict)
    # Models whose edges were never extracted because nothing asked about them.
    # Distinct from unresolved: not attempted is not the same as attempted and failed.
    untraced: set[str] = field(default_factory=set)

    def is_traced(self, model: str) -> bool:
        """Whether an empty answer about this model can be trusted."""
        return model not in self.unresolved and model not in self.untraced

    def consumers_of(self, model: str, column: str, *, depth: int = 10) -> tuple[ColumnRef, ...]:
        """Every downstream column computed from this one, transitively.

        Transitivity is the point. A column removed from a staging model reaches a
        mart four hops away under a different name, and the mart's own SQL never
        mentions either the staging model or the original column.
        """
        start = ColumnRef(model, column)
        seen: set[ColumnRef] = set()
        frontier = [start]
        for _ in range(depth):
            nxt: list[ColumnRef] = []
            for node in frontier:
                for child in self.feeds.get(node, frozenset()):
                    if child not in seen:
                        seen.add(child)
                        nxt.append(child)
            if not nxt:
                break
            frontier = nxt
        return tuple(sorted(seen))

    def consumer_models(self, model: str, column: str) -> tuple[str, ...]:
        """Every model that would break if this column disappeared.

        Two ways to depend on a column, and only one of them shows up as an edge in
        the value graph. A mart that sums it is a consumer; so is the model that joins
        on it and projects nothing. Reporting only the first would go quiet on a
        removed join key, which is the worst case in the family.
        """
        readers = {ref.model for ref in self.consumers_of(model, column)}
        readers |= set(self.used_by.get(ColumnRef(model, column), frozenset()))
        return tuple(sorted(readers))

    def referencing_models(self, model: str, column: str) -> tuple[str, ...]:
        """Models that name the column without carrying it into their own output."""
        named = set(self.used_by.get(ColumnRef(model, column), frozenset()))
        return tuple(sorted(named - {ref.model for ref in self.consumers_of(model, column)}))

    def sources_of(self, model: str, column: str, *, depth: int = 10) -> tuple[ColumnRef, ...]:
        """Every upstream column this one is computed from, transitively.

        The other direction, and the one a reviewer asks when a number looks wrong:
        where does this figure actually come from.
        """
        seen: set[ColumnRef] = set()
        frontier = [ColumnRef(model, column)]
        for _ in range(depth):
            nxt: list[ColumnRef] = []
            for node in frontier:
                for parent in self.reads.get(node, frozenset()):
                    if parent not in seen:
                        seen.add(parent)
                        nxt.append(parent)
            if not nxt:
                break
            frontier = nxt
        return tuple(sorted(seen))


def _relation_key(model: ModelNode) -> str | None:
    """The relation name a compiled reference to this model will carry."""
    return model.relation_name or None


def _topological(snapshot: ProjectSnapshot) -> list[str]:
    """Models in dependency order, tolerating cycles rather than failing on them."""
    order: list[str] = []
    done: set[str] = set()

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if name in done or name in stack:
            return
        model = snapshot.models.get(name)
        if model is None:
            return
        for dep in model.depends_on_models:
            visit(dep.split(".")[-1], (*stack, name))
        done.add(name)
        order.append(name)

    for name in sorted(snapshot.models):
        visit(name, ())
    return order


def _table_index(snapshot: ProjectSnapshot) -> dict[str, str]:
    """Map every spelling of a relation back to the model it is.

    Compiled SQL addresses models by their physical relation, which carries the
    catalog and schema and is quoted. Both the fully-qualified form and the bare
    identifier are indexed, because a project that compiles without a catalog still
    has to resolve.
    """
    index: dict[str, str] = {}
    for name, model in snapshot.models.items():
        index.setdefault(name.lower(), name)
        relation = _relation_key(model)
        if relation:
            index.setdefault(relation.replace('"', "").lower(), name)
    return index


def _model_for_table(table: exp.Table, index: dict[str, str]) -> str | None:
    """Resolve a compiled table reference to a model name."""
    parts = (table.args.get("catalog"), table.args.get("db"), table.this)
    qualified = ".".join(part.name for part in parts if isinstance(part, exp.Identifier))
    for candidate in (qualified.lower(), table.name.lower()):
        if candidate in index:
            return index[candidate]
    return None


def _output_columns(tree: exp.Expression) -> list[str]:
    """Projected names of the outermost SELECT, or an empty list if it is still a star."""
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not isinstance(select, exp.Select):
        return []
    names: list[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            return []
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return []
        name = projection.alias_or_name
        if not name:
            return []
        names.append(name)
    return names


def build_column_graph(
    snapshot: ProjectSnapshot,
    *,
    trace: set[str] | None = None,
    dialect: str = "trino",
) -> ColumnGraph:
    """Derive every model's columns, then trace edges for the models asked about.

    ``trace`` narrows the expensive half. Schema derivation always covers the whole
    project — a mart cannot be resolved without its ancestors — but per-column tracing
    runs only where a caller needs edges, which in a review is the changed models plus
    what is downstream of them. Passing None traces everything.
    """
    graph = ColumnGraph()
    schema = MappingSchema(dialect=dialect, normalize=False)
    parsed: dict[str, exp.Expression] = {}

    for name in _topological(snapshot):
        model = snapshot.models[name]
        sql = model.analysable_sql
        if sql is None:
            # Seeds are CSV, and a parse-only manifest has no compiled SQL. Both are
            # legitimate roots; neither is a failure worth reporting as one.
            if not model.is_seed:
                graph.unresolved[name] = "no compiled SQL"
            continue
        try:
            tree = parse_sql(sql, dialect=dialect)
        except ParseError as exc:
            graph.unresolved[name] = f"unparseable: {exc}"
            continue
        try:
            resolved = qualify(
                tree.copy(),
                schema=schema,
                dialect=dialect,
                validate_qualify_columns=False,
                infer_schema=True,
            )
        except (SqlglotError, KeyError, ValueError) as exc:
            graph.unresolved[name] = f"could not qualify: {type(exc).__name__}"
            continue

        columns = _output_columns(resolved)
        if not columns:
            graph.unresolved[name] = "projection is a star over an undeclared relation"
            continue

        parsed[name] = tree
        graph.outputs[name] = tuple(columns)
        relation = _relation_key(model) or name
        schema.add_table(
            exp.to_table(relation, dialect=dialect),
            {column: "UNKNOWN" for column in columns},
            dialect=dialect,
        )

    index = _table_index(snapshot)
    wanted = set(graph.outputs) if trace is None else (set(trace) & set(graph.outputs))
    graph.untraced = set(graph.outputs) - wanted

    reads: dict[ColumnRef, set[ColumnRef]] = {}
    feeds: dict[ColumnRef, set[ColumnRef]] = {}
    used_by: dict[ColumnRef, set[str]] = {}
    for name in sorted(wanted):
        sql = snapshot.models[name].analysable_sql
        if sql is None:
            continue
        traced = graph.outputs[name]
        if len(traced) > _MAX_COLUMNS_PER_MODEL:
            graph.unresolved[name] = f"too wide to trace ({len(traced)} columns)"
            graph.outputs.pop(name, None)
            continue
        for column in traced:
            for upstream in _trace_column(sql, column, schema=schema, index=index, dialect=dialect):
                node = ColumnRef(name, column)
                reads.setdefault(node, set()).add(upstream)
                feeds.setdefault(upstream, set()).add(node)

        named = _named_columns(parsed[name], schema=schema, index=index, dialect=dialect)
        if named:
            graph.uses[name] = frozenset(named)
            for upstream in named:
                used_by.setdefault(upstream, set()).add(name)

    graph.reads = {key: frozenset(value) for key, value in reads.items()}
    graph.feeds = {key: frozenset(value) for key, value in feeds.items()}
    graph.used_by = {key: frozenset(value) for key, value in used_by.items()}
    return graph


def _named_columns(
    tree: exp.Expression,
    *,
    schema: MappingSchema,
    index: dict[str, str],
    dialect: str,
) -> set[ColumnRef]:
    """Upstream columns this model writes out by name, wherever they appear.

    Qualifying resolves every column to the relation it comes from, which is what
    turns ``on entries.rate_period = rates.rate_date`` into two upstream column
    references rather than two opaque identifiers.

    Star expansion is the complication. Qualifying has to expand stars for scopes to
    resolve at all, but a column pulled in by ``select *`` and then never mentioned is
    not a dependency -- delete it upstream and this model happily produces one column
    fewer. So the expanded references are intersected with the names actually written
    in the SQL, which drops exactly the star-derived ones and keeps every deliberate
    reference.
    """
    written = {column.name for column in tree.find_all(exp.Column)}
    if not written:
        return set()
    try:
        resolved = qualify(
            tree.copy(),
            schema=schema,
            dialect=dialect,
            validate_qualify_columns=False,
            infer_schema=True,
        )
    except (SqlglotError, KeyError, ValueError, RecursionError):
        return set()

    found: set[ColumnRef] = set()
    for scope in traverse_scope(resolved):
        for column in scope.find_all(exp.Column):
            if column.name not in written:
                continue
            source = scope.sources.get(column.table)
            if isinstance(source, exp.Table):
                model = _model_for_table(source, index)
                if model is not None:
                    found.add(ColumnRef(model, column.name))
    return found


def _trace_column(
    sql: str,
    column: str,
    *,
    schema: MappingSchema,
    index: dict[str, str],
    dialect: str,
) -> set[ColumnRef]:
    """The upstream model columns one output column is computed from.

    Only the base-relation leaves matter here: intermediate CTE hops are internal to
    the model and say nothing a reviewer of *another* model needs.
    """
    try:
        root = sqlglot_lineage(column, sql, schema=schema, dialect=dialect)
    except (SqlglotError, KeyError, ValueError, RecursionError) as exc:
        log.debug("lineage_trace_failed", column=column, error=type(exc).__name__)
        return set()

    found: set[ColumnRef] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        source = node.source
        if isinstance(source, exp.Table):
            model = _model_for_table(source, index)
            if model is not None:
                # sqlglot names a leaf "alias.column"; the column is the last part.
                name = node.name.split(".")[-1]
                if name and name != "*":
                    found.add(ColumnRef(model, name))
        stack.extend(node.downstream)
    return found


@dataclass
class LineageIndex:
    """Both revisions' column graphs, built only if something asks.

    Which revision to ask matters more than it looks. *Who reads the column I just
    removed* can only be answered against the **before** graph — in the after graph the
    column no longer exists, nothing resolves to it, and the honest-looking answer
    "nothing reads it" is the exact false negative that lets the change through.
    Forward-looking questions — what does this column feed now, where does this PII
    end up — belong to the after graph.

    Building is deferred because a review where no rule asks for column lineage should
    not pay for it, and a review where several do should pay once.
    """

    before_snapshot: ProjectSnapshot
    after_snapshot: ProjectSnapshot
    trace: frozenset[str] | None = None
    dialect: str = "trino"
    _before: ColumnGraph | None = field(default=None, repr=False)
    _after: ColumnGraph | None = field(default=None, repr=False)

    @property
    def before(self) -> ColumnGraph:
        if self._before is None:
            self._before = build_column_graph(
                self.before_snapshot,
                trace=set(self.trace) if self.trace is not None else None,
                dialect=self.dialect,
            )
        return self._before

    @property
    def after(self) -> ColumnGraph:
        if self._after is None:
            self._after = build_column_graph(
                self.after_snapshot,
                trace=set(self.trace) if self.trace is not None else None,
                dialect=self.dialect,
            )
        return self._after
