"""dbt snapshots: loaded, keyed, traced, and reviewed.

A snapshot is the one node whose table cannot be rebuilt, and until now THEMIS did not
read one at all: a change to it was reported as not analysed, and a model reading it had
no edge back to it. At work the slowly-changing data is snapshots Dagster runs into
Iceberg, so the first question any of this has to answer is whether it holds on what dbt
really writes. The project below is compiled by dbt, not described by hand — two
snapshots, one in the current spelling and one in the legacy one, a model taking the
current version and a model keeping every version.

Rule cases are edits to those compiled nodes: a config edit is what the manifest would
say after it, and a SQL edit is made to the compiled text dbt produced.
"""

from __future__ import annotations

import duckdb
import pytest
import yaml

from themis.acquire.dbt_runner import compile_project
from themis.acquire.manifest import load_manifest
from themis.analyze import history
from themis.analyze.grain import infer_grains, query_grain
from themis.analyze.lineage import build_column_graph
from themis.analyze.parse import parse_sql
from themis.analyze.volatility import volatile_columns
from themis.models import Backend, GrainSource
from themis.rules.base import RuleContext
from themis.snapshot import ModelNode, ProjectSnapshot

SOURCES = {
    "version": 2,
    "sources": [{"name": "crm", "schema": "main", "tables": [{"name": "accounts"}]}],
    # One declared key, so the query's grain can be proven here. The project at work
    # declares none; `_untested` below is that project.
    "models": [
        {
            "name": "stg_accounts",
            "columns": [{"name": "account_id", "data_tests": ["unique"]}],
        }
    ],
}

STG_ACCOUNTS = """
select account_id, account_name, segment, updated_at from {{ source('crm', 'accounts') }}
"""

SNAP_ACCOUNTS = """
{% snapshot snap_accounts %}
{{ config(
    schema='history',
    unique_key='account_id',
    strategy='timestamp',
    updated_at='updated_at',
    hard_deletes='invalidate'
) }}
select account_id, account_name, segment, updated_at from {{ ref('stg_accounts') }}
{% endsnapshot %}
"""

# The spelling most existing projects still use: a fixed target_schema, and the boolean
# that came before `hard_deletes`.
SNAP_LEGACY = """
{% snapshot snap_segments %}
{{ config(
    target_schema='snapshots',
    unique_key='account_id',
    strategy='check',
    check_cols='all',
    invalidate_hard_deletes=True
) }}
select account_id, segment from {{ ref('stg_accounts') }}
{% endsnapshot %}
"""

CURRENT = """
with snap as (
    select * from {{ ref('snap_accounts') }}
)
select account_id, account_name, segment
from snap
where dbt_valid_to is null
"""

