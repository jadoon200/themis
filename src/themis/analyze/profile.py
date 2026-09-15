"""The shape of a dbt project, with nothing in it that belongs to the project.

Whether THEMIS will work on a real project turns on questions about its shape, not its
content: how much of its SQL parses as Trino, how deep its CTEs go, how many models a
macro reaches, how much grain can be derived, how much lineage resolves, whether its
column names are ones the vocabulary recognises. Every answer here is a count. No model,
column, macro or tag name appears — only the names in THEMIS's own configuration, whose
hit counts are the point — so the output can be shared from a project whose code cannot.
"""

from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Any

from sqlglot import exp

from themis.analyze.lineage import ColumnGraph
from themis.analyze.parse import ParseError, parse_sql
from themis.models import Grain, GrainSource
from themis.snapshot import ProjectSnapshot
from themis.vocabulary import Vocabulary


def _depth(snapshot: ProjectSnapshot) -> int:
    """The longest chain of models in the DAG."""
    parents: dict[str, list[str]] = {name: [] for name in snapshot.models}
    for parent, children in snapshot.child_map.items():
        for child in children:
            parents.setdefault(child, []).append(parent)
    memo: dict[str, int] = {}

    def depth(name: str, seen: frozenset[str]) -> int:
        if name in memo:
            return memo[name]
        if name in seen:
            return 0  # a cycle: count no further
        value = 1 + max((depth(p, seen | {name}) for p in parents.get(name, [])), default=0)
        memo[name] = value
        return value

    return max((depth(name, frozenset()) for name in snapshot.models), default=0)


def _spread(values: list[int]) -> dict[str, float | int]:
    if not values:
        return {"max": 0, "median": 0}
    return {"max": max(values), "median": median(values)}


def profile(
    snapshot: ProjectSnapshot,
    grains: dict[str, Grain],
    graph: ColumnGraph,
    vocab: Vocabulary,
    *,
    dialect: str = "trino",
) -> dict[str, Any]:
    """Counts describing a project, and nothing that names anything in it."""
    sql_models = [m for m in snapshot.models.values() if not m.is_seed]
    seeds = [m for m in snapshot.models.values() if m.is_seed]

    parse_failures = 0
    shapes: Counter[str] = Counter()
    cte_counts: list[int] = []
    line_counts: list[int] = []
    for model in sql_models:
        sql = model.analysable_sql
        if sql is None:
            continue
        line_counts.append(sql.count("\n") + 1)
        try:
            tree = parse_sql(sql, dialect=dialect)
        except ParseError:
            parse_failures += 1
            continue
        cte_counts.append(len(list(tree.find_all(exp.CTE))))
        for label, node_type in (
            ("joins", exp.Join),
            ("set_operations", exp.SetOperation),
            ("window_functions", exp.Window),
            ("group_by", exp.Group),
            ("subqueries", exp.Subquery),
            ("case_expressions", exp.Case),
        ):
            if tree.find(node_type) is not None:
                shapes[label] += 1
        if any(isinstance(node, exp.Select) and node.args.get("distinct") for node in tree.walk()):
            shapes["distinct"] += 1

    generators = {m.name for m in snapshot.macros.values() if m.reads_data_at_compile_time}
    macro_reach = [len(snapshot.models_using_macro(name)) for name in snapshot.macros]

    grain_sources = Counter(grains[m.name].source.value for m in sql_models if m.name in grains)
    proven = sum(1 for m in sql_models if m.name in grains and grains[m.name].is_proven)

    columns: set[tuple[str, str]] = set()
    for model in sql_models:
        for column in graph.outputs.get(model.name, ()):
            columns.add((model.name, column))
        for declared in model.columns:
            columns.add((model.name, declared.name))
    money_hits = {
        hint: sum(1 for _, c in columns if hint in c.lower()) for hint in vocab.money_hints
    }
    sensitive_hits = {
        hint: sum(1 for _, c in columns if hint in c.lower()) for hint in vocab.sensitive_hints
    }
    tag_hits = {
        tag: sum(1 for m in sql_models if tag.lower() in {t.lower() for t in m.tags})
        for tag in vocab.governed_tags
    }
    folder_hits = {
        folder: sum(1 for m in sql_models if folder in m.file_path.replace("\\", "/"))
        for folder in vocab.published_folders
    }

    return {
        "nodes": {
            "sql_models": len(sql_models),
            "seeds": len(seeds),
            "project_macros": len(snapshot.macros),
            "declared_tests": len(snapshot.tests),
            "exposures": len(snapshot.exposures),
        },
        "compiled_sql": {
            "models_without_it": len(snapshot.models_without_compiled_sql),
            "parse_failures_as_" + dialect: parse_failures,
            "lines": _spread(line_counts),
            "ctes": _spread(cte_counts),
            "models_with": dict(sorted(shapes.items())),
        },
        "materializations": dict(Counter(m.materialization for m in sql_models).most_common()),
        "incremental_strategies": dict(
            Counter(
                m.incremental_strategy or "default"
                for m in sql_models
                if m.materialization == "incremental"
            ).most_common()
        ),
        "config": {
            "with_unique_key": sum(1 for m in sql_models if m.unique_key),
            "with_enforced_contract": sum(1 for m in sql_models if m.contract_enforced),
            "with_hooks": sum(1 for m in sql_models if m.pre_hooks or m.post_hooks),
            "with_properties": sum(1 for m in sql_models if m.properties),
            "partitioned": sum(1 for m in sql_models if m.partitioned_by),
            "reading_sources": sum(1 for m in sql_models if m.depends_on_sources),
        },
        "dag": {
            "depth": _depth(snapshot),
            "max_direct_children": max((len(c) for c in snapshot.child_map.values()), default=0),
        },
        "macros": {
            "models_reached_per_macro": _spread(macro_reach),
            "macros_reaching_10_or_more_models": sum(1 for n in macro_reach if n >= 10),
            # A single one makes the manifest cache refuse the whole project.
            "building_sql_from_query_results": len(generators),
            "models_depending_on_those": len(snapshot.data_dependent_models()),
        },
        "grain": {
            "proven": proven,
            "by_source": dict(sorted(grain_sources.items())),
            "unknown": grain_sources.get(GrainSource.UNKNOWN.value, 0),
        },
        "lineage": {
            "resolved": sum(1 for m in sql_models if m.name in graph.outputs),
            "unresolved": len(graph.unresolved),
            "output_columns": len(columns),
        },
        # Hit counts per configured name. A hint that matches nothing on a project whose
        # money columns are called something else is a rule that will never fire there.
        "vocabulary": {
            "money_columns": sum(1 for _, c in columns if vocab.is_monetary(c)),
            "money_hint_hits": money_hits,
            "sensitive_columns": sum(1 for _, c in columns if vocab.is_sensitive(c)),
            "sensitive_hint_hits": sensitive_hits,
            "governed_models": sum(1 for m in sql_models if vocab.is_governed(m.tags)),
            "governed_tag_hits": tag_hits,
            "distinct_tags_in_project": len({t for m in sql_models for t in m.tags}),
            "published_models": sum(1 for m in sql_models if vocab.is_published(m.file_path)),
            "published_folder_hits": folder_hits,
        },
    }
