"""Setting THEMIS up on a real project: `themis doctor` and `themis init`.

The refusals are the point. init never proposes a production-looking target for the
allowlist and never overwrites a file someone already has; doctor refuses a production
target outright, finds the profile where dbt would, and says how to fix what is missing
rather than letting it surface later as a failed review about something else.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from themis import onboarding
from themis.config import Settings

_PROFILE = """demo:
  target: dev
  outputs:
    dev: {type: duckdb, path: /tmp/demo.duckdb}
    ci: {type: duckdb, path: /tmp/ci.duckdb}
    prod: {type: trino, host: warehouse, port: 443}
    prod_readonly: {type: trino, host: warehouse, port: 443}
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".dbt").mkdir(parents=True)
    (home / ".dbt" / "profiles.yml").write_text(_PROFILE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    (root / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")
    return root


def _by_name(checks: list[onboarding.Check]) -> dict[str, onboarding.Check]:
    return {check.name: check for check in checks}


def test_production_looking_targets_are_never_proposed(project: Path) -> None:
    assert onboarding.safe_targets(project) == ["dev", "ci"]
    text = onboarding.env_file(project)
    assert 'THEMIS_EXECUTE_ALLOWED_TARGETS=["dev", "ci"]' in text
    assert "prod" not in text.split("THEMIS_EXECUTE_ALLOWED_TARGETS=")[1].splitlines()[0]


def test_init_writes_a_salt_and_never_overwrites(project: Path, tmp_path: Path) -> None:
    destination = tmp_path / "workdir"
    destination.mkdir()
    first = onboarding.initialise(project, destination)
    assert all(w.created for w in first)
    env = (destination / ".env").read_text()
    salt = next(line for line in env.splitlines() if line.startswith("THEMIS_REDACT_SALT="))
    assert len(salt.split("=", 1)[1]) == 32

    (destination / ".env").write_text("mine\n")
    second = onboarding.initialise(project, destination)
    assert not any(w.created for w in second)
    assert (destination / ".env").read_text() == "mine\n"


def test_the_conventions_template_loads_as_empty(project: Path, tmp_path: Path) -> None:
    from themis import conventions

    onboarding.initialise(project, tmp_path)
    loaded = conventions.load(project)
    assert loaded.conventions == () and loaded.rejected == ()


def test_doctor_finds_the_profile_in_home_dbt(project: Path) -> None:
    checks = _by_name(onboarding._check_profile(project, "dev"))
    assert checks["profile"].status == "ok"
    assert ".dbt" in checks["profile"].detail


def test_doctor_refuses_a_production_target_outright(project: Path) -> None:
    check = onboarding._check_allowlist(Settings(), "prod_readonly")
    assert check.status == "fail"
    assert "looks like production" in check.detail


def test_doctor_explains_how_to_allow_a_non_production_target(project: Path) -> None:
    check = onboarding._check_allowlist(Settings(), "analytics_dev")
    assert check.status == "fail"
    assert check.fix is not None and "THEMIS_EXECUTE_ALLOWED_TARGETS" in check.fix


def test_a_missing_adapter_names_the_install(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onboarding.importlib.util, "find_spec", lambda name: None)
    checks = _by_name(onboarding._check_profile(project, "prod_readonly"))
    assert checks["adapter"].status == "fail"
    assert checks["adapter"].fix is not None and "dbt-trino" in checks["adapter"].fix


def test_an_unknown_target_lists_the_real_ones(project: Path) -> None:
    checks = _by_name(onboarding._check_profile(project, "staging"))
    assert checks["target"].status == "fail"
    assert "dev, ci, prod, prod_readonly" in (checks["target"].fix or "")


def test_no_local_model_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(onboarding.httpx, "get", down)
    (check,) = onboarding._check_model(Settings())
    assert check.status == "warn"
    assert "--no-llm" in check.detail


def test_a_model_that_is_not_pulled_says_how_to_pull_it(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "http://127.0.0.1:11434/api/tags")
    monkeypatch.setattr(
        onboarding.httpx,
        "get",
        lambda *a, **k: httpx.Response(200, json={"models": []}, request=request),
    )
    (check,) = onboarding._check_model(Settings())
    assert check.status == "warn" and check.fix == "ollama pull qwen3:8b"


def test_a_project_directory_without_dbt_is_a_failure(tmp_path: Path) -> None:
    assert onboarding._check_project(tmp_path).status == "fail"


def test_the_optional_mcp_sdk_is_reported_but_never_fails_a_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Serving MCP is opt-in, so its absence is a skip with the install line, not a failure.

    A review needs nothing from the SDK; only `themis mcp` does. Reporting it as a failure
    would push people to install a dependency tree they may have no reason to carry.
    """
    assert onboarding._check_mcp().status in {"ok", "skip"}

    monkeypatch.setattr(onboarding.importlib.util, "find_spec", lambda name: None)
    absent = onboarding._check_mcp()
    assert absent.status == "skip" and absent.fix == "uv pip install 'themis[mcp]'"
