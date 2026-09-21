"""The model layer.

Every test here uses a fake provider. That is not only for speed: the properties worth
protecting are about what the system does with an answer, and those must hold for any
answer — including a hostile one. A real model would make the tests non-deterministic
and would only ever exercise the answers it happened to give.
"""

from __future__ import annotations

from typing import Any

from structlog.testing import capture_logs

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


def test_a_heading_quoted_with_its_own_code_block_is_grounded() -> None:
    """A pack is markdown, and specialists quote across its structure.

    "The SQL of `x`, the model being joined to: select ..." joins a section heading to
    the fence beneath it. Every word is genuinely present; only the fence sits between.
    Rejecting that discards a correct answer for punctuation, which is how a third of
    the model layer's output was thrown away once already.
    """
    context = (
        "## The SQL of `dim_accounts`, the model being joined to\n"
        "```sql\n"
        "select account_id, account_code, entity_code\n"
        'from "themis_demo"."main"."stg_accounts"\n'
        "```\n"
    )
    quote = (
        "The SQL of `dim_accounts`, the model being joined to: "
        "select account_id, account_code, entity_code"
    )
    assert selfcheck.quote_is_grounded(quote, context)


def test_a_paraphrased_relation_name_is_still_rejected() -> None:
    """The other rejection from the same run, and this one was correct.

    The compiled SQL names `"themis_demo"."main"."raw_contracts"`. A quote saying
    `from raw_contracts` has rewritten the reference, and a reviewer told the model
    reads an unqualified table would be told something false.
    """
    context = 'select contract_id, customer_id from "themis_demo"."main"."raw_contracts"'
    assert not selfcheck.quote_is_grounded("select contract_id, ... from raw_contracts", context)


def test_a_logged_quote_says_when_it_was_truncated() -> None:
    """A rejection log that silently cuts mid-identifier invents a second fault.

    It reads exactly like the model fabricating a truncated name, and the log exists
    to make rejections diagnosable rather than to add one more thing to diagnose.
    """
    long_quote = "select " + ", ".join(f"column_{i}" for i in range(200))
    logged = selfcheck._for_log(long_quote)
    assert "truncated for the log" in logged
    assert str(len(long_quote)) in logged
    assert selfcheck._for_log("short one") == "short one"


# --- intent: the boolean, and what it is allowed to throw away -----------------


def _intent(payload: dict[str, Any]) -> tuple[list[str], FakeProvider]:
    provider = FakeProvider(payload)
    said = supervisor._intent_pass(
        provider,
        settings=Settings(),
        findings=[_finding()],
        changed_models=("fct_revenue",),
        pr_description="Tidy up the source CTE; no behaviour change.",
        snapshot=_snapshot(),
        usage=Usage(),
    )
    return said, provider


def test_intent_reports_what_the_description_left_out() -> None:
    said, _ = _intent(
        {
            "description_covers_change": False,
            "undisclosed_changes": ["the is_incremental() guard was removed"],
            "rationale": "not mentioned",
        }
    )
    assert said == ["the is_incremental() guard was removed"]


def test_the_boolean_absorbs_the_nothing_here_artefact() -> None:
    """What the boolean exists for: models write "nothing was omitted" as an *item*,
    because the list is the only place to write, and everything downstream then reads
    that sentence as an omission having been found."""
    said, _ = _intent(
        {
            "description_covers_change": True,
            "undisclosed_changes": ["Nothing was omitted from the description."],
            "rationale": "the description matches the SQL",
        }
    )
    assert said == []


def test_a_contradicted_boolean_is_logged_rather_than_discarded_in_silence() -> None:
    """The boolean still wins, and it has to: the artefact it absorbs is itself a
    non-empty list, so keeping the list whenever it had contents would let the false
    alarm straight back in. But a model that sets the boolean and then names something
    substantive has contradicted itself, and which of the two happened is not knowable
    from the counters — only from the text. So the text is logged."""
    with capture_logs() as logs:
        said, _ = _intent(
            {
                "description_covers_change": True,
                "undisclosed_changes": ["the sign convention on signed_amount was flipped"],
                "rationale": "covered",
            }
        )
    assert said == []
    discarded = [entry for entry in logs if entry["event"] == "intent.discarded_by_boolean"]
    assert discarded and "signed_amount" in discarded[0]["items"][0]


def test_intent_survives_a_model_that_answers_in_the_wrong_shape() -> None:
    """A string where a list belongs is the model failing, not the review failing."""
    said, _ = _intent(
        {"description_covers_change": False, "undisclosed_changes": "a sentence", "rationale": ""}
    )
    assert said == []


def test_intent_never_runs_without_a_description() -> None:
    """No description is not the same as an honest one, and there is nothing to compare
    the SQL against."""
    provider = FakeProvider({"description_covers_change": False, "undisclosed_changes": ["x"]})
    said = supervisor._intent_pass(
        provider,
        settings=Settings(),
        findings=[_finding()],
        changed_models=("fct_revenue",),
        pr_description="   ",
        snapshot=_snapshot(),
        usage=Usage(),
    )
    assert said == []
    assert provider.prompts == []


def test_intent_uses_the_supervisor_model_not_the_specialist_one() -> None:
    """Judgement rather than a narrow check, and it happens once per review."""
    _, provider = _intent({"description_covers_change": False, "undisclosed_changes": []})
    assert provider.models == [Settings().llm_supervisor_model]


