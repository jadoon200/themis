"""Real data must not reach an AI assistant reading THEMIS — and fake data may.

At work the warehouse is Trino and holds the bank's rows; the demo's is DuckDB, or THEMIS's
own local Trino, and every row was generated. THEMIS's model may reason over real values;
Claude Code driving THEMIS may not see them. These tests pin both halves: what is decided to
be real, and that a concealed review carries no value the warehouse returned.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from themis.boundary import assistant_reading, classify_warehouse, detect
from themis.config import Settings
from themis.models import (
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Grain,
    GrainSource,
    KeyedDiff,
    Severity,
)
from themis.report.conceal import WITHHELD, conceal_delta, conceal_finding, scrub

# --- is it real ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "real"),
    [
        ({"type": "duckdb", "path": "x.duckdb"}, False),
        ({"type": "trino", "host": "127.0.0.1", "port": 8085}, False),  # THEMIS's own container
        ({"type": "trino", "host": "trino.internal", "port": 443}, True),
        ({"type": "trino", "host": "127.0.0.1", "port": 8080}, True),  # a tunnel is not ours
        ({"type": "trino", "host": "{{ env_var('NO_SUCH_HOST_VAR') }}", "port": 443}, True),
        ({"type": "spark", "host": "localhost"}, True),  # unknown means real
    ],
)
def test_what_counts_as_real(profile: dict, real: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THEMIS_TREAT_ALL_DATA_AS_REAL", raising=False)
    assert classify_warehouse(profile, Settings())[0] is real


def test_everything_can_be_declared_real(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THEMIS_TREAT_ALL_DATA_AS_REAL", "true")
    assert classify_warehouse({"type": "duckdb"}, Settings())[0]


def test_who_is_reading() -> None:
    assert assistant_reading({"CLAUDECODE": "1"}) == "Claude Code"
    assert assistant_reading({"THEMIS_READER": "assistant"}) is not None
    assert assistant_reading({}) is None
    # There is no way to say "a person" that an assistant could say about itself.
    assert assistant_reading({"CLAUDECODE": "1", "THEMIS_READER": "person"}) == "Claude Code"


def _project(tmp_path: Path, output: dict) -> Path:
    (tmp_path / "dbt_project.yml").write_text(yaml.safe_dump({"name": "p", "profile": "p"}))
    (tmp_path / "profiles.yml").write_text(
        yaml.safe_dump({"p": {"target": "dev", "outputs": {"dev": output}}})
    )
    return tmp_path


def test_values_are_withheld_only_when_both_hold(tmp_path: Path) -> None:
    real = _project(tmp_path, {"type": "trino", "host": "trino.internal", "port": 443})
    settings = Settings()
    assert detect(real, target="dev", settings=settings, environ={"CLAUDECODE": "1"}).conceal
    assert not detect(real, target="dev", settings=settings, environ={}).conceal


def test_an_unreadable_profile_is_real(tmp_path: Path) -> None:
    boundary = detect(tmp_path, target="dev", settings=Settings(), environ={"CLAUDECODE": "1"})
    assert boundary.real_data and boundary.conceal


# --- nothing the warehouse returned survives ---------------------------------------------------

_VALUES = ("334586894", "445712033", "142", "189", "0.02", "ACME-SG01", "2026-03-01")


def _delta() -> ExecutionDelta:
    return ExecutionDelta(
        model_name="fct_revenue",
        rows_before=142,
        rows_after=189,
        sum_deltas={"amount_usd": (334586894.0, 445712033.0)},
        null_rate_deltas={"contract_id": (0.02, 0.05)},
        keyed=KeyedDiff(
            key=("entry_id",),
            rows_added=47,
            rows_changed=3,
            columns_changed={"amount_usd": 3},
            sample_keys=("ACME-SG01",),
            period_column="period_month",
            latest_period="2026-06-01",
            prior_period_rows=2,
            earliest_changed_period="2026-03-01",
        ),
    )


def test_a_concealed_measurement_says_what_moved_and_not_by_how_much() -> None:
    delta = _delta()
    concealed = conceal_delta(delta)
    dumped = json.dumps(concealed.model_dump())
    for value in _VALUES:
        assert value not in dumped, value
    assert concealed.is_material  # still a movement a reviewer must act on
    assert "row count rose" in concealed.withheld
    assert "sum(amount_usd) rose" in concealed.withheld
    assert any("amount_usd" in note and "values changed" in note for note in concealed.withheld)
    assert any("closed period_month period" in note for note in concealed.withheld)


def test_measured_text_loses_its_numbers_and_keeps_its_names() -> None:
    finding = Finding(
        rule_id="X0001",
        family="X",
        title="fct_revenue moved with no rule explaining why",
        severity=Severity.CRITICAL,
        confidence=Confidence.MEASURED,
        evidence=Evidence(model_name="fct_revenue", note="rows 142 -> 189 on 2026-03-01"),
        consequence="sum(amount_usd) 334.6M -> 445.7M (+33.2%); key ACME-SG01 among them",
        execution_delta=_delta(),
        llm_rationale="Revenue rose by 111.1M because the FX join lost its date.",
    )
    concealed = conceal_finding(finding, known=("ACME-SG01",))
    text = " ".join(
        [
            concealed.consequence,
            concealed.evidence.note or "",
            concealed.llm_rationale or "",
        ]
    )
    assert not re.search(r"\d", text), text
    assert "ACME" not in text
    assert "sum(amount_usd)" in concealed.consequence and "fct_revenue" in concealed.title


def test_scrub_leaves_identifiers_readable() -> None:
    text = scrub("F1004 on amount_2: 1,234.50 rows, 12% of 'Treasury' on 2026-01-01")
    assert text is not None
    assert "F1004" in text and "amount_2" in text
    assert "1,234.50" not in text and "Treasury" not in text and "2026" not in text
    assert WITHHELD in text


def test_the_reports_carry_no_value() -> None:
    from themis.report import json_out, markdown

    finding = Finding(
        rule_id="F1001",
        family="F1",
        title="New join may fan out",
        severity=Severity.HIGH,
        confidence=Confidence.MEASURED,
        evidence=Evidence(model_name="fct_revenue"),
        consequence="Rows are multiplied.",
        execution_delta=_delta(),
    )
    concealed = conceal_finding(finding)
    rendered = markdown.render([concealed], skipped=[], models_reviewed=1, executed=True)
    document = json_out.render(
        [concealed],
        skipped=[],
        grains={
            "fct_revenue": Grain(
                model_name="fct_revenue", columns=("entry_id",), source=GrainSource.MEASURED
            )
        },
        deltas={"fct_revenue": concealed.execution_delta},  # type: ignore[dict-item]
        models_reviewed=("fct_revenue",),
        executed=True,
    )
    for value in _VALUES:
        assert value not in rendered, value
        assert value not in document, value
    assert "row count rose" in rendered


# --- what an assistant may not do ------------------------------------------------------------


def test_an_assistant_does_not_write_the_training_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from themis.cli import app

    monkeypatch.setenv("CLAUDECODE", "1")
    result = CliRunner().invoke(app, ["dataset", "--out", "/tmp/never-written.jsonl"])
    assert result.exit_code == 2
    assert "real values" in result.output
    assert not Path("/tmp/never-written.jsonl").exists()


def test_log_lines_are_scrubbed_while_concealing(capsys: pytest.CaptureFixture[str]) -> None:
    from themis.logging import conceal_values, configure_logging, get_logger

    configure_logging()
    try:
        conceal_values(True)
        get_logger("t").warning("dbt.failed", tail="Cannot cast 'ACME Corp' to integer: 334.6")
    finally:
        conceal_values(False)
    err = capsys.readouterr().err
    assert "ACME" not in err and "334.6" not in err and "dbt.failed" in err
