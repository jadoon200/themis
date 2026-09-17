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


def _search_models(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).lower()
    matches = sorted(
        name
        for name, model in workspace.after.models.items()
        if query in name.lower() and not model.is_seed
    )
    if not matches:
        return ToolResult(text=f"No model name contains {query!r}.", data={"models": []})
    lines = []
    for name in matches:
        model = workspace.after.models[name]
        tags = f" tags={','.join(model.tags)}" if model.tags else ""
        lines.append(f"{name} ({model.materialization}){tags}")
    return ToolResult(
        text=f"{len(matches)} model(s) matching {query!r}:\n" + _limited(lines, "models"),
        data={"models": matches},
    )


def _model_details(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    if (missing := _unknown_model(workspace, name)) is not None:
        return missing
    model = workspace.after.models[name]
    upstream = sorted(dep.split(".")[-1] for dep in model.depends_on_models)
    downstream = workspace.after.downstream_of(name)
    lines = [
        f"model: {name}",
        f"file: {model.file_path}",
        f"materialization: {model.materialization}",
    ]
    if model.incremental_strategy:
        lines.append(f"incremental strategy: {model.incremental_strategy}")
    if model.unique_key:
        lines.append(f"unique key (config): {', '.join(model.unique_key)}")
    if model.tags:
        lines.append(f"tags: {', '.join(model.tags)}")
    lines.append(f"reads from: {', '.join(upstream) or 'nothing'}")
    lines.append(f"models downstream: {len(downstream)}")
    if model.columns:
        lines.append(f"declared columns: {', '.join(c.name for c in model.columns)}")
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
    downstream = workspace.after.downstream_of(name) if direction == "downstream" else ()
    graph = workspace.lineage(name, *downstream)
    if not graph.is_traced(name):
        reason = graph.unresolved.get(name, "it was not traced")
        return ToolResult(
            text=f"Lineage of {name} is unknown ({reason}); treat it as unknown, not empty.",
            ok=False,
        )
    if column not in graph.outputs.get(name, ()):
        known = ", ".join(graph.outputs.get(name, ())[:20])
        return ToolResult(
            text=f"{name} has no output column {column}. Its columns: {known}", ok=False
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
    untraced = sorted(model for model in downstream if not graph.is_traced(model))
    caveat = (
        f"\n{len(untraced)} downstream model(s) could not be traced, so this may be incomplete: "
        + ", ".join(untraced[:10])
        if untraced
        else ""
    )
    if not items:
        empty = "no upstream model column" if direction == "upstream" else "no downstream column"
        return ToolResult(
            text=f"{name}.{column} {label} {empty}.{caveat}",
            data={"columns": [], "untraced": untraced},
        )
    return ToolResult(
        text=f"{name}.{column} {label}:\n" + _limited(items, "columns") + caveat,
        data={"columns": items, "untraced": untraced},
    )


def _downstream(workspace: Workspace, args: dict[str, Any]) -> ToolResult:
    name = str(args["model"])
    if (missing := _unknown_model(workspace, name)) is not None:
        return missing
    names = list(workspace.after.downstream_of(name))
    if not names:
        return ToolResult(text=f"Nothing is built on {name}.", data={"models": []})
    lines = []
    for child in names:
        model = workspace.after.models.get(child)
        tags = f" tags={','.join(model.tags)}" if model and model.tags else ""
        lines.append(f"{child}{tags}")
    return ToolResult(
        text=f"{len(names)} model(s) downstream of {name}:\n" + _limited(lines, "models"),
        data={"models": names},
    )


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
            "Find models whose name contains some text. Use it when unsure of an exact name.",
            _object({"query": {"type": "string"}}, ("query",)),
            _search_models,
            {"query": "revenue"},
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
            "feeds. direction is 'upstream' (default) or 'downstream'.",
            _object(
                {
                    "model": _MODEL,
                    "column": {"type": "string"},
                    "direction": {"type": "string", "enum": ["upstream", "downstream"]},
                },
                ("model", "column"),
            ),
            _column_lineage,
            {"model": "stg_orders", "column": "amount", "direction": "upstream"},
        ),
        Tool(
            "downstream_models",
            "Every model built on a model, with their tags. Shows what a change can reach.",
            _object({"model": _MODEL}, ("model",)),
            _downstream,
            {"model": "stg_orders"},
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
