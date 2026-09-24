"""Replaying merged changes: the first measurement THEMIS takes of a project it has never seen.

Protected here: that each replayed change is the diff a pull request review would have
seen (first parent, one entry per landed change), that a change which could not be
reviewed is never counted as one that found nothing, and that what can leave the
building — the summary, the JSON — names no commit subject unless asked to.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from themis.acquire.git import GitError
from themis.backtest import BacktestRow, Change, backtest, changes_to_replay, summarise
from themis.config import Settings
from themis.models import Severity


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main: root -> other-dir change -> project change -> merge of a two-commit branch."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    project = tmp_path / "proj"
    project.mkdir()

    def commit(path: Path, text: str, message: str) -> None:
        path.write_text(text)
        _git(tmp_path, "add", str(path))
        _git(tmp_path, "commit", "-q", "-m", message)

    commit(project / "a.sql", "select 1", "root")
    (tmp_path / "docs").mkdir()
    commit(tmp_path / "docs" / "x.md", "x", "docs only")
    commit(project / "a.sql", "select 2", "direct change")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    commit(project / "b.sql", "select 3", "branch one")
    commit(project / "b.sql", "select 4", "branch two")
    _git(tmp_path, "checkout", "-q", "main")
    _git(tmp_path, "merge", "-q", "--no-ff", "feature", "-m", "Merge pull request #1")
    return tmp_path


def test_each_change_is_replayed_as_it_landed(repo: Path) -> None:
    changes = changes_to_replay(repo / "proj", last=10, ref="main")
    subjects = [c.subject for c in changes]
    # The merge, then the direct change: the branch's own commits are not separate
    # reviews, a commit outside the project is not one, and the root has no parent.
    assert subjects == ["Merge pull request #1", "direct change"]
    merge = changes[0]
    assert merge.parent == _git(repo, "rev-parse", "HEAD^1")


def test_a_revision_that_reads_as_an_option_is_refused(repo: Path) -> None:
    with pytest.raises((GitError, ValueError)):
        changes_to_replay(repo / "proj", last=5, ref="--output=/tmp/x")


def test_a_change_that_could_not_be_reviewed_is_not_counted_as_clean(tmp_path: Path) -> None:
    good, bad = Change("a" * 40, "b" * 40, "fine"), Change("c" * 40, "d" * 40, "broken")

    def reviewer(project: Path, *, head: str, **_: Any) -> Any:
        if head == bad.commit:
            raise RuntimeError("did not compile")
        finding = SimpleNamespace(severity=Severity.HIGH, rule_id="F1001")
        return SimpleNamespace(
            findings=[finding], models_reviewed=("m",), skipped=[], incomplete_reasons=()
        )

    rows = backtest(tmp_path, [good, bad], settings=Settings(), reviewer=reviewer)
    assert rows[0].severities["high"] == 1 and rows[0].rules == ("F1001",)
    assert rows[1].error is not None and "did not compile" in rows[1].error
    summary = summarise(rows)
    assert (summary["reviewed"], summary["could_not_review"]) == (1, 1)
    assert summary["with_findings"] == 1


def test_what_leaves_the_building_carries_no_subjects_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import themis.backtest as module
    from themis.cli import app

    change = Change("a" * 40, "b" * 40, "Adjust margin for ACME Holdings")
    monkeypatch.setattr(module, "changes_to_replay", lambda *a, **k: [change])
    monkeypatch.setattr(
        module,
        "backtest",
        lambda *a, **k: [
            BacktestRow(change=change, models=1, severities=dict.fromkeys(module.SEVERITIES, 0))
        ],
    )
    out = tmp_path / "bt.json"
    result = CliRunner().invoke(app, ["backtest", "--project", str(tmp_path), "--json", str(out)])
    assert result.exit_code == 0, result.output
    assert "ACME" not in result.output and "ACME" not in out.read_text()
    assert json.loads(out.read_text())["summary"]["reviewed"] == 1

    shown = CliRunner().invoke(
        app, ["backtest", "--project", str(tmp_path), "--json", str(out), "--subjects"]
    )
    assert "ACME" in shown.output and "ACME" in out.read_text()
