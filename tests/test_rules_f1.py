"""F1 — the fan-out family.

Two properties are being protected here. A genuine fan-out must be flagged even when
grain is only weakly derived (the normal state in a project with no tests), and a join
onto a *proven* key must not be flagged at all — a rule that cries wolf on safe joins
gets switched off, and then it catches nothing.
"""

from __future__ import annotations

from themis.analyze.parse import parse_sql, resolve_relation
from themis.models import Backend, Confidence, Grain, GrainSource, Severity
from themis.rules.base import RuleContext
from themis.rules.families.f1_grain import (
    GroupByGrainChangedRule,
    JoinFanOutRule,
    JoinTypeChangedRule,
)
from themis.snapshot import ModelNode, ProjectSnapshot

SAFE_JOIN = """
with entries as (select * from stg_entries),
     rates as (select * from stg_rates)
select e.id, r.rate
from entries e
inner join rates r on e.ccy = r.ccy and e.period = r.period
"""

FANOUT_JOIN = """
with entries as (select * from stg_entries),
     rates as (select * from stg_rates)
select e.id, r.rate
from entries e
inner join rates r on e.ccy = r.ccy
"""


def _model(name: str, sql: str) -> ModelNode:
    return ModelNode(
        name=name,
        unique_id=f"model.t.{name}",
        file_path=f"models/{name}.sql",
        raw_sql=sql,
        compiled_sql=sql,
    )


def _ctx(before_sql: str | None, after_sql: str, grains: dict[str, Grain]) -> RuleContext:
    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)
    return RuleContext(
        model_name="m",
        before=_model("m", before_sql) if before_sql else None,
        after=_model("m", after_sql),
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains=grains,
    )


def _grain(source: GrainSource, *columns: str) -> Grain:
    return Grain(model_name="stg_rates", columns=columns, source=source)


def test_cte_alias_resolves_to_the_underlying_model() -> None:
    """dbt wraps every ref() in a CTE, so without this the grain lookup always misses."""
    tree = parse_sql(FANOUT_JOIN)
    assert resolve_relation(tree, "rates") == "stg_rates"


def test_dropped_join_key_is_flagged() -> None:
    findings = JoinFanOutRule().check(
        _ctx(SAFE_JOIN, FANOUT_JOIN, {"stg_rates": _grain(GrainSource.STRUCTURAL, "ccy", "period")})
    )
    assert len(findings) == 1
    assert findings[0].confidence is Confidence.LIKELY
    assert "stg_rates" in findings[0].title


def test_join_covering_a_proven_key_is_not_flagged() -> None:
    """The false-positive case. Flagging this is how the rule gets switched off."""
    findings = JoinFanOutRule().check(
        _ctx(None, SAFE_JOIN, {"stg_rates": _grain(GrainSource.STRUCTURAL, "ccy", "period")})
    )
    assert findings == []


def test_weak_grain_does_not_suppress_a_fan_out() -> None:
    """A heuristic key is most likely to be incomplete — exactly what a fan-out hides
    behind — so it must never be treated as proof of safety."""
    findings = JoinFanOutRule().check(
        _ctx(None, FANOUT_JOIN, {"stg_rates": _grain(GrainSource.HEURISTIC, "ccy")})
    )
    assert len(findings) == 1
    assert findings[0].confidence is Confidence.POSSIBLE


def test_weak_grain_evidence_is_not_self_contradictory() -> None:
    """It must not claim the key 'does not cover' a grain the key literally equals."""
    findings = JoinFanOutRule().check(
        _ctx(None, FANOUT_JOIN, {"stg_rates": _grain(GrainSource.HEURISTIC, "ccy")})
    )
    note = findings[0].evidence.note or ""
    assert "not proven" in note
    assert "does not cover" not in note


def test_unchanged_join_is_not_reported() -> None:
    """Only what this change introduced is this review's business."""
    assert JoinFanOutRule().check(_ctx(FANOUT_JOIN, FANOUT_JOIN, {})) == []


