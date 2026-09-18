"""A project whose staging models read `{{ source(...) }}`, which the demo project does not.

Every staging model in the demo project reads a seed through `ref()`, so nothing here had
ever been run against a `source()` — and at work that is how every staging model starts.
A source is not a node in `manifest.nodes`; it lives in `manifest.sources` and appears on a
model only as a `depends_on.nodes` entry of a different resource type. Any stage that
assumed a parent is always a model or a seed would be reading a project whose roots are
invisible to it.

So this compiles a real project, with dbt, that reads from a source: two staging models on
one source, a mart joining them, and a change to one of them. It is small and it is real —
the kind of thing the demo project cannot show because of how it happens to be built.
"""

from __future__ import annotations

import duckdb
import pytest
import yaml

from themis.acquire.dbt_runner import compile_project
from themis.acquire.manifest import load_manifest
from themis.analyze.grain import infer_grains
from themis.analyze.lineage import LineageIndex
from themis.models import Backend
from themis.snapshot import ProjectSnapshot

SOURCES = {
    "version": 2,
    "sources": [
        {
            "name": "raw",
            "schema": "main",
            "tables": [{"name": "entries"}, {"name": "accounts"}],
        }
    ],
}

STG_ENTRIES = """
select
    entry_id,
    account_id,
    cast(amount as decimal(18, 2)) as amount,
    currency_code
from {{ source('raw', 'entries') }}
"""

STG_ACCOUNTS = """
select account_id, account_name from {{ source('raw', 'accounts') }}
"""

MART = """
select
    e.account_id,
    a.account_name,
    sum(e.amount) as total_amount
from {{ ref('stg_entries') }} as e
inner join {{ ref('stg_accounts') }} as a
    on e.account_id = a.account_id
group by e.account_id, a.account_name
"""


@pytest.fixture(scope="module")
def source_project(tmp_path_factory: pytest.TempPathFactory) -> ProjectSnapshot:
    root = tmp_path_factory.mktemp("source_project")
    database = root / "warehouse.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "create table entries as select * from (values "
            "(1, 10, 100.00, 'USD'), (2, 10, 50.00, 'EUR'), (3, 11, 25.00, 'USD')"
            ") as t(entry_id, account_id, amount, currency_code)"
        )
        connection.execute(
            "create table accounts as select * from (values "
            "(10, 'Trading'), (11, 'Treasury')) as t(account_id, account_name)"
        )
    finally:
        connection.close()

    (root / "dbt_project.yml").write_text(
        yaml.safe_dump({"name": "sourced", "profile": "sourced", "version": "1.0.0"})
    )
    (root / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "sourced": {
                    "target": "dev",
                    # Absolute: where the code is is not where the data is.
                    "outputs": {"dev": {"type": "duckdb", "path": str(database)}},
                }
            }
        )
    )
    models = root / "models"
    models.mkdir()
    (models / "_sources.yml").write_text(yaml.safe_dump(SOURCES))
    (models / "stg_entries.sql").write_text(STG_ENTRIES)
    (models / "stg_accounts.sql").write_text(STG_ACCOUNTS)
    (models / "mart_totals.sql").write_text(MART)

    compiled = compile_project(
        root,
        target="dev",
        allowed_targets=("dev",),
        profiles_dir=root,
        target_path=root / "target",
    )
    return load_manifest(compiled.manifest_path, revision="sourced", backend=Backend.MANIFEST)


def test_a_model_reading_a_source_is_loaded_with_the_source_recorded(
    source_project: ProjectSnapshot,
) -> None:
    staging = source_project.models["stg_entries"]
    # No upstream *model*: its parent is a source, which is where a project really starts.
    assert staging.depends_on_models == ()
    assert any("raw" in source for source in staging.depends_on_sources)
    assert staging.compiled_sql and "{{" not in staging.compiled_sql
    # Compiled to the real relation, which is what every later stage reads.
    assert "entries" in staging.compiled_sql


def test_the_graph_below_a_source_is_intact(source_project: ProjectSnapshot) -> None:
    assert source_project.downstream_of("stg_entries") == ("mart_totals",)
    assert source_project.downstream_of("mart_totals") == ()


def test_grain_is_derived_for_a_source_rooted_model(source_project: ProjectSnapshot) -> None:
    """The reason this matters: grain is derived from SQL, and no test is declared."""
    grains = infer_grains(source_project, dialect="duckdb")
    assert grains["mart_totals"].columns == ("account_id", "account_name")
    # Unknown is a valid answer for a staging model; a crash, or a confident wrong key
    # because the source could not be read, is not.
    assert "stg_entries" in grains


def test_column_lineage_traces_through_a_source_rooted_model(
    source_project: ProjectSnapshot,
) -> None:
    index = LineageIndex(before_snapshot=source_project, after_snapshot=source_project)
    graph = index.after
    assert graph.is_traced("mart_totals")
    sources = {str(ref) for ref in graph.sources_of("mart_totals", "total_amount")}
    assert "stg_entries.amount" in sources
    # The source table's own columns are not traced, and saying so is the point: an
    # untraced root has to read as unknown, never as "computed from nothing".
    assert all(not ref.startswith("raw.") for ref in sources)
