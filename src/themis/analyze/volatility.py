"""Values that differ between two builds of identical code.

THEMIS compares two revisions by compiling and building each separately. Anything that
depends on *when* or *which* invocation ran — rather than on the code and the data —
therefore differs between them even when nothing was changed, and reads as a change.
Found on a comment-only edit: an audit column stamped with `current_timestamp`, one with
`'{{ run_started_at }}'` and one with `'{{ invocation_id }}'` turned an untouched model into
a high "changed and no rule explains why", with every row "changed" in all three.

There are two kinds, and they need different handling:

- **Compile-time** — `run_started_at` and `invocation_id` render into the compiled SQL as
  literals, so the *code* looks different. Masked when the manifest is loaded, using the
  invocation the manifest itself records, so identical code compares identical.
- **Build-time** — `current_timestamp`, `now()`, `random()`, `uuid()` are evaluated while the
  table is built, so the *data* differs. Detected statically from the SQL and propagated to
  every downstream column computed from them, then left out of row-by-row comparison — and
  named in the report, because a column that was not compared must never look like one
  that did not change.

Neither is a heuristic about column names; a load timestamp called anything at all is
found. The name list in settings remains for values this cannot see, such as a column an
upstream system stamps before dbt ever reads it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlglot import exp

from themis.analyze.parse import ParseError, parse_sql
from themis.logging import get_logger
from themis.snapshot import ModelNode, ProjectSnapshot

log = get_logger(__name__)

INVOCATION_ID = "<dbt invocation_id>"
RUN_STARTED_AT = "<dbt run_started_at>"

_QUOTED = re.compile(r"'((?:[^']|'')*)'")

# How far either side of the recorded invocation a rendered timestamp may fall. dbt stamps
# `invocation_started_at` a few hundred microseconds before `run_started_at` is rendered.
_BEFORE_START = timedelta(seconds=5)
# `generated_at` is written when the manifest is, after compiling the whole project, so a
# window closed at it covers every model without a guess at how long compiling takes.
_AFTER_GENERATED = timedelta(seconds=5)
# With only `generated_at` to go on, how long a compile may plausibly have taken.
_COMPILE_WINDOW = timedelta(hours=2)


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_timestamp(text: str) -> datetime | None:
    """A literal that carries a time of day, not a date alone.

    A date alone is the same in two compiles minutes apart, so it never needs masking —
    and masking it would risk hiding a hard-coded business date.
    """
    if len(text) < 16 or text[4:5] != "-" or ":" not in text:
        return None
    return _instant(text.replace(" ", "T", 1))


def invocation_window(metadata: dict[str, object]) -> tuple[datetime, datetime] | None:
    """The span in which a compile's rendered run timestamps can fall."""
    started = _instant(metadata.get("invocation_started_at"))
    generated = _instant(metadata.get("generated_at"))
    if started is not None and generated is not None:
        return started - _BEFORE_START, generated + _AFTER_GENERATED
    if generated is not None:
        return generated - _COMPILE_WINDOW, generated + _AFTER_GENERATED
    if started is not None:
        return started - _BEFORE_START, started + _COMPILE_WINDOW
    return None


def mask_invocation_literals(
    sql: str, *, invocation_id: str | None, window: tuple[datetime, datetime] | None
) -> str:
    """Replace literals rendered from the invocation with fixed placeholders.

    Only exact matches are replaced: the manifest's own invocation id, and timestamps that
    fall inside the span the compile ran in. Anything else stays exactly as written.
    """
    if not invocation_id and window is None:
        return sql

    def replace(match: re.Match[str]) -> str:
        text = match.group(1)
        if invocation_id and text == invocation_id:
            return f"'{INVOCATION_ID}'"
        if window is not None:
            moment = _is_timestamp(text)
            if moment is not None and window[0] <= moment <= window[1]:
                return f"'{RUN_STARTED_AT}'"
        return match.group(0)

    return _QUOTED.sub(replace, sql)


# Evaluated when the table is built. current_date is deliberately absent: two builds
# minutes apart agree on the date, and excluding every column computed from it would
# stop measuring columns that compare perfectly well.
_VOLATILE_NODES: tuple[type[exp.Expression], ...] = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "CurrentTimestamp",
            "Localtimestamp",
            "CurrentTime",
            "Localtime",
            "Rand",
            "Uuid",
        )
    )
    if isinstance(node, type)
)
_VOLATILE_FUNCTIONS = frozenset(
    {"now", "uuid", "random", "rand", "gen_random_uuid", "random_uuid", "getdate", "sysdate"}
)


