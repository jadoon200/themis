"""The compiled-manifest cache, and the two cases where it must refuse.

Speed is the easy half. The half that matters is knowing when a git revision does
*not* determine the compiled SQL, because serving a stale manifest would make the
semantic diff report changes nobody made — or stay silent about ones they did.
"""

from __future__ import annotations

import json
from pathlib import Path

from themis.acquire.cache import CacheKey, ManifestCache
from themis.models import Backend
from themis.snapshot import MacroNode, ModelNode, ProjectSnapshot


def _snapshot(*, data_dependent: bool = False) -> ProjectSnapshot:
    macros = {}
    depends: tuple[str, ...] = ()
    if data_dependent:
        macros["entity_case"] = MacroNode(
            name="entity_case",
            unique_id="macro.d.entity_case",
            file_path="macros/generated.sql",
            raw_sql="{% macro entity_case() %}{% set r = run_query('select 1') %}{% endmacro %}",
        )
        depends = ("macro.d.entity_case",)
    return ProjectSnapshot(
        revision="abc123",
        backend=Backend.MANIFEST,
        macros=macros,
        models={
            "m": ModelNode(
                name="m",
                unique_id="model.d.m",
                file_path="models/m.sql",
                compiled_sql="select 1",
                depends_on_macros=depends,
            )
        },
    )


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"nodes": {}}))
    return path


def _key() -> CacheKey:
    return CacheKey(revision="abc1234567890", target="dev", project="demo_project")


def test_a_stored_manifest_is_read_back(tmp_path: Path) -> None:
    cache = ManifestCache(tmp_path)
    assert cache.get(_key()) is None
    cache.put(_key(), _manifest(tmp_path), _snapshot())
    assert cache.get(_key()) is not None


def test_a_project_whose_sql_comes_from_data_is_refused(tmp_path: Path) -> None:
    """The same revision compiles differently as the warehouse moves.

    Caching it would hand a later review compiled SQL built from older data, and the
    diff would then describe a change that never happened.
    """
    cache = ManifestCache(tmp_path)
    assert cache.put(_key(), _manifest(tmp_path), _snapshot(data_dependent=True)) is None
    assert cache.get(_key()) is None


def test_the_target_is_part_of_the_key(tmp_path: Path) -> None:
    """Two targets compile the same code to different catalogs and schemas.

    Serving one for the other would put the wrong relation names in front of every
    rule that reads a table reference.
    """
    cache = ManifestCache(tmp_path)
    cache.put(_key(), _manifest(tmp_path), _snapshot())
    other = CacheKey(revision="abc1234567890", target="prod_ci", project="demo_project")
    assert cache.get(other) is None


def test_the_project_is_part_of_the_key(tmp_path: Path) -> None:
    """A monorepo compiles several projects at one revision."""
    cache = ManifestCache(tmp_path)
    cache.put(_key(), _manifest(tmp_path), _snapshot())
    other = CacheKey(revision="abc1234567890", target="dev", project="other_project")
    assert cache.get(other) is None


def test_a_corrupt_entry_costs_a_recompile_not_a_run(tmp_path: Path) -> None:
    """A half-written file must read as a miss, never raise into the pipeline."""
    cache = ManifestCache(tmp_path)
    cache.put(_key(), _manifest(tmp_path), _snapshot())
    cache.path_for(_key()).write_text("{ truncated")
    assert cache.get(_key()) is None
    assert not cache.path_for(_key()).exists()


def test_a_disabled_cache_neither_reads_nor_writes(tmp_path: Path) -> None:
    cache = ManifestCache(tmp_path, enabled=False)
    assert cache.put(_key(), _manifest(tmp_path), _snapshot()) is None
    assert cache.get(_key()) is None


def test_clearing_reports_what_it_removed(tmp_path: Path) -> None:
    cache = ManifestCache(tmp_path)
    cache.put(_key(), _manifest(tmp_path), _snapshot())
    assert cache.clear() == 1
    assert cache.get(_key()) is None
