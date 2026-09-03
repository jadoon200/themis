"""The model layer.

Every test here uses a fake provider. That is not only for speed: the properties worth
protecting are about what the system does with an answer, and those must hold for any
answer — including a hostile one. A real model would make the tests non-deterministic
and would only ever exercise the answers it happened to give.
"""

from __future__ import annotations

from typing import Any

from themis.config import Settings
from themis.llm.context_pack import ContextPack, build_pack
from themis.llm.provider import LLMError, Response, Usage
from themis.models import (
    Backend,
    Confidence,
    Evidence,
    ExecutionDelta,
    Finding,
    Grain,
    GrainSource,
    Severity,
    Verdict,
)
from themis.review import selfcheck, supervisor
from themis.review.specialists import GRAIN, Adjudication, specialist_for
from themis.snapshot import ModelNode, ProjectSnapshot


class FakeProvider:
    """Returns a scripted payload, and records what it was asked."""

    def __init__(self, payload: dict[str, Any] | None = None, *, fail: bool = False):
        self._payload = payload or {}
        self._fail = fail
        self.prompts: list[str] = []
        self.models: list[str] = []

    def complete(self, *, system: str, prompt: str, schema: dict, model: str) -> Response:
        self.prompts.append(prompt)
        self.models.append(model)
        if self._fail:
            raise LLMError("model unavailable")
        return Response(payload=self._payload, usage=Usage(calls=1, prompt_tokens=100))


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "fct_revenue": ModelNode(
                name="fct_revenue",
                unique_id="model.t.fct_revenue",
                file_path="models/marts/fct_revenue.sql",
                raw_sql="select 1",
                compiled_sql="select 1",
                depends_on_models=("model.t.stg_fx_rates",),
                tags=("regulatory",),
            )
        },
    )


def _finding(
    confidence: Confidence = Confidence.LIKELY,
    severity: Severity = Severity.HIGH,
    delta: ExecutionDelta | None = None,
) -> Finding:
    return Finding(
        rule_id="F1001",
        family="F1",
        title="New join to stg_fx_rates may fan out",
        severity=severity,
        confidence=confidence,
        evidence=Evidence(
            model_name="fct_revenue",
            file_path="models/marts/fct_revenue.sql",
            note="stg_fx_rates looks unique on (currency_code), but that is heuristic",
        ),
        consequence="Amounts would be duplicated.",
        execution_delta=delta,
    )


def _grains() -> dict[str, Grain]:
    return {
        "fct_revenue": Grain(
            model_name="fct_revenue", columns=("entry_id",), source=GrainSource.STRUCTURAL
        )
    }


def _run(payload: dict[str, Any] | None, finding: Finding, **kwargs: Any):
    provider = FakeProvider(payload)
    summary = supervisor.review(
        [finding],
        provider=provider,
        settings=Settings(),
        snapshot=_snapshot(),
        grains=_grains(),
        **kwargs,
    )
    return summary, provider


# --- routing ------------------------------------------------------------------


def test_families_route_to_their_specialist() -> None:
    assert specialist_for("F1") is not None
    assert specialist_for("F3") is not None
    assert specialist_for("F5") is not None
    assert specialist_for("F6") is not None


def test_unknown_family_has_no_specialist() -> None:
    """A family with no reviewer must leave the finding alone rather than guess."""
    assert specialist_for("F99") is None


# --- what the model is asked about --------------------------------------------


def test_measured_findings_never_reach_the_model() -> None:
    """Nothing is left to judge once the numbers have moved, and asking anyway
    invites the model to argue with a measurement."""
    delta = ExecutionDelta(
        model_name="fct_revenue",
        rows_before=15,
        rows_after=45,
        sum_deltas={"amount_usd": (100.0, 300.0)},
    )
    summary, provider = _run(None, _finding(confidence=Confidence.MEASURED, delta=delta))
    assert provider.prompts == []
    assert summary.settled_without_llm == 1
    assert summary.usage.calls == 0


def test_proven_findings_never_reach_the_model() -> None:
    summary, provider = _run(None, _finding(confidence=Confidence.PROVEN))
    assert provider.prompts == []
    assert summary.settled_without_llm == 1


def test_likely_findings_are_adjudicated() -> None:
    payload = {
        "verdict": "confirm",
        "severity": "high",
        "rationale": "The join key does not cover the grain.",
        "evidence_quote": "stg_fx_rates looks unique on (currency_code), but that is heuristic",
    }
    summary, provider = _run(payload, _finding())
    assert len(provider.prompts) == 1
    assert summary.adjudicated == 1


def test_the_specialist_model_is_used_for_adjudication() -> None:
    payload = {
        "verdict": "uncertain",
        "severity": "high",
        "rationale": "unclear",
        "evidence_quote": "",
    }
    _, provider = _run(payload, _finding())
    assert provider.models == [Settings().llm_specialist_model]


# --- what an answer is allowed to change --------------------------------------


