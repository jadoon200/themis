"""A manifest written by a dbt release older than the one this project develops against.

The work project will not be on the same dbt version, and the compiled manifest is the
primary grounding backend: if a field moves between releases, every later stage reads
something subtly different and nothing says so. The fixture here is the real article — the
demo project compiled by dbt 1.8.10 — rather than a hand-written approximation, because a
fixture that was never produced by dbt cannot show that dbt still produces it.

`python scripts/dbt_versions.py` is the wider sweep: it builds an environment per release,
compiles the project with each, and compares every model the loader reads. dbt 1.8, 1.9,
1.10 and 1.12 all emit manifest v12 and all parse identically. This test is the part of
that which can run in CI with no network and no dbt of another version installed.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from themis.acquire.manifest import VERIFIED_MANIFEST_SCHEMAS, _schema_version, load_manifest
from themis.models import Backend
from themis.snapshot import ProjectSnapshot

FIXTURE = Path(__file__).parent / "fixtures" / "manifest-dbt-1.8.json.gz"


@pytest.fixture(scope="module")
def dbt_18(tmp_path_factory: pytest.TempPathFactory) -> ProjectSnapshot:
    path = tmp_path_factory.mktemp("dbt18") / "manifest.json"
    path.write_bytes(gzip.decompress(FIXTURE.read_bytes()))
    return load_manifest(path, revision="dbt-1.8", backend=Backend.MANIFEST)


def test_the_fixture_really_is_an_older_dbt(tmp_path: Path) -> None:
    payload = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    metadata = payload["metadata"]
    assert metadata["dbt_version"].startswith("1.8.")
    assert _schema_version(metadata) in VERIFIED_MANIFEST_SCHEMAS


def test_every_field_a_later_stage_depends_on_is_read(dbt_18: ProjectSnapshot) -> None:
    """Not "it loaded" — the specific fields the rules, grain and execution all read."""
    assert len(dbt_18.models) == 20
    assert len(dbt_18.macros) == 12
    assert sum(1 for model in dbt_18.models.values() if model.is_seed) == 4
    assert dbt_18.has_compiled_sql

    incremental = dbt_18.models["fct_revenue_incremental"]
    assert incremental.materialization == "incremental"
    assert incremental.incremental_strategy == "delete+insert"
    assert incremental.unique_key == ("entry_id",)

    governed = dbt_18.models["fct_regulatory_summary"]
    assert set(governed.tags) == {"regulatory", "recon"}
    assert [d.split(".")[-1] for d in governed.depends_on_models] == ["fct_revenue"]
    assert governed.file_path.endswith("fct_regulatory_summary.sql")

    # Jinja expanded, which is the whole reason a *compiled* manifest is mandatory.
    entries = dbt_18.models["stg_gl_entries"]
    assert entries.compiled_sql and "{{" not in entries.compiled_sql


def test_the_dependency_graph_is_the_same_shape(dbt_18: ProjectSnapshot) -> None:
    assert "fct_regulatory_summary" in dbt_18.downstream_of("stg_gl_entries")
    assert dbt_18.downstream_of("fct_regulatory_summary") == ()


def test_an_unrecognised_schema_version_is_a_warning_and_not_a_refusal(tmp_path: Path) -> None:
    """A newer dbt most likely still provides what THEMIS reads.

    Failing closed here would block a review for a version that works, and passing in
    silence would let a moved field become a quiet misreading. So: it loads, and it says so.
    """
    payload = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    payload["metadata"]["dbt_schema_version"] = "https://schemas.getdbt.com/dbt/manifest/v99.json"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))

    snapshot = load_manifest(path, revision="future", backend=Backend.MANIFEST)
    assert len(snapshot.models) == 20
    assert _schema_version(payload["metadata"]) == "v99"
    assert "v99" not in VERIFIED_MANIFEST_SCHEMAS


def test_a_manifest_with_no_schema_version_still_loads(tmp_path: Path) -> None:
    payload = json.loads(gzip.decompress(FIXTURE.read_bytes()))
    payload["metadata"].pop("dbt_schema_version", None)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload))
    assert len(load_manifest(path, revision="none", backend=Backend.MANIFEST).models) == 20
