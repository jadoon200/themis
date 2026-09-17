"""Finding profiles.yml where dbt finds it.

THEMIS passed the project directory to dbt as --profiles-dir unconditionally. Most teams
keep the profile in ~/.dbt, dbt's default, or point DBT_PROFILES_DIR elsewhere — and a
review there failed at its first compile with "Could not find profile". Reproduced with
the demo project's profile moved to a home directory, then fixed and re-run both ways.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from themis.execute.profiles import read_profile, resolve_profiles_dir

_PROFILE = """demo:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: /tmp/demo.duckdb
"""


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    for directory in (home / ".dbt", project, shared):
        directory.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
    return {"home": home, "project": project, "shared": shared}


def test_a_profile_in_home_dbt_is_found(layout: dict[str, Path]) -> None:
    (layout["home"] / ".dbt" / "profiles.yml").write_text(_PROFILE)
    assert resolve_profiles_dir(layout["project"]) == layout["home"] / ".dbt"
    assert read_profile(layout["project"], target="dev")["type"] == "duckdb"


def test_dbt_profiles_dir_wins_over_home(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (layout["home"] / ".dbt" / "profiles.yml").write_text(_PROFILE)
    (layout["shared"] / "profiles.yml").write_text(_PROFILE)
    monkeypatch.setenv("DBT_PROFILES_DIR", str(layout["shared"]))
    assert resolve_profiles_dir(layout["project"]) == layout["shared"]


def test_a_profile_in_the_project_wins_over_home(layout: dict[str, Path]) -> None:
    (layout["home"] / ".dbt" / "profiles.yml").write_text(_PROFILE)
    (layout["project"] / "profiles.yml").write_text(_PROFILE)
    assert resolve_profiles_dir(layout["project"]) == layout["project"]


def test_an_explicit_directory_wins_over_everything(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    (layout["project"] / "profiles.yml").write_text(_PROFILE)
    (layout["shared"] / "profiles.yml").write_text(_PROFILE)
    monkeypatch.setenv("DBT_PROFILES_DIR", str(layout["home"]))
    assert resolve_profiles_dir(layout["project"], layout["shared"]) == layout["shared"]


def test_no_profile_anywhere_leaves_dbt_to_say_so(layout: dict[str, Path]) -> None:
    assert resolve_profiles_dir(layout["project"]) == layout["project"]
