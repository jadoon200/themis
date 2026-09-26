"""The scheduler's environment file: what dbt is told about where things live.

At work Dagster reads an `env.config.ini` — the Trino environment and every schema, once per
environment (prod, pre-prod, UAT) — and dbt sees the values. THEMIS runs dbt itself, so it
has to hand dbt the same values or the first compile fails on a variable nobody set. The
project below is compiled by dbt and reads the file both ways a project can: a model names
its source schema with `var()`, and the profile finds its database with `env_var()`.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml

from themis.acquire.env_config import EnvConfigError, read_env_config

INI = """\
; one section per environment, as the scheduler keeps it
[DEFAULT]
trino_env = shared

[uat]
raw_schema = uat_raw
database_path = {database}

[preprod]
raw_schema = preprod_raw
database_path = {database}

[prod]
raw_schema = prod_raw
database_path = {database}
"""


# --- reading the file ----------------------------------------------------------------------


def test_one_section_is_read_with_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "env.config.ini"
    path.write_text(INI.format(database="/data/w.duckdb"))
    config = read_env_config(path, "uat")
    assert config.values == {
        "raw_schema": "uat_raw",
        "database_path": "/data/w.duckdb",
        "trino_env": "shared",
    }
    # Both spellings, because a scheduler usually exports keys upper-cased.
    assert config.environment["RAW_SCHEMA"] == config.environment["raw_schema"] == "uat_raw"


@pytest.mark.parametrize("section", ["prod", "preprod", "PRD", "live"])
def test_a_production_looking_section_is_refused(tmp_path: Path, section: str) -> None:
    """Its values can point dbt at production whatever the target is called."""
    path = tmp_path / "env.config.ini"
    path.write_text(INI.format(database="/data/w.duckdb").replace("[prod]", "[PRD]"))
    with pytest.raises(EnvConfigError, match="production"):
        read_env_config(path, section)


def test_a_sectioned_file_needs_a_section_named(tmp_path: Path) -> None:
    path = tmp_path / "env.config.ini"
    path.write_text(INI.format(database="/data/w.duckdb"))
    with pytest.raises(EnvConfigError, match="uat, preprod, prod"):
        read_env_config(path, None)
    with pytest.raises(EnvConfigError, match="no section \\[dev\\]"):
        read_env_config(path, "dev")


def test_a_file_without_sections_is_read_whole(tmp_path: Path) -> None:
    path = tmp_path / "env.config.ini"
    path.write_text("trino_env='uat'\nfx_schema = \"uat_fx\"\n")
    config = read_env_config(path, None)
    assert config.values == {"trino_env": "uat", "fx_schema": "uat_fx"}


def test_vars_go_only_to_commands_that_take_them(tmp_path: Path) -> None:
    path = tmp_path / "env.config.ini"
    path.write_text("raw_schema=uat_raw\n")
    config = read_env_config(path, None)
    assert config.vars_argument("compile") == ["--vars", '{"raw_schema": "uat_raw"}']
    assert config.vars_argument("deps") == []


# --- a project compiled by dbt ---------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    database = tmp_path / "warehouse.duckdb"
    connection = duckdb.connect(str(database))
    try:
        connection.execute("create schema uat_raw")
        connection.execute("create table uat_raw.accounts as select 1 as account_id")
    finally:
        connection.close()
    (root / "dbt_project.yml").write_text(
        yaml.safe_dump({"name": "envfile", "profile": "envfile", "version": "1.0.0"})
    )
    (root / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "envfile": {
                    "target": "dev",
                    "outputs": {
                        "dev": {"type": "duckdb", "path": "{{ env_var('DATABASE_PATH') }}"}
                    },
                }
            }
        )
    )
    (root / "models").mkdir()
    (root / "models" / "accounts.sql").write_text(
        "select account_id from {{ var('raw_schema') }}.accounts\n"
    )
    (tmp_path / "env.config.ini").write_text(INI.format(database=database))
    return root


def _compile(project: Path) -> tuple[bool, str]:
    from themis.acquire.dbt_runner import compile_project, run_dbt

    result = run_dbt(
        project,
        ["compile"],
        target="dev",
        allowed_targets=("dev",),
        profiles_dir=project,
        target_path=project / "target",
    )
    if not result.ok:
        return False, result.stdout
    compiled = compile_project(
        project,
        target="dev",
        allowed_targets=("dev",),
        profiles_dir=project,
        target_path=project / "target",
    )
    assert compiled.manifest_path is not None
    return True, compiled.manifest_path.read_text()


def test_without_the_file_the_project_does_not_compile(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("THEMIS_DBT_ENV_CONFIG", raising=False)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    ok, output = _compile(project)
    assert not ok and "DATABASE_PATH" in output


def test_with_the_file_dbt_sees_the_section_both_ways(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("THEMIS_DBT_ENV_CONFIG", str(project.parent / "env.config.ini"))
    monkeypatch.setenv("THEMIS_DBT_ENV_SECTION", "uat")
    ok, manifest = _compile(project)
    assert ok, manifest
    # var() rendered from the section, and the profile's env_var() found its database.
    assert "uat_raw.accounts" in manifest
    assert "prod_raw" not in manifest


def test_a_production_section_stops_dbt_before_it_starts(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from themis.acquire.dbt_runner import DbtError

    monkeypatch.setenv("THEMIS_DBT_ENV_CONFIG", str(project.parent / "env.config.ini"))
    monkeypatch.setenv("THEMIS_DBT_ENV_SECTION", "prod")
    with pytest.raises(DbtError, match="production"):
        _compile(project)


def test_themis_logs_in_with_the_same_values(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from themis.execute.profiles import read_profile, render_profile

    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("THEMIS_DBT_ENV_CONFIG", str(project.parent / "env.config.ini"))
    monkeypatch.setenv("THEMIS_DBT_ENV_SECTION", "uat")
    rendered = render_profile(read_profile(project, target="dev"))
    assert rendered["path"] == str(project.parent / "warehouse.duckdb")


def test_doctor_names_what_is_missing_and_then_passes(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from themis.config import Settings
    from themis.onboarding import _check_env_config, _check_project_inputs

    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.delenv("THEMIS_DBT_ENV_CONFIG", raising=False)
    check = _check_project_inputs(project, Settings())
    assert check.status == "fail"
    assert "var raw_schema" in check.detail and "env_var DATABASE_PATH" in check.detail

    monkeypatch.setenv("THEMIS_DBT_ENV_CONFIG", str(project.parent / "env.config.ini"))
    monkeypatch.setenv("THEMIS_DBT_ENV_SECTION", "uat")
    settings = Settings()
    assert _check_project_inputs(project, settings).status == "ok"
    assert _check_env_config(settings).status == "ok"
    monkeypatch.setenv("THEMIS_DBT_ENV_SECTION", "preprod")
    assert _check_env_config(Settings()).status == "fail"


# --- what `themis profile` can say about it, naming nothing ------------------------------------


def test_profile_counts_how_models_name_what_they_read() -> None:
    """A table this project builds, read by name and not through ref(), is a dependency
    no stage can see. Counted, so the answer comes home from the office as a number."""
    from themis.analyze.lineage import build_column_graph
    from themis.analyze.profile import profile
    from themis.models import Backend
    from themis.snapshot import ModelNode, ProjectSnapshot
    from themis.vocabulary import DEFAULT

    def node(name: str, raw: str, compiled: str, deps: tuple[str, ...] = ()) -> ModelNode:
        return ModelNode(
            name=name,
            unique_id=f"model.p.{name}",
            file_path=f"models/{name}.sql",
            relation_name=f'"lake"."uat_mart"."{name}"',
            raw_sql=raw,
            compiled_sql=compiled,
            depends_on_models=deps,
        )

    snapshot = ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "base": node("base", "select 1 as k", "select 1 as k"),
            "by_ref": node(
                "by_ref",
                "select k from {{ ref('base') }}",
                'select k from "lake"."uat_mart"."base"',
                ("model.p.base",),
            ),
            "by_var": node(
                "by_var",
                "select k from {{ var('mart_schema') }}.base",
                "select k from uat_mart.base",
            ),
        },
    )
    counts = profile(snapshot, {}, build_column_graph(snapshot), DEFAULT)["table_naming"]
    assert counts["models_calling_var"] == 1
    assert counts["models_reading_tables_with_no_ref_or_source"] == 1
    assert counts["project_tables_read_by_name_not_ref"] == 1
    assert counts["models_reading_project_tables_by_name"] == 1