def test_a_re_record_replaces_rather_than_accumulates(tmp_path: Any) -> None:
    """Merging is how a stale recording hides.

    A key is a hash of the prompt, so editing a prompt does not update its entry — it
    orphans it and adds a second. The file then holds two answers, only one of which is
    to a question still being asked, and nothing distinguishes them.
    """
    from themis.llm.cassette import Cassette

    path = tmp_path / "review.json"
    first = Cassette(path)
    first.put("old-prompt-key", {"verdict": "confirm"}, note="the prompt before the edit")
    first.save()

    merged = Cassette(path)
    assert len(merged) == 1

    fresh = Cassette(path, load=False)
    fresh.put("new-prompt-key", {"verdict": "refute"}, note="the prompt after the edit")
    fresh.save()
    assert len(Cassette(path)) == 1
    assert Cassette(path).get("old-prompt-key") is None


# --- what was shown, kept ------------------------------------------------------


def test_every_adjudication_is_captured_with_its_context() -> None:
    """The pack is assembled, sent, and was thrown away. Nothing could be tuned on a
    record that does not exist, and nothing could explain an answer a week later."""
    summary, provider = _run(
        {
            "verdict": "refute",
            "severity": "low",
            "rationale": "the key is unique",
            "evidence_quote": "New join to stg_fx_rates may fan out",
        },
        _finding(),
    )
    assert len(summary.calls) == 1
    call = summary.calls[0]
    assert call.seat == GRAIN.name
    assert call.context == provider.prompts[0]
    assert call.system.strip()
    assert call.response["verdict"] == "refute"
    assert call.finding is not None and call.finding.rule_id == "F1001"


def test_an_answer_the_selfcheck_rejected_is_captured_as_rejected() -> None:
    summary, _ = _run(
        {
            "verdict": "refute",
            "severity": "low",
            "rationale": "trust me",
            "evidence_quote": "a uniqueness test passed on rate_date",
        },
        _finding(),
    )
    assert summary.calls[0].accepted is False
    assert "does not appear" in (summary.calls[0].rejected_reason or "")


def test_a_call_that_never_happened_is_not_captured() -> None:
    provider = FakeProvider(fail=True)
    summary = supervisor.review(
        [_finding()],
        provider=provider,
        settings=Settings(),
        snapshot=_snapshot(),
        grains=_grains(),
    )
    assert summary.calls == []


# --- grounding: the short value after a colon ------------------------------------


def test_a_fabricated_short_value_after_a_colon_is_not_grounded() -> None:
    """Short pieces used to be skipped as insubstantial, so a fabricated value passed as
    long as its label was real: this quote was accepted against "materialization: view"."""
    context = "model: stg_0\nmaterialization: view\nmodels downstream: 4"
    assert not selfcheck.quote_is_grounded("materialization: incremental", context)


def test_a_true_short_value_beside_its_label_is_grounded() -> None:
    context = "model: stg_0\nmaterialization: view\nmodels downstream: 4"
    assert selfcheck.quote_is_grounded("materialization: view", context)


def test_a_short_value_borrowed_from_elsewhere_in_the_context_is_not_grounded() -> None:
    """Every word present somewhere is not the same as the claim being there."""
    context = "materialization: view\nincremental strategy: none recorded"
    assert not selfcheck.quote_is_grounded("materialization: incremental", context)


def test_lines_joined_with_commas_are_still_grounded() -> None:
    context = "stg_fx_rates looks unique on (currency_code)\nbut that is heuristic"
    assert selfcheck.quote_is_grounded(
        "stg_fx_rates looks unique on (currency_code), but that is heuristic", context
    )


def test_a_change_with_hundreds_of_findings_does_not_spend_hundreds_of_model_calls() -> None:
    """A refactor touching fifty models is a normal pull request and an hour of reviewing.

    Each open finding is a specialist call and possibly a fix call, at ten to twenty
    seconds on a local 8B model. The bound is on findings rather than on a clock, so the
    same review twice produces the same report — and what it skips is counted, not silent.
    """
    findings = [
        _finding().model_copy(update={"rule_id": f"F100{i % 9}", "title": f"finding {i}"})
        for i in range(25)
    ]
    provider = FakeProvider({"verdict": "uncertain", "severity": "high", "rationale": "x"})
    summary = supervisor.review(
        findings,
        provider=provider,
        settings=Settings(llm_max_findings_reviewed=10),
        snapshot=_snapshot(),
        grains=_grains(),
    )
    assert summary.not_reviewed_for_budget == 15
    assert len(summary.findings) == 25  # every finding is still reported
    # Two calls per reviewed finding at most (adjudicate, then a fix), never per finding
    # in the change.
    assert provider.models and len(provider.prompts) <= 2 * 10


def test_the_budget_spends_itself_on_the_worst_findings_first() -> None:
    """The order is the report's own, so "12 were not reviewed" means the bottom twelve."""
    worst = _finding(severity=Severity.CRITICAL).model_copy(update={"rule_id": "F1001"})
    rest = [
        _finding(severity=Severity.LOW).model_copy(update={"rule_id": f"F200{i}"}) for i in range(5)
    ]
    summary = supervisor.review(
        [*rest, worst],
        provider=FakeProvider({"verdict": "uncertain", "severity": "low", "rationale": "x"}),
        settings=Settings(llm_max_findings_reviewed=1),
        snapshot=_snapshot(),
        grains=_grains(),
    )
    assert summary.not_reviewed_for_budget == 5
    reviewed = [f for f in summary.findings if f.llm_rationale]
    assert [f.rule_id for f in reviewed] == ["F1001"]


def test_an_ordinary_review_is_not_bounded_at_all() -> None:
    summary = supervisor.review(
        [_finding()],
        provider=FakeProvider({"verdict": "uncertain", "severity": "high", "rationale": "x"}),
        settings=Settings(),
        snapshot=_snapshot(),
        grains=_grains(),
    )
    assert summary.not_reviewed_for_budget == 0