def test_a_refutation_suppresses_the_finding() -> None:
    payload = {
        "verdict": "refute",
        "severity": "low",
        "rationale": "The join is on the full key.",
        "evidence_quote": "stg_fx_rates looks unique on (currency_code), but that is heuristic",
    }
    summary, _ = _run(payload, _finding())
    assert summary.findings[0].verdict is Verdict.SAFE
    assert summary.findings[0].suppressed_reason
    assert summary.suppressed == 1


def test_severity_may_be_lowered() -> None:
    payload = {
        "verdict": "confirm",
        "severity": "low",
        "rationale": "Real but minor here.",
        "evidence_quote": "stg_fx_rates looks unique on (currency_code), but that is heuristic",
    }
    summary, _ = _run(payload, _finding(severity=Severity.HIGH))
    assert summary.findings[0].severity is Severity.LOW


def test_severity_may_not_be_raised() -> None:
    """The rules encode the domain reasoning about how bad each class is. A model that
    has seen one finding in isolation is not better placed to escalate it."""
    payload = {
        "verdict": "confirm",
        "severity": "critical",
        "rationale": "This seems very bad.",
        "evidence_quote": "stg_fx_rates looks unique on (currency_code), but that is heuristic",
    }
    summary, _ = _run(payload, _finding(severity=Severity.MEDIUM))
    assert summary.findings[0].severity is Severity.MEDIUM


def test_uncertainty_escalates_rather_than_defaulting_to_safe() -> None:
    payload = {
        "verdict": "uncertain",
        "severity": "high",
        "rationale": "The context does not settle this.",
        "evidence_quote": "",
    }
    summary, _ = _run(payload, _finding())
    assert summary.findings[0].verdict is Verdict.UNDECIDABLE
    assert summary.findings[0].suppressed_reason is None


def test_an_unavailable_model_leaves_findings_untouched() -> None:
    provider = FakeProvider(fail=True)
    finding = _finding()
    summary = supervisor.review(
        [finding],
        provider=provider,
        settings=Settings(),
        snapshot=_snapshot(),
        grains=_grains(),
    )
    assert summary.findings[0].severity is finding.severity
    assert summary.findings[0].suppressed_reason is None


# --- the self-check -----------------------------------------------------------


def _pack() -> ContextPack:
    return build_pack(_finding(), snapshot=_snapshot(), grains=_grains())


def _adjudication(**overrides: Any) -> Adjudication:
    defaults = dict(
        verdict="confirm",
        severity="high",
        rationale="Because of the grain.",
        evidence_quote="stg_fx_rates looks unique on (currency_code), but that is heuristic",
        specialist=GRAIN.name,
        usage=Usage(),
    )
    defaults.update(overrides)
    return Adjudication(**defaults)  # type: ignore[arg-type]


def test_a_grounded_answer_passes() -> None:
    assert selfcheck.check(_adjudication(), _pack()).ok


def test_a_fabricated_quote_is_rejected() -> None:
    """The direct defence against a model inventing what it was shown."""
    result = selfcheck.check(
        _adjudication(evidence_quote="the manifest declares a unique test on rate_date"),
        _pack(),
    )
    assert not result.ok
    assert "does not appear" in result.reason


def test_a_trivially_short_quote_is_rejected() -> None:
    assert not selfcheck.check(_adjudication(evidence_quote="the"), _pack()).ok


def test_a_reflowed_quote_still_matches() -> None:
    """Whitespace is not evidence of fabrication."""
    quote = "stg_fx_rates  looks unique\non (currency_code), but that is heuristic"
    assert selfcheck.check(_adjudication(evidence_quote=quote), _pack()).ok


def test_an_abstention_needs_no_quote() -> None:
    """Demanding evidence to abstain would push the model to invent a quote in order
    to say it does not know."""
    assert selfcheck.check(_adjudication(verdict="uncertain", evidence_quote=""), _pack()).ok


def test_a_missing_rationale_is_rejected() -> None:
    assert not selfcheck.check(_adjudication(rationale="  "), _pack()).ok


def test_a_rejected_answer_leaves_the_finding_unchanged() -> None:
    payload = {
        "verdict": "refute",
        "severity": "low",
        "rationale": "Trust me.",
        "evidence_quote": "a uniqueness test guarantees this join is safe",
    }
    summary, _ = _run(payload, _finding(severity=Severity.HIGH))
    assert summary.rejected_by_selfcheck == 1
    assert summary.findings[0].severity is Severity.HIGH
    assert summary.findings[0].suppressed_reason is None


# --- context packs ------------------------------------------------------------


def test_a_pack_stays_small() -> None:
    """A model shown a whole file will reason about the whole file."""
    assert _pack().approx_tokens < Settings().llm_max_context_tokens


def test_a_pack_carries_the_grain_and_its_source() -> None:
    text = _pack().text
    assert "entry_id" in text
    assert "structural" in text


def test_a_pack_carries_measured_deltas_when_present() -> None:
    delta = ExecutionDelta(model_name="fct_revenue", rows_before=15, rows_after=45, sum_deltas={})
    pack = build_pack(_finding(delta=delta), snapshot=_snapshot(), grains=_grains())
    assert "15" in pack.text and "45" in pack.text


