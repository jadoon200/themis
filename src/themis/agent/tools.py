"""The tools: every fact THEMIS establishes, fetchable by name, and nothing else.

Designed for a small local model reading the results, and for a reviewer checking what it
quoted. So results are short, line-oriented and stable; a list says how much it left out;
a model name that does not exist gets the closest real names back rather than an empty
answer; and a question the workspace cannot answer says *why* — "no base revision", not
silence, because an agent that reads silence as "nothing there" reports a clean result.

No tool writes, builds or runs SQL the model wrote. The model chooses a fact; THEMIS
computes it. That is the line between an agent that investigates and one that invents.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any

from themis.agent.workspace import Workspace
from themis.analyze.lineage import ColumnRef

_LIST_LIMIT = 25
_SQL_LINES = 60


@dataclass(frozen=True)
class ToolResult:
    """What a tool returns: text for the model to read and quote, data for a program."""

    text: str
    data: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    # JSON schema for the arguments. Used verbatim to constrain the model's output, so a
    # call with a missing or misspelt argument cannot be produced in the first place.
    parameters: dict[str, Any]
    handler: Callable[[Workspace, dict[str, Any]], ToolResult]
    # The shape of a correct call. Shown to the model as a format, not a value to copy: an
    # 8B model given only a schema once passed `search_models('fct_revenue')` as the query.
    example: dict[str, Any] = field(default_factory=dict)

    def run(self, workspace: Workspace, arguments: dict[str, Any]) -> ToolResult:
        try:
            return self.handler(workspace, arguments)
        except Exception as exc:  # a tool failure is a result the agent can act on
            return ToolResult(
                text=f"{self.name} failed: {type(exc).__name__}: {str(exc)[:200]}", ok=False
            )


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_MODEL = {"type": "string", "description": "A model name, e.g. fct_revenue."}


def _unknown_model(workspace: Workspace, name: str) -> ToolResult | None:
    if name in workspace.after.models:
        return None
    close = difflib.get_close_matches(name, list(workspace.after.models), n=5, cutoff=0.5)
    hint = f" Closest names: {', '.join(close)}." if close else " Use search_models to find it."
    return ToolResult(text=f"There is no model named {name}.{hint}", ok=False)


def _limited(items: list[str], noun: str) -> str:
    shown = items[:_LIST_LIMIT]
    more = len(items) - len(shown)
    tail = f"\n... and {more} more {noun}" if more > 0 else ""
    return "\n".join(shown) + tail


def _not_a_review(tool: str) -> ToolResult:
    return ToolResult(
        text=(
            f"{tool} needs a review of a change, and this workspace is a single project "
            "snapshot with no base revision to compare against."
        ),
        ok=False,
    )


# --- project tools -----------------------------------------------------------------------


_TAGGED = {
    "type": "string",
    "description": "Keep only models carrying this tag, e.g. regulatory.",
}
_MATERIALIZED = {
    "type": "string",
    "description": "Keep only models with this materialization, e.g. incremental.",
}


def _describe(workspace: Workspace, name: str) -> str:
    model = workspace.after.models.get(name)
    if model is None:
        return name
    # Materialization and tags on every line: "which of these are incremental" otherwise
    # costs one more tool call per model, and a small step budget runs out.
    return f"{name} ({model.materialization}) — tags: {', '.join(model.tags) or 'none'}"


def _any_tag(workspace: Workspace, names: list[str], value: str) -> bool:
    lowered = value.lower()
    return any(
        lowered == tag.lower() for name in names for tag in workspace.after.models[name].tags
    )


def _any_materialization(workspace: Workspace, names: list[str], value: str) -> bool:
    lowered = value.lower()
    return any(workspace.after.models[name].materialization.lower() == lowered for name in names)


def _filtered(
    workspace: Workspace, names: list[str], args: dict[str, Any]
) -> tuple[list[str], str]:
    """Apply the tag/materialization filters, and say in words what was applied.

    The filter exists because asking an 8B model to read a list and keep the rows with a
    tag is where it drops one. Answering "which of these are regulatory" is then a lookup,
    not a list comprehension it has to perform in prose — and the count in the reply is
    THEMIS's, so a partial answer stops being quotable as a complete one.
    """
    tagged = str(args["tagged"]).strip() if args.get("tagged") else None
    materialized = str(args["materialized"]).strip() if args.get("materialized") else None

    # A value put in the wrong argument is answered, not refused. Asked which downstream
    # models were incremental, the model filled `tagged="incremental"`; the tool truthfully
    # said none was *tagged* incremental, and the answer became "there are none" — a filter
    # that had just fixed one question breaking another. Nothing here is ambiguous: no tag
    # is named `incremental` and no materialization is named `regulatory`, so the answer is
    # the one the caller meant, with the swap stated in the reply so nobody is misled.
    if (
        tagged is not None
        and materialized is None
        and not _any_tag(workspace, names, tagged)
        and _any_materialization(workspace, names, tagged)
    ):
        tagged, materialized = None, tagged
    elif (
        materialized is not None
        and tagged is None
        and not _any_materialization(workspace, names, materialized)
        and _any_tag(workspace, names, materialized)
    ):
        tagged, materialized = materialized, None

    kept = names
    if tagged is not None:
        lowered = tagged.lower()
        kept = [
            name
            for name in kept
            if any(lowered == tag.lower() for tag in workspace.after.models[name].tags)
        ]
    if materialized is not None:
        lowered = materialized.lower()
        kept = [
            name for name in kept if workspace.after.models[name].materialization.lower() == lowered
        ]
    described = ""
    if tagged is not None and materialized is not None:
        described = f" tagged {tagged} and materialized as {materialized}"
    elif tagged is not None:
        described = f" tagged {tagged}"
    elif materialized is not None:
        described = f" materialized as {materialized}"
    return kept, described


def _search_models(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).lower()
    matches = sorted(
        name
        for name, model in workspace.after.models.items()
        if query in name.lower() and not model.is_seed
    )
    kept, described = _filtered(workspace, matches, args)
    if not matches:
        return ToolResult(text=f"No model name contains {query!r}.", data={"models": []})
    if not kept:
        return ToolResult(
            text=(
                f"None of the {len(matches)} model(s) matching {query!r} is{described}."
                if described
                else f"No model name contains {query!r}."
            ),
            data={"models": []},
        )
    lines = [_describe(workspace, name) for name in kept]
    if described:
        headline = f"{len(kept)} of the {len(matches)} model(s) matching {query!r} are{described}:"
    else:
        headline = f"{len(matches)} model(s) matching {query!r}:"
    return ToolResult(text=headline + "\n" + _limited(lines, "models"), data={"models": kept})


def _model_details(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    if (missing := _unknown_model(workspace, name)) is not None:
        return missing
    model = workspace.after.models[name]
    upstream = sorted(dep.split(".")[-1] for dep in model.depends_on_models)
    downstream = workspace.after.downstream_of(name)
    facts = [
        ("file", model.file_path),
        ("materialization", model.materialization),
    ]
    if model.incremental_strategy:
        facts.append(("incremental strategy", model.incremental_strategy))
    if model.unique_key:
        facts.append(("unique key (config)", ", ".join(model.unique_key)))
    facts.append(("tags", ", ".join(model.tags) or "none"))
    facts.append(("reads from", ", ".join(upstream) or "nothing"))
    facts.append(("models downstream", str(len(downstream))))
    if model.columns:
        facts.append(("declared columns", ", ".join(c.name for c in model.columns)))
    # Every line names its model, so a quoted line cannot be read as being about another.
    lines = [f"{name} — {label}: {value}" for label, value in facts]
    return ToolResult(
        text="\n".join(lines),
        data={
            "model": name,
            "materialization": model.materialization,
            "tags": list(model.tags),
            "upstream": upstream,
            "downstream_count": len(downstream),
        },
    )


def _model_sql(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    revision = str(args.get("revision", "after"))
    if revision == "before" and workspace.before is None:
        return _not_a_review("model_sql with revision=before")
    snapshot = workspace.before if revision == "before" else workspace.after
    assert snapshot is not None
    if name not in snapshot.models:
        return _unknown_model(workspace, name) or ToolResult(
            text=f"{name} does not exist in the {revision} revision.", ok=False
        )
    sql = snapshot.models[name].analysable_sql
    if not sql:
        return ToolResult(text=f"{name} has no compiled SQL in this manifest.", ok=False)
    lines = sql.splitlines()
    shown = lines[:_SQL_LINES]
    tail = (
        f"\n-- ... {len(lines) - len(shown)} more lines not shown"
        if len(lines) > len(shown)
        else ""
    )
    return ToolResult(
        text=f"compiled SQL of {name} ({revision}):\n" + "\n".join(shown) + tail,
        data={"model": name, "revision": revision, "sql": sql},
    )


def _grain(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    if (missing := _unknown_model(workspace, name)) is not None:
        return missing
    grain = workspace.grains.get(name)
    if grain is None or not grain.columns:
        return ToolResult(
            text=f"The grain of {name} could not be established from its SQL.",
            data={"model": name, "columns": [], "source": "unknown"},
        )
    line = f"grain of {name}: ({', '.join(grain.columns)}), established by {grain.source.value}"
    if grain.rows_per_key is not None:
        line += f", measured at {grain.rows_per_key:.2f} rows per key"
    return ToolResult(
        text=line,
        data={
            "model": name,
            "columns": list(grain.columns),
            "source": grain.source.value,
            "rows_per_key": grain.rows_per_key,
        },
    )


def _column_lineage(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    column = str(args["column"])
    direction = str(args.get("direction", "upstream"))
    if (missing := _unknown_model(workspace, name)) is not None:
        return missing
    # Downstream edges are recorded on the models that consume a column, so answering "what
    # does this feed" means tracing everything built on it. Tracing only the model asked
    # about answered "feeds no downstream column" for an FX rate that two columns are
    # computed from — an absence that was really "never looked".
    # The same holds upstream: an ancestor's edges exist only once it is traced, and tracing
    # only the model asked about answered with one hop as though it were the whole answer.
    related = (
        workspace.after.downstream_of(name)
        if direction == "downstream"
        else _ancestors(workspace, name)
    )
    graph = workspace.lineage(name, *related)
    if not graph.is_traced(name):
        reason = graph.unresolved.get(name, "it was not traced")
        return ToolResult(
            text=f"Lineage of {name} is unknown ({reason}); treat it as unknown, not empty.",
            ok=False,
        )
    if column not in graph.outputs.get(name, ()):
        known = ", ".join(graph.outputs.get(name, ())[:20])
        # Where that column *does* exist. The agent's last wrong answer started here: asked
        # which columns of a mart come from an FX rate, it looked for `rate` on the mart,
        # found nothing, and reported the absence — an absence it had checked, which is
        # exactly what a citation check cannot catch. A dead end has to offer a next step.
        elsewhere = sorted(
            other for other, columns in graph.outputs.items() if other != name and column in columns
        )
        hint = ""
        if elsewhere:
            hint = (
                f" A column named {column} exists on: {', '.join(elsewhere[:5])}. To find what "
                f"it feeds in {name}, ask about it there with direction=downstream and "
                f"in_model={name}."
            )
        return ToolResult(
            text=f"{name} has no output column {column}. Its columns: {known}{hint}", ok=False
        )
    refs = (
        graph.sources_of(name, column)
        if direction == "upstream"
        else graph.consumers_of(name, column)
    )
    label = "is computed from" if direction == "upstream" else "feeds"
    items = sorted(str(ref) for ref in refs)
    if not items and direction == "upstream":
        # A staging model reads seeds and sources, whose columns are not traced. Saying
        # "no upstream column" there would read as "a constant", which is a different fact.
        model = workspace.after.models[name]
        roots = sorted(
            {dep.split(".")[-1] for dep in model.depends_on_models if dep.startswith("seed.")}
            | {dep.split(".")[-1] for dep in model.depends_on_sources}
        )
        if roots:
            return ToolResult(
                text=(
                    f"{name}.{column} reads from {', '.join(roots)} — a seed or source whose "
                    "columns THEMIS does not trace, so its exact source column is unknown."
                ),
                data={"columns": [], "untraced_roots": roots},
            )
    # "Which columns of the mart come from this rate" names two ends, and answering it used
    # to mean reading a list of every column the rate feeds and keeping the ones on that
    # model. That is the enumeration an 8B model drops, so the second end is an argument:
    # the count in the reply is THEMIS's, and a partial answer is not quotable as a whole one.
    in_model = str(args["in_model"]).strip() if args.get("in_model") else None
    total = len(items)
    if in_model is not None:
        if (missing := _unknown_model(workspace, in_model)) is not None:
            return missing
        items = [item for item in items if item.split(".")[0] == in_model]

    untraced = sorted(
        model
        for model in related
        if not graph.is_traced(model) and not workspace.after.models[model].is_seed
    )
    caveat = (
        f"\n{len(untraced)} related model(s) could not be traced, so this may be incomplete: "
        + ", ".join(untraced[:10])
        if untraced
        else ""
    )
    if not items:
        if in_model is not None:
            return ToolResult(
                text=(
                    f"None of the {total} column(s) {name}.{column} {label} is in "
                    f"{in_model}.{caveat}"
                ),
                data={"columns": [], "untraced": untraced},
            )
        empty = "no upstream model column" if direction == "upstream" else "no downstream column"
        return ToolResult(
            text=f"{name}.{column} {label} {empty}.{caveat}",
            data={"columns": [], "untraced": untraced},
        )
    # One relation per line, subject and object both named. A list under a header let a
    # small model attach a column to the wrong relation; a line that says it cannot.
    # Direct and indirect kept apart. Once the answer followed every hop, "which column is
    # this computed from" got the whole chain back and a model named a grandparent.
    start = ColumnRef(name, column)
    direct = {
        str(ref)
        for ref in (
            graph.reads.get(start, frozenset())
            if direction == "upstream"
            else graph.feeds.get(start, frozenset())
        )
    }
    if direction == "upstream":
        lines = [
            f"{name}.{column} is computed {'directly' if item in direct else 'indirectly'} "
            f"from {item}"
            for item in items
        ]
    else:
        lines = [
            f"{name}.{column} {'directly' if item in direct else 'indirectly'} feeds {item}"
            for item in items
        ]
    lines.sort(key=lambda line: (" indirectly " in line, line))
    if in_model is not None:
        headline = (
            f"{len(items)} of the {total} column(s) {name}.{column} {label} are in {in_model}:\n"
        )
        return ToolResult(
            text=headline + _limited(lines, "columns") + caveat,
            data={"columns": items, "untraced": untraced},
        )
    return ToolResult(
        text=_limited(lines, "columns") + caveat,
        data={"columns": items, "untraced": untraced},
    )


def _ancestors(workspace: Workspace, name: str) -> tuple[str, ...]:
    """Every model a model is built from, transitively."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        model = workspace.after.models.get(stack.pop())
        for dependency in model.depends_on_models if model else ():
            parent = dependency.split(".")[-1]
            if parent in workspace.after.models and parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return tuple(sorted(seen))