HISTORY = """
select account_id, segment, dbt_valid_from, dbt_valid_to
from {{ ref('snap_accounts') }}
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> ProjectSnapshot:
    root = tmp_path_factory.mktemp("snapshot_project")
    database = root / "warehouse.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "create table accounts as select * from (values "
            "('A1', 'Trading', 'corporate', timestamp '2026-01-01 09:00:00'), "
            "('A2', 'Treasury', 'retail', timestamp '2026-01-02 09:00:00')"
            ") as t(account_id, account_name, segment, updated_at)"
        )
    finally:
        connection.close()

    (root / "dbt_project.yml").write_text(
        yaml.safe_dump({"name": "history", "profile": "history", "version": "1.0.0"})
    )
    (root / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "history": {
                    "target": "dev",
                    "outputs": {"dev": {"type": "duckdb", "path": str(database)}},
                }
            }
        )
    )
    models = root / "models"
    models.mkdir()
    (models / "_sources.yml").write_text(yaml.safe_dump(SOURCES))
    (models / "stg_accounts.sql").write_text(STG_ACCOUNTS)
    (models / "dim_accounts_current.sql").write_text(CURRENT)
    (models / "fct_account_history.sql").write_text(HISTORY)
    snapshots = root / "snapshots"
    snapshots.mkdir()
    (snapshots / "snap_accounts.sql").write_text(SNAP_ACCOUNTS)
    (snapshots / "snap_segments.sql").write_text(SNAP_LEGACY)

    compiled = compile_project(
        root,
        target="dev",
        allowed_targets=("dev",),
        profiles_dir=root,
        target_path=root / "target",
    )
    return load_manifest(compiled.manifest_path, revision="r", backend=Backend.MANIFEST)


def _ctx(
    project: ProjectSnapshot,
    name: str,
    *,
    before: ModelNode | None = None,
    after: ModelNode | None = None,
) -> RuleContext:
    original = project.models[name]
    head = after if after is not None else original
    after_project = project.model_copy(update={"models": {**project.models, name: head}})
    return RuleContext(
        model_name=name,
        before=before if before is not None else original,
        after=head,
        before_snapshot=project,
        after_snapshot=after_project,
        grains=infer_grains(after_project),
    )


# --- loaded as what they are ------------------------------------------------------------


def test_a_snapshot_is_a_node_with_its_history_settings(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    assert snap.is_snapshot and snap.materialization == "snapshot"
    assert snap.history is not None
    assert snap.history.strategy == "timestamp"
    assert snap.history.updated_at == "updated_at"
    assert snap.history.hard_deletes == "invalidate"
    assert snap.unique_key == ("account_id",)
    assert snap.history.fixed_schema is None
    # The query, compiled: what grain and lineage read.
    assert "account_name" in (snap.compiled_sql or "")


def test_the_legacy_spelling_means_the_same_thing(project: ProjectSnapshot) -> None:
    legacy = project.models["snap_segments"].history
    assert legacy is not None
    assert legacy.hard_deletes == "invalidate"  # from invalidate_hard_deletes=True
    assert legacy.checks_all
    assert legacy.fixed_schema == "snapshots"


def test_a_model_reading_a_snapshot_keeps_its_edge(project: ProjectSnapshot) -> None:
    """Dependencies were filtered to models and seeds, so a mart on a snapshot had none."""
    assert "dim_accounts_current" in project.downstream_of("snap_accounts")
    assert "snap_accounts" in project.downstream_of("stg_accounts")
    assert "dim_accounts_current" in project.downstream_of("stg_accounts")


def test_a_changed_snapshot_resolves_to_its_node(project: ProjectSnapshot) -> None:
    from themis.acquire.git import ChangedFile
    from themis.acquire.snapshot_builder import AcquireResult

    acquired = AcquireResult(
        before=project,
        after=project,
        changed=(ChangedFile(path="snapshots/snap_accounts.sql", status="M"),),
    )
    assert acquired.changed_models == ("snap_accounts",)
    assert acquired.unanalysed_changes == ()


# --- two grains ---------------------------------------------------------------------------


def test_the_table_is_keyed_by_version_and_the_query_by_key(project: ProjectSnapshot) -> None:
    grains = infer_grains(project)
    table = grains["snap_accounts"]
    assert table.columns == ("account_id", "dbt_valid_from")
    assert table.is_proven
    query = query_grain(project.models["snap_accounts"], project, grains)
    assert query.columns == ("account_id",)


def test_taking_the_current_version_gives_the_key_back(project: ProjectSnapshot) -> None:
    """Through a CTE and filtered after it — how dbt models are actually written."""
    grains = infer_grains(project)
    current = grains["dim_accounts_current"]
    assert current.columns == ("account_id",)
    assert current.source is GrainSource.PROPAGATED


def test_keeping_every_version_keeps_the_version_in_the_key(project: ProjectSnapshot) -> None:
    grains = infer_grains(project)
    assert grains["fct_account_history"].columns == ("account_id", "dbt_valid_from")


def _untested(project: ProjectSnapshot) -> ProjectSnapshot:
    """The same project with no declared tests — the shape of the project at work."""
    return project.model_copy(update={"tests": ()})


def test_with_nothing_declared_the_key_is_taken_from_config(project: ProjectSnapshot) -> None:
    """A staging model on a source, with no test: the query's grain cannot be derived.

    The table's key then rests on the snapshot's own unique_key, as an incremental
    model's does — and F9001 is where that is questioned, when a change puts it in doubt.
    """
    untested = _untested(project)
    grains = infer_grains(untested)
    assert grains["snap_accounts"].source is GrainSource.CONFIG
    assert grains["snap_accounts"].columns == ("account_id", "dbt_valid_from")
    query = query_grain(untested.models["snap_accounts"], untested, grains)
    assert query.source is GrainSource.UNKNOWN


def test_a_key_that_does_not_identify_a_row_is_not_a_grain(project: ProjectSnapshot) -> None:
    """Were it kept, every join onto the snapshot on that key would read as safe."""
    snap = project.models["snap_accounts"].model_copy(update={"unique_key": ("segment",)})
    rekeyed = project.model_copy(update={"models": {**project.models, "snap_accounts": snap}})
    grains = infer_grains(rekeyed)
    assert grains["snap_accounts"].source is GrainSource.UNKNOWN
    assert not grains["dim_accounts_current"].is_proven


# --- reading one version, or all of them ---------------------------------------------------


def test_reads_are_told_apart(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    current = parse_sql(project.models["dim_accounts_current"].compiled_sql or "")
    whole = parse_sql(project.models["fct_account_history"].compiled_sql or "")
    assert history.restricted_to_one_version(current, snap)
    assert not history.restricted_to_one_version(whole, snap)
    # Every version, handed on: a history model, not the mistake.
    assert history.keeps_versions(whole, snap)
    assert not history.reads_every_version(whole, snap)


def test_an_as_of_join_takes_one_version() -> None:
    snap = ModelNode(
        name="snap",
        unique_id="snapshot.p.snap",
        file_path="snapshots/snap.sql",
        resource_type="snapshot",
        relation_name='"db"."history"."snap"',
        unique_key=("account_id",),
        history={"strategy": "timestamp", "updated_at": "updated_at"},
    )
    tree = parse_sql(
        'select e.entry_id, s.segment from "db"."main"."entries" as e '
        'join "db"."history"."snap" as s on s.account_id = e.account_id '
        "and e.posted_at >= s.dbt_valid_from "
        "and e.posted_at < coalesce(s.dbt_valid_to, timestamp '9999-12-31')"
    )
    assert history.restricted_to_one_version(tree, snap)


# --- lineage and volatility -----------------------------------------------------------------


def test_the_bookkeeping_columns_are_part_of_the_table(project: ProjectSnapshot) -> None:
    graph = build_column_graph(project)
    assert "dbt_valid_to" in graph.outputs["snap_accounts"]
    assert "dim_accounts_current" in graph.referencing_models("snap_accounts", "dbt_valid_to")
    sources = {str(ref) for ref in graph.sources_of("dim_accounts_current", "segment")}
    assert "snap_accounts.segment" in sources


def test_only_a_check_snapshot_stamps_its_versions_at_build_time(
    project: ProjectSnapshot,
) -> None:
    stamped = volatile_columns(project)
    assert "dbt_valid_from" in stamped.get("snap_segments", frozenset())
    assert "snap_accounts" not in stamped  # timestamp: the source's own updated_at


# --- F9 ------------------------------------------------------------------------------------------


def _rules() -> dict[str, object]:
    from themis.rules.families import f9_history

    return {rule.rule_id: rule for rule in f9_history.RULES}


def _check(rule_id: str, ctx: RuleContext) -> list:
    rule = _rules()[rule_id]
    assert rule.applies_to(ctx)  # type: ignore[attr-defined]
    return rule.check(ctx)  # type: ignore[attr-defined]


def test_an_unchanged_snapshot_says_nothing(project: ProjectSnapshot) -> None:
    for rule_id in _rules():
        for name in ("snap_accounts", "snap_segments", "dim_accounts_current"):
            assert _check(rule_id, _ctx(project, name)) == [], (rule_id, name)


def test_a_coarser_key_is_flagged_as_not_identifying_a_row(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    ctx = _ctx(project, "snap_accounts", after=snap.model_copy(update={"unique_key": ("segment",)}))
    (finding,) = _check("F9001", ctx)
    assert "MERGE_TARGET_ROW_MULTIPLE_MATCHES" in finding.consequence
    assert finding.confidence.value == "likely"  # the query is proven unique on another key
    (rekeyed,) = _check("F9002", ctx)
    assert "account_id" in (rekeyed.evidence.note or "")


def test_with_nothing_declared_a_new_key_is_a_question(project: ProjectSnapshot) -> None:
    untested = _untested(project)
    snap = untested.models["snap_accounts"]
    rekeyed = snap.model_copy(update={"unique_key": ("segment",)})
    ctx = _ctx(untested, "snap_accounts", after=rekeyed)
    (finding,) = _check("F9001", ctx)
    assert finding.confidence.value == "possible"
    # And an edit that leaves the key and the query's shape alone asks nothing.
    assert _check("F9001", _ctx(untested, "snap_accounts")) == []


def test_strategy_and_updated_at_edits_are_flagged(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    assert snap.history is not None
    to_check = snap.model_copy(
        update={"history": snap.history.model_copy(update={"strategy": "check"})}
    )
    (strategy,) = _check("F9003", _ctx(project, "snap_accounts", after=to_check))
    assert "timestamp to check" in strategy.title
    moved_clock = snap.model_copy(
        update={"history": snap.history.model_copy(update={"updated_at": "created_at"})}
    )
    (clock,) = _check("F9003", _ctx(project, "snap_accounts", after=moved_clock))
    assert "created_at" in clock.title


def test_narrowing_check_cols_is_flagged_and_widening_is_not(project: ProjectSnapshot) -> None:
    legacy = project.models["snap_segments"]
    assert legacy.history is not None
    listed = legacy.model_copy(
        update={"history": legacy.history.model_copy(update={"check_cols": ("segment",)})}
    )
    (narrowed,) = _check("F9003", _ctx(project, "snap_segments", after=listed))
    assert narrowed.severity.value == "medium"
    # From a list back to all of them: records changes it used to miss, loses none.
    assert _check("F9003", _ctx(project, "snap_segments", before=listed)) == []


def test_an_updated_at_stamped_at_build_time_versions_every_row(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    sql = (snap.compiled_sql or "").replace(
        "updated_at from", "current_timestamp as updated_at from"
    )
    assert sql != snap.compiled_sql
    (finding,) = _check(
        "F9004", _ctx(project, "snap_accounts", after=snap.model_copy(update={"compiled_sql": sql}))
    )
    assert finding.confidence.value == "proven"


def test_deletions_switched_off_and_filters_that_fake_them(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    assert snap.history is not None
    ignoring = snap.model_copy(
        update={"history": snap.history.model_copy(update={"hard_deletes": "ignore"})}
    )
    (off,) = _check("F9005", _ctx(project, "snap_accounts", after=ignoring))
    assert "stops recording deletions" in off.title

    sql = (snap.compiled_sql or "").rstrip() + "\nwhere segment <> 'retail'"
    filtered = snap.model_copy(update={"compiled_sql": sql})
    (faked,) = _check("F9005", _ctx(project, "snap_accounts", after=filtered))
    assert "recorded as deleted" in faked.title
    # The same filter where deletions are not tracked removes rows from tracking, and
    # records nothing false.
    untracked = ignoring.model_copy(update={"compiled_sql": sql})
    assert _check("F9005", _ctx(project, "snap_accounts", before=ignoring, after=untracked)) == []


def test_moving_a_snapshot_leaves_its_history_behind(project: ProjectSnapshot) -> None:
    snap = project.models["snap_accounts"]
    moved = snap.model_copy(
        update={"relation_name": (snap.relation_name or "").replace("history", "scd")}
    )
    (finding,) = _check("F9006", _ctx(project, "snap_accounts", after=moved))
    assert "stays behind" in finding.title


def test_removing_the_current_version_filter_is_flagged(project: ProjectSnapshot) -> None:
    model = project.models["dim_accounts_current"]
    sql = (model.compiled_sql or "").replace("where dbt_valid_to is null", "")
    assert sql != model.compiled_sql
    after = model.model_copy(update={"compiled_sql": sql})
    (finding,) = _check("F9007", _ctx(project, "dim_accounts_current", after=after))
    assert finding.evidence.related_model == "snap_accounts"


def test_a_new_model_reading_history_on_purpose_is_left_alone(project: ProjectSnapshot) -> None:
    ctx = _ctx(project, "fct_account_history")
    ctx.before = None
    assert _check("F9007", ctx) == []


def test_a_new_model_reading_every_version_as_if_current_is_flagged(
    project: ProjectSnapshot,
) -> None:
    model = project.models["fct_account_history"]
    sql = (model.compiled_sql or "").replace(", dbt_valid_from, dbt_valid_to", "")
    assert sql != model.compiled_sql
    ctx = _ctx(project, "fct_account_history", after=model.model_copy(update={"compiled_sql": sql}))
    ctx.before = None
    (finding,) = _check("F9007", ctx)
    assert "every version" in finding.title


# --- where they are written ------------------------------------------------------------------


def test_a_snapshot_on_hive_is_flagged(project: ProjectSnapshot) -> None:
    from themis.rules.families.f8_engine import SnapshotOnHiveRule

    snap = project.models["snap_accounts"]
    on_hive = snap.model_copy(update={"relation_name": '"hive"."main_history"."snap_accounts"'})
    (finding,) = SnapshotOnHiveRule().check(_ctx(project, "snap_accounts", after=on_hive))
    assert finding.rule_id == "F8007"
    assert SnapshotOnHiveRule().check(_ctx(project, "snap_accounts")) == []


# --- joins onto a snapshot ------------------------------------------------------------------------


def _join_ctx(project: ProjectSnapshot, predicate: str) -> RuleContext:
    relation = project.models["snap_accounts"].relation_name
    sql = (
        f'select e.entry_id, s.segment from "warehouse"."main"."entries" as e '
        f"join {relation} as s on s.account_id = e.account_id{predicate}"
    )
    node = ModelNode(
        name="fct_entries",
        unique_id="model.history.fct_entries",
        file_path="models/fct_entries.sql",
        compiled_sql=sql,
        depends_on_models=("snapshot.history.snap_accounts",),
    )
    models = {**project.models, "fct_entries": node}
    after = project.model_copy(update={"models": models})
    return RuleContext(
        model_name="fct_entries",
        before=None,
        after=node,
        before_snapshot=project,
        after_snapshot=after,
        grains=infer_grains(after),
    )


def test_a_join_to_the_current_version_on_the_key_is_safe(project: ProjectSnapshot) -> None:
    from themis.rules.families.f1_grain import JoinFanOutRule

    ctx = _join_ctx(project, " and s.dbt_valid_to is null")
    assert JoinFanOutRule().check(ctx) == []
    assert _check("F9007", ctx) == []


def test_a_join_to_every_version_is_one_finding_that_says_why(project: ProjectSnapshot) -> None:
    """F1001 sees a key that does not cover the table's; F9007 says why, and ranks above."""
    from themis.rules.families.f1_grain import JoinFanOutRule
    from themis.triage.rubric import triage

    ctx = _join_ctx(project, "")
    fanout = JoinFanOutRule().check(ctx)
    versions = _check("F9007", ctx)
    assert fanout and versions
    ranked = {t.finding.rule_id: t for t in triage([*fanout, *versions])}
    assert ranked["F1001"].subsumed_by == "F9007"


def test_the_history_reviewer_takes_f9() -> None:
    from themis.review.specialists import specialist_for

    specialist = specialist_for("F9")
    assert specialist is not None and specialist.name == "history"


def test_a_snapshot_key_change_is_not_called_an_incremental_one(project: ProjectSnapshot) -> None:
    from themis.rules.families.f5_incremental import IncrementalKeyChangedRule

    snap = project.models["snap_accounts"]
    rekeyed = snap.model_copy(update={"unique_key": ("account_id", "segment")})
    assert IncrementalKeyChangedRule().check(_ctx(project, "snap_accounts", after=rekeyed)) == []
