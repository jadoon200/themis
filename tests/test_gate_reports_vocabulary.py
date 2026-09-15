"""The merge gate, shareable reports, stable identities and configurable names.

Four gaps, each of the same kind as the ones before them: a gate that passed a review
which had skipped most of its checks; a fingerprint that gave the same issue a new identity
whenever the data moved; reports that could not leave the building; and the names checks
match on, hard-coded so that a project's own vocabulary made rules silently never fire.
"""

from __future__ import annotations

import json

import httpx
import pytest

from themis.config import Settings
from themis.db.models import fingerprint_finding
from themis.execute.runner import ExecutionResult
from themis.models import (
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Grain,
    GrainSource,
    Severity,
)
from themis.pipeline import ReviewResult
from themis.report import json_out, sarif
from themis.rules.base import RuleContext, SkippedRule
from themis.snapshot import ModelNode, ProjectSnapshot
from themis.vocabulary import Vocabulary, from_settings


def _finding(severity: Severity = Severity.HIGH, **evidence: object) -> Finding:
    return Finding(
        rule_id="F1001",
        family="F1",
        title="New join to secret_dim may fan out",
        severity=severity,
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name="secret_model", **evidence),  # type: ignore[arg-type]
        consequence="rows in secret_dim multiply revenue_secret",
        suggestion="add secret_key to the ON clause",
        blast_radius=("secret_mart",),
    )


# --- the merge gate -------------------------------------------------------------------


def _exit(result: ReviewResult, fail_on: str | None = "high") -> int:
    from themis.cli import _review_exit_code

    return _review_exit_code(result, fail_on)


def test_a_clean_complete_review_passes() -> None:
    assert _exit(ReviewResult()) == 0


def test_a_review_with_skipped_checks_cannot_pass_a_blocking_gate() -> None:
    """The product's version of the corpus job: most rules skipped, exit 0."""
    result = ReviewResult(skipped=[SkippedRule("F1001", "m", "no compiled SQL")])
    assert _exit(result) == 3


def test_degraded_grounding_cannot_pass_a_blocking_gate() -> None:
    assert _exit(ReviewResult(degraded_reason="the head manifest has no compiled SQL")) == 3


def test_execution_asked_for_and_not_run_cannot_pass_a_blocking_gate() -> None:
    result = ReviewResult(
        execution_requested=True,
        execution=ExecutionResult(skipped_reason="no supported warehouse client"),
    )
    assert _exit(result) == 3
    assert "no supported warehouse client" in result.incomplete_reasons[0]


def test_a_blocking_finding_still_reports_as_a_finding() -> None:
    result = ReviewResult(findings=[_finding()], degraded_reason="partial compile")
    assert _exit(result) == 1


def test_advisory_mode_never_blocks() -> None:
    assert _exit(ReviewResult(degraded_reason="x"), fail_on=None) == 0