def test_unknown_grain_is_flagged_as_possible_not_certain() -> None:
    findings = JoinFanOutRule().check(_ctx(None, FANOUT_JOIN, {}))
    assert len(findings) == 1
    assert findings[0].confidence is Confidence.POSSIBLE
    assert "could not be derived" in (findings[0].evidence.note or "")


def test_left_to_inner_flip_is_proven_and_names_row_loss() -> None:
    before = "select 1 from a left join b on a.id = b.id"
    after = "select 1 from a inner join b on a.id = b.id"
    findings = JoinTypeChangedRule().check(_ctx(before, after, {}))
    assert len(findings) == 1
    assert findings[0].confidence is Confidence.PROVEN
    assert "dropped" in findings[0].consequence


def test_inner_to_left_flip_names_null_introduction() -> None:
    before = "select 1 from a inner join b on a.id = b.id"
    after = "select 1 from a left join b on a.id = b.id"
    findings = JoinTypeChangedRule().check(_ctx(before, after, {}))
    assert len(findings) == 1
    assert "NULL" in findings[0].consequence


def test_reformatting_a_join_is_not_a_type_change() -> None:
    before = "select 1 from a inner join b on a.id = b.id"
    after = "SELECT 1\nFROM a\nINNER JOIN b\n  ON a.id = b.id"
    assert JoinTypeChangedRule().check(_ctx(before, after, {})) == []


def test_group_by_key_change_is_reported_with_both_directions() -> None:
    before = "select a, b, sum(x) as s from t group by a, b"
    after = "select a, sum(x) as s from t group by a"
    findings = GroupByGrainChangedRule().check(_ctx(before, after, {}))
    assert len(findings) == 1
    assert "b" in findings[0].consequence


def test_governed_model_escalates_severity() -> None:
    """A regulatory figure is not an ordinary mart."""
    snapshot = ProjectSnapshot(revision="r", backend=Backend.MANIFEST)
    after = _model("m", FANOUT_JOIN).model_copy(update={"tags": ("regulatory",)})
    ctx = RuleContext(
        model_name="m",
        before=None,
        after=after,
        before_snapshot=snapshot,
        after_snapshot=snapshot,
        grains={},
    )
    findings = JoinFanOutRule().check(ctx)
    assert findings[0].severity is Severity.CRITICAL


# --- false-positive control ---------------------------------------------------
#
# These are the cases that decide whether the tool stays switched on. A rule that
# fires on a behaviour-preserving refactor trains reviewers to ignore the family,
# and then it catches nothing when a real fan-out arrives.

RENAMED_CTES = """
with fx as (select * from stg_entries),
     rate_table as (select * from stg_rates)
select f.id, r.rate
from fx f
inner join rate_table r on f.ccy = r.ccy and f.period = r.period
"""


def test_renaming_a_cte_is_not_a_new_join() -> None:
    """Joins must be compared by the model they resolve to, never by CTE alias.

    Renaming CTEs is a routine tidy-up. Comparing aliases makes every join in a
    refactored model look new — the exact failure that turned a control set of
    behaviour-preserving refactors into a wall of false positives.
    """
    assert JoinFanOutRule().check(_ctx(SAFE_JOIN, RENAMED_CTES, {})) == []


def test_renaming_a_cte_is_not_a_join_type_change() -> None:
    assert JoinTypeChangedRule().check(_ctx(SAFE_JOIN, RENAMED_CTES, {})) == []


def test_a_real_fan_out_still_fires_after_a_rename() -> None:
    """The rename fix must not suppress genuine changes hiding inside a refactor."""
    renamed_and_broken = """
    with fx as (select * from stg_entries),
         rate_table as (select * from stg_rates)
    select f.id, r.rate
    from fx f
    inner join rate_table r on f.ccy = r.ccy
    """
    findings = JoinFanOutRule().check(_ctx(SAFE_JOIN, renamed_and_broken, {}))
    assert len(findings) == 1
    assert "stg_rates" in findings[0].title
