"""Scale: the synthetic project, and the lineage speed-up that has to change nothing.

Timings belong in scripts/scale_check.py, where a person reads them; a timing assertion in
CI is a flaky test. What belongs here is what must hold at any size: the synthetic project
is valid SQL that every stage can analyse, and tracing a model's columns in one pass gives
exactly the graph tracing them one by one did.
"""

from __future__ import annotations

from unittest import mock

import themis.analyze.lineage as lineage
from themis.analyze.grain import infer_grains
from themis.analyze.parse import parse_sql
from themis.analyze.volatility import volatile_columns
from themis.eval import synthetic
from themis.pipeline import build_contexts
from themis.rules.registry import run_rules


def test_the_synthetic_project_is_valid_compiled_sql() -> None:
    project = synthetic.project(80)
    for model in project.models.values():
        if model.compiled_sql:
            parse_sql(model.compiled_sql)


def test_every_stage_analyses_the_synthetic_project() -> None:
    acquired = synthetic.changed(synthetic.project(80), count=2)
    grains = infer_grains(acquired.after)
    contexts = build_contexts(acquired, grains, dialect="trino")
    findings, _ = run_rules(contexts)
    assert len(contexts) == 2
    assert any(f.rule_id == "F2001" for f in findings)
    assert volatile_columns(acquired.after)


def test_single_pass_lineage_is_identical_to_tracing_column_by_column() -> None:
    """Asked for every column at once, sqlglot parses a model once instead of once per
    column — twice as fast, and it must not change a single edge. Checked here so a sqlglot
    upgrade that changes the all-columns mode fails a test rather than a review."""

    def per_column(sql, columns, *, schema, index, dialect):  # type: ignore[no-untyped-def]
        return {
            column: lineage._trace_column(sql, column, schema=schema, index=index, dialect=dialect)
            for column in columns
        }

    project = synthetic.project(60)
    single_pass = lineage.build_column_graph(project)
    with mock.patch.object(lineage, "_trace_model", per_column):
        column_by_column = lineage.build_column_graph(project)

    assert single_pass.reads == column_by_column.reads
    assert single_pass.feeds == column_by_column.feeds
    assert single_pass.uses == column_by_column.uses
    assert single_pass.unresolved == column_by_column.unresolved
    assert sum(len(edges) for edges in single_pass.reads.values()) > 100
