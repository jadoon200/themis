"""F6 — a column removed while something downstream still depends on it.

The rule's whole difficulty is the consumer list. Column lineage answers it; the name
search that used to answer it is kept only for models lineage cannot resolve, and the
two disagree in both directions, so both paths are tested.
"""

from __future__ import annotations

from themis.analyze.lineage import LineageIndex
from themis.models import Backend, Confidence
from themis.rules.base import RuleContext
from themis.rules.families.f6_contracts import ColumnRemovedWithConsumersRule
from themis.snapshot import ModelNode, ProjectSnapshot

STG_BEFORE = "select id, amount, currency from raw_entries"
STG_AFTER = "select id, amount from raw_entries"

# Reads everything through a star, so `currency` appears nowhere in its own SQL.
MART_STAR = "with s as (select * from stg) select * from s"
# Names a column of its own called `currency`, sourced from somewhere else entirely.
MART_UNRELATED = "select id, 'USD' as currency from stg"


def _model(name: str, sql: str, *, depends: tuple[str, ...] = ()) -> ModelNode:
    return ModelNode(
        name=name,
        unique_id=f"model.t.{name}",
        file_path=f"models/{name}.sql",
        raw_sql=sql,
        compiled_sql=sql,
        depends_on_models=tuple(f"model.t.{d}" for d in depends),
    )


def _snapshot(stg_sql: str, mart_sql: str) -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "stg": _model("stg", stg_sql),
            "mart": _model("mart", mart_sql, depends=("stg",)),
        },
        child_map={"stg": ("mart",)},
    )


def _ctx(mart_sql: str, *, with_lineage: bool) -> RuleContext:
    before = _snapshot(STG_BEFORE, mart_sql)
    after = _snapshot(STG_AFTER, mart_sql)
    lineage = LineageIndex(before_snapshot=before, after_snapshot=after) if with_lineage else None
    return RuleContext(
        model_name="stg",
        before=before.models["stg"],
        after=after.models["stg"],
        before_snapshot=before,
        after_snapshot=after,
        grains={},
        lineage=lineage,
    )


def test_a_star_reading_consumer_is_reported() -> None:
    """The case the name search cannot see: the consumer never writes the name."""
    findings = ColumnRemovedWithConsumersRule().check(_ctx(MART_STAR, with_lineage=True))
    assert len(findings) == 1
    assert findings[0].blast_radius == ("mart",)
    assert findings[0].confidence is Confidence.PROVEN
    assert "mart.currency" in (findings[0].evidence.note or "")


def test_the_name_search_alone_misses_the_star_reading_consumer() -> None:
    """Kept as a test so the regression is visible if lineage ever stops being used."""
    assert ColumnRemovedWithConsumersRule().check(_ctx(MART_STAR, with_lineage=False)) == []


def test_a_same_named_column_from_elsewhere_is_not_reported() -> None:
    """The other direction: the name is present downstream, the dependency is not."""
    assert ColumnRemovedWithConsumersRule().check(_ctx(MART_UNRELATED, with_lineage=True)) == []


def test_the_name_search_alone_reports_the_unrelated_column() -> None:
    findings = ColumnRemovedWithConsumersRule().check(_ctx(MART_UNRELATED, with_lineage=False))
    assert len(findings) == 1
    assert findings[0].confidence is Confidence.LIKELY