def test_sarif_marks_an_incomplete_run_unsuccessful() -> None:
    result = ReviewResult(degraded_reason="partial compile")
    log = json.loads(sarif.render([], incomplete=result.incomplete))
    invocation = log["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is False
    assert invocation["toolExecutionNotifications"][0]["descriptor"]["id"] == "grounding_degraded"
    assert json.loads(sarif.render([]))["runs"][0]["invocations"][0]["executionSuccessful"]


# --- redaction ------------------------------------------------------------------------

_SECRETS = (
    "secret",
    "select ",
    "334586894",
    "2009759297",
    "models/",
    "revenue_secret",
)


def _everything() -> dict[str, object]:
    finding = _finding(
        file_path="models/marts/secret_model.sql",
        sql_after="select secret_col from secret_table",
        note="sum(revenue_secret) 334586894 -> 2009759297",
    ).model_copy(
        update={
            "llm_rationale": "secret_dim is one row per secret_key",
            "suggested_fix": "select * from secret_table",
            "execution_delta": ExecutionDelta(
                model_name="secret_model",
                rows_before=33,
                rows_after=34,
                sum_deltas={"revenue_secret": (334586894.0, 2009759297.0)},
            ),
        }
    )
    return {
        "findings": [finding],
        "skipped": [SkippedRule("F3001", "secret_model", "rule raised KeyError: 'secret_col'")],
        "grains": {
            "secret_model": Grain(
                model_name="secret_model",
                columns=("secret_key",),
                source=GrainSource.MEASURED,
                rows_per_key=3.0,
                note="measured: 334586894 rows",
            )
        },
        "deltas": {"secret_model": finding.execution_delta},
        "models_reviewed": ("secret_model",),
        "degraded_reason": "secret_model has no compiled SQL",
        "incomplete": (("grounding_degraded", "grounding degraded: secret_model"),),
        "seed_affected": {"secret_seed": ("secret_model",)},
    }


def test_a_redacted_json_report_carries_no_project_detail() -> None:
    text = json_out.render(**_everything(), redact="salt")  # type: ignore[arg-type]
    for secret in _SECRETS:
        assert secret not in text.lower(), secret
    doc = json.loads(text)
    assert doc["findings"][0]["rule_id"] == "F1001"
    assert doc["findings"][0]["measured"] is True
    assert doc["execution_deltas"][0]["rows"] == "up"
    assert doc["incomplete"] == ["grounding_degraded"]


def test_a_redacted_sarif_log_carries_no_project_detail() -> None:
    everything = _everything()
    text = sarif.render(
        everything["findings"],  # type: ignore[arg-type]
        incomplete=everything["incomplete"],  # type: ignore[arg-type]
        redact="salt",
    )
    for secret in _SECRETS:
        assert secret not in text.lower(), secret


def test_redacted_names_are_stable_and_depend_on_the_salt() -> None:
    from themis.report.redact import token

    assert token("fct_revenue", "a") == token("fct_revenue", "a")
    assert token("fct_revenue", "a") != token("fct_revenue", "b")


# --- fingerprints ---------------------------------------------------------------------


def test_a_measured_finding_keeps_its_identity_when_the_numbers_move() -> None:
    """X0001's note is what moved and by how much. Hashing it made every run new."""
    from themis.pipeline import unexplained_change_findings

    snapshot = ProjectSnapshot(
        revision="r",
        backend="manifest",  # type: ignore[arg-type]
        models={
            "m": ModelNode(
                name="m", unique_id="model.p.m", file_path="models/m.sql", compiled_sql="select 1"
            )
        },
    )
    changed = ProjectSnapshot(
        revision="r",
        backend="manifest",  # type: ignore[arg-type]
        models={
            "m": ModelNode(
                name="m", unique_id="model.p.m", file_path="models/m.sql", compiled_sql="select 2"
            )
        },
    )

    def fingerprint(rows_after: int) -> str:
        result = ExecutionResult(
            deltas={"m": ExecutionDelta(model_name="m", rows_before=10, rows_after=rows_after)}
        )
        (finding,) = unexplained_change_findings(result, [], snapshot, changed)
        return fingerprint_finding(
            rule_id=finding.rule_id,
            model_name="m",
            project="p",
            evidence_note=finding.evidence.identity
            if finding.evidence.identity is not None
            else finding.evidence.note,
        )

    assert fingerprint(12) == fingerprint(40)


# --- vocabulary -----------------------------------------------------------------------


def test_the_vocabulary_is_read_from_settings() -> None:
    vocab = from_settings(
        Settings(money_column_hints=("ntnl", "mtm"), governed_tags=("regulator",))
    )
    assert vocab.is_monetary("trade_ntnl_lcy")
    assert not vocab.is_monetary("amount_usd")
    assert vocab.is_governed(["Regulator"])


def test_a_projects_own_money_word_makes_the_money_rule_fire() -> None:
    """With the default vocabulary `ntnl` is not money, and a DOUBLE cast on it is silent."""
    from themis.rules.families.f3_money import MoneyAsFloatRule

    before_sql = "select cast(trade_ntnl as decimal(38, 6)) as trade_ntnl from t"
    after_sql = "select cast(trade_ntnl as double) as trade_ntnl from t"
    snapshot = ProjectSnapshot(revision="r", backend="manifest")  # type: ignore[arg-type]

    def check(vocab: Vocabulary) -> list[Finding]:
        node = lambda sql: ModelNode(  # noqa: E731
            name="m", unique_id="model.p.m", file_path="models/m.sql", compiled_sql=sql
        )
        ctx = RuleContext(
            model_name="m",
            before=node(before_sql),
            after=node(after_sql),
            before_snapshot=snapshot,
            after_snapshot=snapshot,
            grains={},
            vocabulary=vocab,
        )
        return MoneyAsFloatRule().check(ctx)

    assert check(Vocabulary()) == []
    assert check(Vocabulary(money_hints=("ntnl",)))


# --- profile --------------------------------------------------------------------------


def test_a_profile_names_nothing_in_the_project() -> None:
    from themis.analyze.grain import infer_grains
    from themis.analyze.lineage import build_column_graph
    from themis.analyze.profile import profile

    snapshot = ProjectSnapshot(
        revision="r",
        backend="manifest",  # type: ignore[arg-type]
        models={
            "secret_orders": ModelNode(
                name="secret_orders",
                unique_id="model.p.secret_orders",
                file_path="models/marts/secret_orders.sql",
                compiled_sql="select secret_id, sum(amount_secret) as amount_secret "
                "from secret_raw group by secret_id",
                tags=("secret_tag",),
            )
        },
    )
    result = profile(snapshot, infer_grains(snapshot), build_column_graph(snapshot), Vocabulary())
    text = json.dumps(result)
    assert "secret" not in text
    assert result["nodes"]["sql_models"] == 1
    assert result["compiled_sql"]["parse_failures_as_trino"] == 0


# --- retry ----------------------------------------------------------------------------


def _ollama_reply(payload: dict[str, object]) -> httpx.Response:
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
    return httpx.Response(200, json={"response": json.dumps(payload)}, request=request)


def test_a_transient_failure_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    from themis.llm import provider as provider_module

    calls: list[int] = []

    def flaky(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return _ollama_reply({"verdict": "confirm"})

    monkeypatch.setattr(provider_module.httpx, "post", flaky)
    monkeypatch.setattr(provider_module.time, "sleep", lambda s: None)
    reply = provider_module.OllamaProvider(Settings()).complete(
        system="s", prompt="p", schema={}, model="m"
    )
    assert reply.payload == {"verdict": "confirm"}
    assert len(calls) == 2


def test_a_refused_request_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    from themis.llm import provider as provider_module

    calls: list[int] = []

    def refused(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(1)
        request = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
        return httpx.Response(404, json={"error": "model not found"}, request=request)

    monkeypatch.setattr(provider_module.httpx, "post", refused)
    monkeypatch.setattr(provider_module.time, "sleep", lambda s: None)
    with pytest.raises(provider_module.LLMError, match="refused"):
        provider_module.OllamaProvider(Settings()).complete(
            system="s", prompt="p", schema={}, model="m"
        )
    assert len(calls) == 1


def test_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from themis.llm import provider as provider_module

    calls: list[int] = []

    def down(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(provider_module.httpx, "post", down)
    monkeypatch.setattr(provider_module.time, "sleep", lambda s: None)
    with pytest.raises(provider_module.LLMError):
        provider_module.OllamaProvider(Settings(llm_retries=2)).complete(
            system="s", prompt="p", schema={}, model="m"
        )
    assert len(calls) == 3