def _is_volatile_node(node: object) -> bool:
    if isinstance(node, _VOLATILE_NODES):
        return True
    if isinstance(node, exp.Anonymous) and str(node.name).lower() in _VOLATILE_FUNCTIONS:
        return True
    if isinstance(node, exp.Literal) and node.is_string:
        text = str(node.this)
        return INVOCATION_ID in text or RUN_STARTED_AT in text
    return False


def _output_names(tree: exp.Expression) -> set[str] | None:
    """The outermost statement's column names, or None when a star hides them."""
    outer = tree
    while isinstance(outer, exp.SetOperation):
        outer = outer.this
    if not isinstance(outer, exp.Select):
        return None
    names: set[str] = set()
    for projection in outer.selects:
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            return None
        if projection.alias_or_name:
            names.add(projection.alias_or_name)
    return names


def _volatile_in_model(tree: exp.Expression, inherited: set[str]) -> set[str]:
    """Every column name, anywhere in the statement, computed from a volatile value.

    A fixpoint over every projection, CTEs included, so a timestamp stamped in one CTE and
    renamed in the next is followed. A projection is volatile if it contains a volatile
    function or reads a column already known to be volatile — including one inherited
    from an upstream model by name.
    """
    names = set(inherited)
    projections = [
        (projection.alias_or_name, projection)
        for select in tree.find_all(exp.Select)
        for projection in select.selects
        if projection.alias_or_name
    ]
    changed = True
    while changed:
        changed = False
        for alias, projection in projections:
            if alias in names:
                continue
            if any(_is_volatile_node(node) for node in projection.walk()) or any(
                column.name in names for column in projection.find_all(exp.Column)
            ):
                names.add(alias)
                changed = True
    return names


def _snapshot_stamped(model: ModelNode, volatile: set[str]) -> set[str]:
    """A snapshot's bookkeeping columns, when they record when the build ran.

    Under `check` every version is stamped with the time of the run that wrote it, so
    two builds of identical data disagree on each of them. Under `timestamp` they are the
    source's own `updated_at` — unless that is itself stamped at build time, which is
    the defect F9004 exists for.
    """
    history = model.history
    if history is None:
        return set()
    if history.strategy == "check" or (history.updated_at or "") in volatile:
        return set(history.meta_columns)
    return set()


def volatile_columns(
    snapshot: ProjectSnapshot, *, dialect: str = "trino"
) -> dict[str, frozenset[str]]:
    """Each model's output columns whose values differ between any two builds.

    Walks the project in dependency order so a volatile column propagates into everything
    computed from it, and into every model that passes it through with `select *`.
    """
    found: dict[str, frozenset[str]] = {}
    for name in _dependency_order(snapshot):
        model = snapshot.models[name]
        upstream: set[str] = set()
        for dependency in model.depends_on_models:
            upstream |= found.get(dependency.split(".")[-1], frozenset())
        sql = model.analysable_sql
        if sql is None:
            continue
        try:
            tree = parse_sql(sql, dialect=dialect)
        except ParseError:
            # Unparseable: say nothing rather than guess. Pass-through names still flow,
            # because a model that cannot be read is still built from its upstreams.
            if upstream:
                found[name] = frozenset(upstream)
            continue
        names = _volatile_in_model(tree, upstream)
        outputs = _output_names(tree)
        volatile = names if outputs is None else names & outputs
        volatile |= _snapshot_stamped(model, volatile)
        if volatile:
            found[name] = frozenset(volatile)
    if found:
        log.debug("volatility.found", models=len(found))
    return found


def _dependency_order(snapshot: ProjectSnapshot) -> list[str]:
    order: list[str] = []
    state: dict[str, int] = {}
    for start in sorted(snapshot.models):
        if state.get(start):
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node, done = stack.pop()
            if done:
                state[node] = 2
                order.append(node)
                continue
            if state.get(node):
                continue
            state[node] = 1
            stack.append((node, True))
            model = snapshot.models.get(node)
            for dependency in model.depends_on_models if model else ():
                parent = dependency.split(".")[-1]
                if parent in snapshot.models and not state.get(parent):
                    stack.append((parent, False))
    return order
