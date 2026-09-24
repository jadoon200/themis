"""Nothing THEMIS starts sends anything out of the network.

The approval page says so, so a test does. Two things did, and both came from dbt rather
than from THEMIS's own code, which is why they went unnoticed:

- every dbt command reports anonymous usage to dbt Labs unless told not to;
- `dbt --version`, which `themis doctor` ran, asks pypi.org for the latest release.

At a bank a proxy would probably have blocked both. "Probably blocked" is not something to
put on an approval form.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest


def test_every_dbt_themis_starts_is_told_not_to_report_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from themis.acquire import dbt_runner

    (tmp_path / "dbt_project.yml").write_text("name: p\nprofile: p\n")
    (tmp_path / "profiles.yml").write_text("p:\n  target: dev\n  outputs: {}\n")
    # Even with the shell opting in: a review is not the place to.
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "True")
    seen: dict[str, Any] = {}

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(dbt_runner.subprocess, "run", fake_run)
    dbt_runner.run_dbt(tmp_path, ["compile"], target="dev", allowed_targets=("dev",))
    assert seen["env"]["DBT_SEND_ANONYMOUS_USAGE_STATS"] == "False"
    assert seen["env"]["DO_NOT_TRACK"] == "1"


def test_doctor_reads_the_dbt_version_without_asking_pypi(monkeypatch: pytest.MonkeyPatch) -> None:
    from themis import onboarding

    def no_subprocess(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("doctor ran a subprocess to learn dbt's version")

    monkeypatch.setattr(onboarding.subprocess, "run", no_subprocess)
    check = onboarding._check_dbt()
    assert check.status == "ok"
    assert check.detail.startswith("dbt-core ")
