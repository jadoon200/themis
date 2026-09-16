"""Conventions a project's reviewers write down, shown to specialists as context.

The properties worth protecting are the refusals. A convention with no stated
implication is refused, because the reader is left to guess what follows. A malformed
file is reported, never read as "no conventions". And no convention, however it is
worded, can be quoted as evidence: a stale one must not refute a live finding.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from themis import conventions
from themis.cli import app
from themis.llm.context_pack import build_pack
from themis.llm.provider import Usage
from themis.models import Backend, Confidence, Evidence, Finding, Severity
from themis.review import selfcheck
from themis.review.specialists import GRAIN, Adjudication
from themis.snapshot import ModelNode, ProjectSnapshot

_GOOD = """
conventions:
  - id: fx-rates-one-per-period
    rules: [f1001]
    models: ["int_*"]
    condition: A join onto stg_fx_rates on currency and rate period.
    guidance: stg_fx_rates holds one row per currency per month, by contract with treasury.
    implication: Such a join does not multiply rows when both keys are in the condition.
    owner: data-platform
  - id: amounts-in-minor-units
    condition: A column named amount_minor.
    guidance: Raw ledger amounts are integers in the currency's minor unit.
    implication: Dividing by 100 is correct only for two-decimal currencies.
"""


def _write(tmp_path: Path, text: str) -> Path:
    (tmp_path / conventions.FILENAME).write_text(text)
    return tmp_path


def _finding(rule_id: str = "F1001", model: str = "int_gl_entries_converted") -> Finding:
    return Finding(
        rule_id=rule_id,
        family=rule_id[:2],
        title="join may fan out",
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name=model, note="the join key is not proven unique"),
        consequence="revenue could be multiplied",
    )


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "int_gl_entries_converted": ModelNode(
                name="int_gl_entries_converted",
                unique_id="model.d.int_gl_entries_converted",
                file_path="models/int_gl_entries_converted.sql",
                compiled_sql="select 1",
            )
        },
    )


# --- loading ----------------------------------------------------------------------


def test_a_project_with_no_file_has_no_conventions(tmp_path: Path) -> None:
    loaded = conventions.load(tmp_path)
    assert loaded.conventions == () and loaded.rejected == ()


def test_well_formed_conventions_load_with_their_scope(tmp_path: Path) -> None:
    loaded = conventions.load(_write(tmp_path, _GOOD))
    assert [c.id for c in loaded.conventions] == [
        "fx-rates-one-per-period",
        "amounts-in-minor-units",
    ]
    assert loaded.conventions[0].rules == ("F1001",)
    assert loaded.rejected == ()


def test_a_convention_without_an_implication_is_refused(tmp_path: Path) -> None:
    """The reader of a memory with no stated consequence has to guess what follows."""
    loaded = conventions.load(
        _write(
            tmp_path,
            "conventions:\n  - id: vague\n    condition: FX joins.\n    guidance: They are fine.\n",
        )
    )
    assert loaded.conventions == ()
    assert loaded.rejected == (("vague", "missing implication"),)


def test_broken_yaml_is_reported_not_read_as_empty(tmp_path: Path) -> None:
    loaded = conventions.load(_write(tmp_path, "conventions: [unclosed"))
    assert loaded.conventions == ()
    assert loaded.rejected and "not valid YAML" in loaded.rejected[0][1]


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    doubled = _GOOD + _GOOD.split("conventions:\n", 1)[1]
    loaded = conventions.load(_write(tmp_path, doubled))
    assert len(loaded.conventions) == 2
    assert all(reason == "duplicate `id`" for _, reason in loaded.rejected)


# --- scope ------------------------------------------------------------------------


def test_scope_matches_rule_and_model_glob(tmp_path: Path) -> None:
    loaded = conventions.load(_write(tmp_path, _GOOD)).conventions
    fx = loaded[0]
    assert fx.applies_to(_finding("F1001", "int_gl_entries_converted"))
    assert not fx.applies_to(_finding("F2001", "int_gl_entries_converted"))
    assert not fx.applies_to(_finding("F1001", "fct_revenue"))


def test_an_unscoped_convention_applies_everywhere(tmp_path: Path) -> None:
    unscoped = conventions.load(_write(tmp_path, _GOOD)).conventions[1]
    assert unscoped.applies_to(_finding("F3001", "anything"))


# --- in the pack, and never as evidence -----------------------------------------------


def test_applicable_conventions_reach_the_specialist(tmp_path: Path) -> None:
    loaded = conventions.load(_write(tmp_path, _GOOD)).conventions
    pack = build_pack(_finding(), snapshot=_snapshot(), grains={}, conventions=loaded)
    assert "fx-rates-one-per-period" in pack.text
    assert "one row per currency per month" in pack.text


def test_a_convention_can_never_be_quoted_as_evidence(tmp_path: Path) -> None:
    """A stale convention must not refute a live finding on its own say-so."""
    loaded = conventions.load(_write(tmp_path, _GOOD)).conventions
    pack = build_pack(_finding(), snapshot=_snapshot(), grains={}, conventions=loaded)
    assert "one row per currency per month" not in pack.evidence_text

    leaning_on_it = Adjudication(
        verdict="refute",
        severity="low",
        rationale="the team says the rates are unique",
        evidence_quote="stg_fx_rates holds one row per currency per month",
        specialist=GRAIN.name,
        usage=Usage(),
    )
    assert not selfcheck.check(leaning_on_it, pack).ok


def test_a_convention_that_is_a_key_claim_is_flagged_as_testable(tmp_path: Path) -> None:
    loaded = conventions.load(_write(tmp_path, _GOOD)).conventions
    assert conventions.checkable(loaded[0])
    assert not conventions.checkable(loaded[1])


def test_the_command_exits_non_zero_when_anything_was_refused(tmp_path: Path) -> None:
    _write(tmp_path, "conventions:\n  - id: vague\n    condition: x\n    guidance: y\n")
    result = CliRunner().invoke(app, ["conventions", "--project", str(tmp_path)])
    assert result.exit_code == 1
    assert "REFUSED vague" in result.output


def test_the_command_points_key_claims_at_tests(tmp_path: Path) -> None:
    _write(tmp_path, _GOOD)
    result = CliRunner().invoke(app, ["conventions", "--project", str(tmp_path)])
    assert result.exit_code == 0
    assert "declare it as a uniqueness test" in result.output


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t.invalid",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_review_of_a_commit_reads_that_commits_conventions(tmp_path: Path) -> None:
    """Conventions are versioned with the code, so the revision under review decides
    them — the same mistake `--head` once made with the SQL would otherwise recur."""
    repo = tmp_path / "repo"
    project = repo / "proj"
    project.mkdir(parents=True)
    _git(repo, "init", "-q")
    (project / conventions.FILENAME).write_text(_GOOD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "conventions as reviewed")
    reviewed = _git(repo, "rev-parse", "HEAD")

    one = _GOOD.split("  - id: amounts-in-minor-units")[0]
    (project / conventions.FILENAME).write_text(one)
    _git(repo, "commit", "-qam", "drop one")
    # And something different again on disk, uncommitted.
    (project / conventions.FILENAME).write_text("conventions: []\n")

    at_commit = conventions.load_at(project, reviewed)
    assert [c.id for c in at_commit.conventions] == [
        "fx-rates-one-per-period",
        "amounts-in-minor-units",
    ]
    assert conventions.load_at(project, "HEAD").conventions == ()


def test_a_revision_without_the_file_has_no_conventions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    project = repo / "proj"
    project.mkdir(parents=True)
    _git(repo, "init", "-q")
    (project / "model.sql").write_text("select 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "no conventions yet")
    first = _git(repo, "rev-parse", "HEAD")
    (project / conventions.FILENAME).write_text(_GOOD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "conventions added")

    assert conventions.load_at(project, first) == conventions.Loaded()