def test_an_elided_quote_is_accepted() -> None:
    """Models shorten a long quote rather than reproducing it in full. That is not
    fabrication, and rejecting it discards correct answers."""
    quote = "stg_fx_rates looks unique [...] but that is heuristic"
    assert selfcheck.check(_adjudication(evidence_quote=quote), _pack()).ok


def test_elision_does_not_smuggle_in_invention() -> None:
    """Every substantial segment must still appear, so a fabricated half is caught."""
    quote = "stg_fx_rates looks unique [...] a uniqueness test guarantees this is safe"
    assert not selfcheck.check(_adjudication(evidence_quote=quote), _pack()).ok


def test_a_quote_of_only_elision_markers_is_rejected() -> None:
    assert not selfcheck.check(_adjudication(evidence_quote="... [...] ..."), _pack()).ok


def test_the_pack_carries_the_joined_model_sql() -> None:
    """The fact that decides a fan-out.

    Without it a specialist can only repeat that the derived grain is unproven, which
    is what it was already told. With it, it can read the upstream model and work out
    the real key.
    """
    snapshot = _snapshot()
    snapshot.models["stg_fx_rates"] = ModelNode(
        name="stg_fx_rates",
        unique_id="model.t.stg_fx_rates",
        file_path="models/staging/stg_fx_rates.sql",
        raw_sql="select currency_code, rate_date, rate from raw",
        compiled_sql="select currency_code, rate_date, rate from raw",
    )
    finding = _finding().model_copy(
        update={
            "evidence": Evidence(
                model_name="fct_revenue",
                note="stg_fx_rates looks unique on (currency_code)",
                related_model="stg_fx_rates",
            )
        }
    )
    pack = build_pack(finding, snapshot=snapshot, grains=_grains())
    assert "stg_fx_rates`, the model being joined to" in pack.text
    assert "rate_date" in pack.text


def test_a_missing_related_model_does_not_break_the_pack() -> None:
    finding = _finding().model_copy(
        update={"evidence": Evidence(model_name="fct_revenue", related_model="not_a_model")}
    )
    assert build_pack(finding, snapshot=_snapshot(), grains=_grains()).text


def test_the_prompt_distinguishes_risk_from_proven_harm() -> None:
    """The specialists were answering "uncertain" while their own rationale stated the
    problem, because they read "confirm" as claiming the damage was proven."""
    prompt = GRAIN.system_prompt
    assert "risk" in prompt.lower()
    assert "not mean you have proved" in prompt


def test_a_requoted_context_is_accepted() -> None:
    """Models re-punctuate when they quote. Joining separate lines of context into one
    sentence with commas is the commonest form, and rejecting it discards a correct
    answer — five of fifteen in one corpus run, none of them actually fabricated."""
    context = "model: fct_revenue\ntitle: Column removed but still selected downstream"
    quote = "model: fct_revenue, title: Column removed but still selected downstream"
    assert selfcheck.quote_is_grounded(quote, context)


def test_reordered_words_are_still_rejected() -> None:
    """Order must survive: the words being present somewhere is not the same claim."""
    context = "stg_fx_rates is unique on currency_code and rate_date"
    quote = "rate_date and currency_code on unique is stg_fx_rates"
    assert not selfcheck.quote_is_grounded(quote, context)


def test_invented_words_are_still_rejected() -> None:
    context = "model: fct_revenue\ntitle: Column removed but still selected downstream"
    quote = "a uniqueness test on rate_date confirms this join is safe"
    assert not selfcheck.quote_is_grounded(quote, context)


def test_a_quote_that_skips_a_line_is_accepted() -> None:
    """Models quote selectively as well as elliptically.

    Here the context carries a severity line between the title and the reason, and the
    model joined the other two with a comma and left it out. Every phrase it did use is
    genuinely present.
    """
    context = (
        "model: fct_revenue\n"
        "title: Column removed but still selected downstream\n"
        "severity as flagged: high\n"
        "why it was flagged: referenced by fct_regulatory_summary"
    )
    quote = (
        "model: fct_revenue, title: Column removed but still selected downstream, "
        "why it was flagged: referenced by fct_regulatory_summary"
    )
    assert selfcheck.quote_is_grounded(quote, context)


def test_a_fabricated_clause_among_real_ones_is_still_rejected() -> None:
    """The point of the check: one invented phrase spoils the whole quote."""
    context = "model: fct_revenue\ntitle: Column removed but still selected downstream"
    quote = (
        "model: fct_revenue, a uniqueness test confirms this is safe, "
        "title: Column removed but still selected downstream"
    )
    assert not selfcheck.quote_is_grounded(quote, context)


def test_every_rule_family_has_a_specialist() -> None:
    """A family with no reviewer passes through unadjudicated and silently.

    F2 had none: filter and NULL-semantics findings reached the supervisor, found no
    specialist, and were returned untouched — which looks identical to a specialist
    declining to change them.
    """
    from themis.review.specialists import ALL_SPECIALISTS
    from themis.rules.registry import ALL_RULES

    families = {rule.family for rule in ALL_RULES}
    covered = {family for s in ALL_SPECIALISTS for family in s.families}
    assert families <= covered, f"no specialist for: {sorted(families - covered)}"
