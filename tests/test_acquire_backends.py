"""Backend selection — and, above all, saying so when it is not what was asked for.

THEMIS runs three grounding backends of different strength. Which one it got changes
what the review can see, so a caller who asked for the strong one and silently got a
weaker one would be reading a report that means something other than they think.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from themis.acquire.snapshot_builder import manifest_file
from themis.models import Backend


def test_a_directory_resolves_to_the_manifest_inside_it(tmp_path: Path) -> None:
    """dbt's own --state takes a directory, so one path should serve both flags."""
    (tmp_path / "manifest.json").write_text("{}")
    assert manifest_file(tmp_path) == tmp_path / "manifest.json"


def test_a_file_path_is_taken_as_given(tmp_path: Path) -> None:
    given = tmp_path / "prod-manifest.json"
    given.write_text("{}")
    assert manifest_file(given) == given


def test_a_missing_path_resolves_to_itself_rather_than_guessing(tmp_path: Path) -> None:
    """Nothing on disk to inspect, so the caller's path is reported back verbatim."""
    given = tmp_path / "nowhere.json"
    assert manifest_file(given) == given


def _write_manifest(path: Path, *, sql: str) -> None:
    path.write_text(
        json.dumps(
            {
                "nodes": {
                    "model.demo.m": {
                        "name": "m",
                        "resource_type": "model",
                        "original_file_path": "models/m.sql",
                        "raw_code": sql,
                        "compiled_code": sql,
                        "relation_name": '"db"."main"."m"',
                        "config": {"materialized": "table"},
                        "depends_on": {"nodes": [], "macros": []},
                    }
                },
                "macros": {},
                "exposures": {},
                "child_map": {},
            }
        )
    )


def test_a_production_manifest_loads_as_the_dual_manifest_backend(tmp_path: Path) -> None:
    """The point of backend A: the base costs nothing to obtain."""
    from themis.acquire.manifest import load_manifest

    _write_manifest(tmp_path / "manifest.json", sql="select a from t")
    snapshot = load_manifest(
        manifest_file(tmp_path), revision="abc123", backend=Backend.DUAL_MANIFEST
    )
    assert snapshot.backend is Backend.DUAL_MANIFEST
    assert snapshot.models["m"].analysable_sql == "select a from t"


def test_an_unusable_production_manifest_is_reported_not_swallowed(tmp_path: Path) -> None:
    """Falling back quietly would mean comparing against a different revision.

    The reviewer would read a report about base-versus-head believing it was
    production-versus-head, which is a different question with a different answer.
    """
    from themis.acquire.manifest import ManifestError, load_manifest

    (tmp_path / "manifest.json").write_text("not json at all")
    with pytest.raises(ManifestError):
        load_manifest(manifest_file(tmp_path), revision="abc", backend=Backend.DUAL_MANIFEST)


def test_a_dirty_working_tree_is_not_described_by_its_revision(tmp_path: Path) -> None:
    """The cleanliness check the head-caching decision rests on.

    A checkout with uncommitted edits compiles to something no SHA names, so caching
    it would serve one reviewer's unsaved work to the next run of that revision.
    """
    import subprocess

    from themis.acquire import git

    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "a.sql").write_text("select 1")
    run("add", "-A")
    run("commit", "-q", "-m", "first")
    assert git.is_clean(repo)

    (repo / "a.sql").write_text("select 2")
    assert not git.is_clean(repo)


def test_cleanliness_can_be_asked_about_one_path(tmp_path: Path) -> None:
    """A change elsewhere in the repo does not make this project's revision a lie."""
    import subprocess

    from themis.acquire import git

    repo = tmp_path / "repo"
    (repo / "proj").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "proj" / "a.sql").write_text("select 1")
    (repo / "elsewhere.md").write_text("notes")
    run("add", "-A")
    run("commit", "-q", "-m", "first")

    (repo / "elsewhere.md").write_text("edited")
    assert not git.is_clean(repo)
    assert git.is_clean(repo, repo / "proj")