def _downstream(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    if (missing := _unknown_model(workspace, name)) is not None:
        return missing
    names = list(workspace.after.downstream_of(name))
    if not names:
        return ToolResult(text=f"Nothing is built on {name}.", data={"models": []})
    kept, described = _filtered(workspace, names, args)
    if not kept:
        return ToolResult(
            text=f"None of the {len(names)} model(s) downstream of {name} is{described}.",
            data={"models": []},
        )
    lines = [_describe(workspace, child) for child in kept]
    if described:
        headline = f"{len(kept)} of the {len(names)} model(s) downstream of {name} are{described}:"
    else:
        headline = f"{len(names)} model(s) downstream of {name}:"
    return ToolResult(text=headline + "\n" + _limited(lines, "models"), data={"models": kept})


def _conventions(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    if not workspace.conventions:
        return ToolResult(text="This project has written down no conventions.", data={})
    model = str(args["model"]) if args.get("model") else None
    rule = str(args["rule"]).upper() if args.get("rule") else None
    matching = [
        convention
        for convention in workspace.conventions
        if (rule is None or not convention.rules or rule in convention.rules)
        and (
            model is None
            or not convention.models
            or any(fnmatch(model, pattern) for pattern in convention.models)
        )
    ]
    if not matching:
        return ToolResult(text="No written convention applies there.", data={"conventions": []})
    lines = [
        f"{c.id} — when: {c.condition} known: {c.guidance} so: {c.implication}" for c in matching
    ]
    return ToolResult(
        text="Conventions (context from the team, not evidence about a change):\n"
        + "\n".join(lines),
        data={"conventions": [c.id for c in matching]},
    )


def _rule(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    from themis.rules.registry import ALL_RULES

    wanted = str(args["rule_id"]).upper()
    for rule in ALL_RULES:
        if rule.rule_id == wanted:
            doc = " ".join((type(rule).__doc__ or "").split())
            return ToolResult(
                text=f"{rule.rule_id} ({rule.family}): {doc}",
                data={"rule_id": rule.rule_id, "family": rule.family},
            )
    safety = {
        "X0001": "a measured change no rule accounts for — the safety net under every rule",
        "X0002": "the head revision no longer builds",
    }
    if wanted in safety:
        return ToolResult(text=f"{wanted}: {safety[wanted]}", data={"rule_id": wanted})
    return ToolResult(text=f"There is no rule {wanted}.", ok=False)


# --- review tools ------------------------------------------------------------------------


def _changed_models(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    if not workspace.is_review:
        return _not_a_review("changed_models")
    if not workspace.changed_models:
        return ToolResult(text="The change reaches no model.", data={"models": []})
    return ToolResult(
        text=f"{len(workspace.changed_models)} model(s) reviewed:\n"
        + _limited(list(workspace.changed_models), "models"),
        data={"models": list(workspace.changed_models)},
    )


def _findings(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    if not workspace.is_review:
        return _not_a_review("findings")
    model = args.get("model")
    rule = str(args["rule"]).upper() if args.get("rule") else None
    selected = [
        (index, f)
        for index, f in enumerate(workspace.findings, start=1)
        if (not model or f.evidence.model_name == model) and (not rule or f.rule_id == rule)
    ]
    if not selected:
        scope = " matching that" if model or rule else ""
        return ToolResult(text=f"The review recorded no findings{scope}.", data={"findings": []})
    lines = []
    for index, f in selected:
        lines.append(
            f"#{index} {f.rule_id} on {f.evidence.model_name} "
            f"[{f.severity.value}, {f.confidence.value}]: {f.title}"
        )
        if f.evidence.note:
            lines.append(f"   evidence: {f.evidence.note[:300]}")
        if f.suppressed_reason:
            lines.append(f"   set aside: {f.suppressed_reason[:200]}")
    return ToolResult(
        text=f"{len(selected)} finding(s):\n" + _limited(lines, "lines"),
        data={"findings": [index for index, _ in selected]},
    )


def _sql_diff(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    import difflib as _difflib

    if workspace.before is None:
        return _not_a_review("sql_diff")
    name = str(args["model"])
    before = workspace.before.models.get(name)
    after = workspace.after.models.get(name)
    if before is None and after is None:
        return _unknown_model(workspace, name) or ToolResult(text=f"No model {name}.", ok=False)
    old = (before.analysable_sql or "").splitlines() if before else []
    new = (after.analysable_sql or "").splitlines() if after else []
    diff = list(
        _difflib.unified_diff(old, new, fromfile="before", tofile="after", lineterm="", n=2)
    )
    if not diff:
        return ToolResult(text=f"The compiled SQL of {name} is identical in both revisions.")
    return ToolResult(
        text=f"compiled SQL diff of {name}:\n" + _limited(diff, "diff lines"),
        data={"model": name, "diff": diff},
    )


def _measured_change(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    if workspace.execution is None or not workspace.execution.ran:
        return ToolResult(
            text="Nothing was measured: this review did not build both revisions.", ok=False
        )
    name = str(args["model"])
    delta = workspace.execution.deltas.get(name)
    if delta is None:
        return ToolResult(text=f"{name} was not among the models built and measured.", ok=False)
    lines = [f"measured for {name}:"]
    if delta.build_error:
        lines.append(f"build failed ({delta.failed_revision}): {delta.build_error[:200]}")
    if delta.rows_before is not None and delta.rows_after is not None:
        lines.append(f"rows {delta.rows_before} -> {delta.rows_after}")
    for column, (was, now) in sorted(delta.sum_deltas.items()):
        lines.append(f"sum({column}) {was:.2f} -> {now:.2f}")
    keyed = delta.keyed
    if keyed is not None:
        changed = ", ".join(f"{c} ({n})" for c, n in sorted(keyed.columns_changed.items()))
        lines.append(
            f"paired on ({', '.join(keyed.key)}): {keyed.rows_changed} row(s) changed"
            + (f" in {changed}" if changed else "")
            + f", {keyed.rows_added} added, {keyed.rows_removed} removed"
        )
        if keyed.volatile_columns:
            lines.append(
                f"not compared (differ in any two builds): {', '.join(keyed.volatile_columns)}"
            )
    elif delta.keyed_skipped_reason:
        lines.append(f"rows not paired: {delta.keyed_skipped_reason}")
    lines.append(f"material: {'yes' if delta.is_material else 'no'}")
    return ToolResult(text="\n".join(lines), data={"model": name, "material": delta.is_material})


def registry() -> dict[str, Tool]:
    """Every tool, by name. The one definition the agent loop and the MCP server share."""
    tools = (
        Tool(
            "search_models",
            "Find models whose name contains some text, optionally only those with a given "
            "tag or materialization. Use it when unsure of an exact name.",
            _object(
                {"query": {"type": "string"}, "tagged": _TAGGED, "materialized": _MATERIALIZED},
                ("query",),
            ),
            _search_models,
            {"query": "revenue", "tagged": "regulatory"},
        ),
        Tool(
            "model_details",
            "A model's materialization, tags, file, what it reads and how much is built on it.",
            _object({"model": _MODEL}, ("model",)),
            _model_details,
            {"model": "stg_orders"},
        ),
        Tool(
            "model_sql",
            "A model's compiled SQL. revision is 'after' (default) or 'before' in a review.",
            _object(
                {"model": _MODEL, "revision": {"type": "string", "enum": ["after", "before"]}},
                ("model",),
            ),
            _model_sql,
            {"model": "stg_orders", "revision": "after"},
        ),
        Tool(
            "grain",
            "The key that identifies one row of a model, and how THEMIS established it.",
            _object({"model": _MODEL}, ("model",)),
            _grain,
            {"model": "stg_orders"},
        ),
        Tool(
            "column_lineage",
            "Which upstream columns a column is computed from, or which downstream columns it "
            "feeds. direction is 'upstream' (default) or 'downstream'. Give in_model to ask "
            "only about one model's columns — 'which columns of M come from X.c' is "
            "column_lineage(model=X, column=c, direction=downstream, in_model=M).",
            _object(
                {
                    "model": _MODEL,
                    "column": {"type": "string"},
                    "direction": {"type": "string", "enum": ["upstream", "downstream"]},
                    "in_model": {
                        "type": "string",
                        "description": "Keep only the columns belonging to this model.",
                    },
                },
                ("model", "column"),
            ),
            _column_lineage,
            {
                "model": "stg_orders",
                "column": "amount",
                "direction": "downstream",
                "in_model": "fct_revenue",
            },
        ),
        Tool(
            "downstream_models",
            "Every model built on a model, with their tags — or only those carrying a given "
            "tag or materialization. Shows what a change can reach.",
            _object(
                {"model": _MODEL, "tagged": _TAGGED, "materialized": _MATERIALIZED}, ("model",)
            ),
            _downstream,
            {"model": "stg_orders", "tagged": "regulatory"},
        ),
        Tool(
            "conventions",
            "What the project's reviewers have written down, optionally for a model or rule.",
            _object({"model": {"type": "string"}, "rule": {"type": "string"}}),
            _conventions,
            {"model": "stg_orders", "rule": "F1001"},
        ),
        Tool(
            "explain_rule",
            "What a rule id (e.g. F1001, X0001) checks for.",
            _object({"rule_id": {"type": "string"}}, ("rule_id",)),
            _rule,
            {"rule_id": "F1001"},
        ),
        Tool(
            "changed_models",
            "The models the change under review reaches. Review only.",
            _object({}),
            _changed_models,
        ),
        Tool(
            "findings",
            "The review's findings, optionally for one model or rule. Review only.",
            _object({"model": {"type": "string"}, "rule": {"type": "string"}}),
            _findings,
            {"model": "stg_orders"},
        ),
        Tool(
            "sql_diff",
            "How a model's compiled SQL changed between the two revisions. Review only.",
            _object({"model": _MODEL}, ("model",)),
            _sql_diff,
            {"model": "stg_orders"},
        ),
        Tool(
            "measured_change",
            "What building both revisions measured for a model: rows, totals, and values "
            "paired on its key. Only when the review ran with execution.",
            _object({"model": _MODEL}, ("model",)),
            _measured_change,
            {"model": "stg_orders"},
        ),
    )
    return {tool.name: tool for tool in tools}
