"""F5 — incremental models, as they are written on Hive.

On Hive an incremental model works by overwriting partitions: row-level deletes are
refused, so `delete+insert` and `merge` fail on the second run. That makes the filter in
the `is_incremental()` block load-bearing in a way it is not elsewhere — whatever it
selects *replaces* the partitions it touches.

These tests run against the demo project's own model text and the corpus's own edit to
it, not a hand-written stand-in: a rule that passes on SQL nobody writes can still never
fire on the SQL people do.
"""

from __future__ import annotations

from pathlib import Path

from themis.eval.mutations import select
from themis.models import Backend
from themis.rules.base import RuleContext
from themis.rules.families.f5_incremental import FilterNarrowerThanPartitionRule
from themis.snapshot import ModelNode, ProjectSnapshot

MODEL = (
    Path(__file__).resolve().parents[1] / "demo_project/models/marts/fct_revenue_incremental.sql"
)
OVERWRITE = "set session hive.insert_existing_partitions_behavior = 'OVERWRITE'"


def _node(sql: str, *, hook: bool = True, partitions: str = "ARRAY['period_month']") -> ModelNode:
    return ModelNode(
        name="fct_revenue_incremental",
        unique_id="model.themis_demo.fct_revenue_incremental",
        file_path="models/marts/fct_revenue_incremental.sql",
        raw_sql=sql,
        materialization="incremental",
        incremental_strategy="append",
        properties={"partitioned_by": partitions},
        pre_hooks=(OVERWRITE,) if hook else (),
    )


def _ctx(before: ModelNode | None, after: ModelNode) -> RuleContext:
    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)
    return RuleContext(
        model_name=after.name,
        before=before,
        after=after,
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains={},
    )


def _mutated() -> str:
    """The model as the corpus's partition-misaligning edit leaves it."""
    (case,) = select("incremental_filter_narrower_than_partition")
    source = MODEL.read_text()
    assert case.find in source, "the corpus edit no longer applies to the model"
    return source.replace(case.find, case.replace, 1)


def test_a_filter_narrower_than_the_partitions_it_overwrites_is_flagged() -> None:
    """Measured on Hive: 142 rows and 334.6M became 122 and 291.3M on the next run."""
    original = MODEL.read_text()
    (finding,) = FilterNarrowerThanPartitionRule().check(_ctx(_node(original), _node(_mutated())))
    assert finding.rule_id == "F5008"
    assert "period_month" in (finding.evidence.note or "")
    assert "posting_date" in (finding.evidence.note or "")
    assert "deleted" in finding.consequence


def test_the_model_as_written_is_silent() -> None:
    """Whole periods reprocessed and whole periods overwritten: the correct shape."""
    original = MODEL.read_text()
    assert FilterNarrowerThanPartitionRule().check(_ctx(_node(original), _node(original))) == []
    assert FilterNarrowerThanPartitionRule().check(_ctx(None, _node(original))) == []


def test_a_model_that_was_already_misaligned_is_not_blamed_on_the_change() -> None:
    mutated = _mutated()
    assert FilterNarrowerThanPartitionRule().check(_ctx(_node(mutated), _node(mutated))) == []


def test_without_partition_overwrite_the_filter_replaces_nothing() -> None:
    """Appending writes delete nothing — that is F5007's hazard, not this one."""
    original = MODEL.read_text()
    assert (
        FilterNarrowerThanPartitionRule().check(
            _ctx(_node(original, hook=False), _node(_mutated(), hook=False))
        )
        == []
    )


def test_changing_the_partition_column_away_from_the_filter_is_flagged_too() -> None:
    """The other way into the same hazard: the filter stays, the partitions move."""
    original = MODEL.read_text()
    (finding,) = FilterNarrowerThanPartitionRule().check(
        _ctx(_node(original), _node(original, partitions="ARRAY['posting_date']"))
    )
    assert "partitioned by posting_date" in (finding.evidence.note or "")


def test_iceberg_spells_the_partition_spec_differently_and_it_is_still_read() -> None:
    node = ModelNode(
        name="m",
        unique_id="model.t.m",
        file_path="models/m.sql",
        properties={"partitioning": "ARRAY['period_month']"},
    )
    assert node.partitioned_by == "ARRAY['period_month']"
