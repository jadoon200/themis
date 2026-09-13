"""Reviewing the revision that was asked for, and every file that can change the numbers.

Two defects shape this file. A review with ``--head`` naming another commit compiled
the working tree anyway and labelled it with the requested SHA, so a real fan-out
branch reviewed from a checkout of main came back clean. And a change to a seed, to
``dbt_project.yml``, or to a model outside ``models/`` reached no model at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from themis.acquire import git
from themis.acquire.snapshot_builder import AcquireResult
from themis.models import Backend
from themis.pipeline import _reconfigured_models
from themis.report import markdown
from themis.rules.base import RuleContext
from themis.rules.families.f1_grain import JoinFanOutRule
from themis.rules.registry import run_rules
from themis.snapshot import MacroNode, ModelNode, ProjectSnapshot


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _run(tmp_path, "init", "-q", "-b", "main")
    _run(tmp_path, "config", "user.email", "t@example.invalid")
    _run(tmp_path, "config", "user.name", "t")
    _run(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text("select 1 as x\n")
    _run(tmp_path, "add", ".")
    _run(tmp_path, "commit", "-q", "-m", "base")
    _run(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "models" / "a.sql").write_text("select 2 as x\n")
    _run(tmp_path, "commit", "-q", "-am", "feature")
    _run(tmp_path, "checkout", "-q", "main")
    return tmp_path


# --- which revision the files on disk are --------------------------------------------


def test_head_is_always_the_working_tree(repo: Path) -> None:
    assert git.is_working_tree(repo, "HEAD")


def test_another_branch_is_not_the_working_tree(repo: Path) -> None:
    """The defect: this was compiled from disk, i.e. from main, under feature's SHA."""
    assert not git.is_working_tree(repo, "feature")


def test_the_checked_out_branch_by_name_is_the_working_tree_only_when_clean(repo: Path) -> None:
    assert git.is_working_tree(repo, "main")
    (repo / "models" / "a.sql").write_text("select 3 as x\n")
    assert not git.is_working_tree(repo, "main")


@pytest.mark.parametrize("revision", ["--output=/tmp/x", "-p", "", "main\nHEAD"])
def test_a_revision_git_would_read_as_an_option_is_refused(repo: Path, revision: str) -> None:
    with pytest.raises(git.GitError):
        git.resolve_revision(repo, revision)


def test_an_unknown_revision_names_itself(repo: Path) -> None:
    with pytest.raises(git.GitError, match="nope"):
        git.resolve_revision(repo, "nope")


def test_working_tree_changes_include_uncommitted_and_untracked_files(repo: Path) -> None:
    """Compiled from disk means reviewed from disk: edits used to be compiled and not listed."""
    base = git.resolve_revision(repo, "main")
    (repo / "models" / "a.sql").write_text("select 4 as x\n")
    (repo / "models" / "new.sql").write_text("select 5 as y\n")

    committed = {c.path for c in git.changed_files(repo, base, base)}
    on_disk = {c.path for c in git.changed_files(repo, base, base, working_tree=True)}
    assert committed == set()
    assert on_disk == {"models/a.sql", "models/new.sql"}


def test_a_commit_diff_ignores_the_working_tree(repo: Path) -> None:
    base = git.resolve_revision(repo, "main")
    head = git.resolve_revision(repo, "feature")
    (repo / "models" / "unrelated.sql").write_text("select 0\n")
    assert {c.path for c in git.changed_files(repo, base, head)} == {"models/a.sql"}


# --- routing a changed file to the models it affects ---------------------------------


def _node(
    name: str, path: str, *, sql: str | None = "select 1", resource: str = "model"
) -> ModelNode:
    return ModelNode(
        name=name,
        unique_id=f"{resource}.p.{name}",
        file_path=path,
        compiled_sql=sql if resource == "model" else None,
        resource_type=resource,
    )


def _snapshot(*nodes: ModelNode, macros: tuple[MacroNode, ...] = ()) -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={n.name: n for n in nodes},
        macros={m.name: m for m in macros},
    )


def _acquired(changed: tuple[str, ...], snapshot: ProjectSnapshot) -> AcquireResult:
    return AcquireResult(
        before=snapshot,
        after=snapshot,
        changed=tuple(git.ChangedFile(path=p, status="M") for p in changed),
    )


def test_a_model_outside_models_is_found_through_the_manifest() -> None:
    snapshot = _snapshot(_node("fct", "transform/marts/fct.sql"))
    assert _acquired(("proj/transform/marts/fct.sql",), snapshot).changed_models == ("fct",)


def test_a_seed_change_is_a_seed_not_a_model() -> None:
    snapshot = _snapshot(_node("raw_fx_rates", "seeds/raw_fx_rates.csv", resource="seed"))
    acquired = _acquired(("demo/seeds/raw_fx_rates.csv",), snapshot)
    assert acquired.changed_seeds == ("raw_fx_rates",)
    assert acquired.changed_models == ()


def test_a_macro_file_outside_macros_is_found_through_the_manifest() -> None:
    snapshot = _snapshot(
        macros=(
            MacroNode(
                name="money", unique_id="macro.p.money", file_path="lib/money.sql", raw_sql=""
            ),
        )
    )
    assert _acquired(("proj/lib/money.sql",), snapshot).changed_macro_files == (
        "proj/lib/money.sql",
    )


def test_dbt_project_yml_reaches_the_models_it_reconfigured() -> None:
    before = _snapshot(_node("a", "models/a.sql"), _node("b", "models/b.sql"))
    after = _snapshot(
        _node("a", "models/a.sql").model_copy(update={"materialization": "table"}),
        _node("b", "models/b.sql"),
    )
    assert _reconfigured_models(before, after) == ("a",)


# --- a revision with no compiled SQL for a model ----------------------------------------


def test_a_model_whose_base_has_no_compiled_sql_is_skipped_not_read_as_new() -> None:
    """Every join would otherwise look added, and F1001 would fire on each of them."""
    after_sql = (
        'select e.id from "db"."main"."entries" e join "db"."main"."accounts" a on a.id = e.id'
    )
    before = _snapshot(_node("m", "models/m.sql", sql=None))
    after = _snapshot(_node("m", "models/m.sql", sql=after_sql))
    ctx = RuleContext(
        model_name="m",
        before=before.models["m"],
        after=after.models["m"],
        before_snapshot=before,
        after_snapshot=after,
        grains={},
    )
    findings, skipped = run_rules([ctx], rules=(JoinFanOutRule(),))
    assert findings == []
    assert skipped and "base revision" in skipped[0].reason


def test_a_partial_compile_is_counted_not_hidden() -> None:
    snapshot = _snapshot(
        _node("ok", "models/ok.sql"),
        _node("lost", "models/lost.sql", sql=None),
        _node("raw", "seeds/raw.csv", resource="seed"),
    )
    assert snapshot.has_compiled_sql
    assert snapshot.models_without_compiled_sql == ("lost",)


# --- the report --------------------------------------------------------------------------


def test_a_seed_change_is_stated_even_with_nothing_to_rank() -> None:
    output = markdown.render([], seed_affected={"raw_fx_rates": ("stg_fx_rates", "fct_revenue")})
    assert "Seed `raw_fx_rates` changed" in output
    assert "--execute" in output
