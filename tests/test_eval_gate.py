"""The corpus gate — what makes a CI run of the eval fail.

The job that runs the corpus reported 9/29 rule coverage, 76% recall and four missed
defects, and passed, for ten days: its exit code depended on stale mutations and nothing
else, and an outcome never recorded that twenty rules had been skipped. These tests pin
what a run must now refuse to call a pass.
"""

from __future__ import annotations

from themis.eval.harness import EvalReport, MutationOutcome, _unscorable
from themis.eval.mutations import Kind, Mutation
from themis.execute.runner import ExecutionResult
from themis.models import ExecutionDelta
from themis.pipeline import ReviewResult


def _mutation(
    kind: Kind = Kind.DEFECT, *, build_fails: str | None = None, id: str = "m"
) -> Mutation:
    return Mutation(
        id=id,
        kind=kind,
        expects_family="F1",
        description="d",
        relative_path="models/m.sql",
        find="a",
        replace="b",
        build_fails=build_fails,
    )


def _executed(**deltas: ExecutionDelta) -> ReviewResult:
    return ReviewResult(executed=True, execution=ExecutionResult(deltas=dict(deltas)))


# --- which reviews can be scored at all -------------------------------------------------


def test_a_review_with_degraded_grounding_is_not_scored() -> None:
    """The CI case: compile aborted, twenty rules skipped, X0001 fired, "detected"."""
    result = ReviewResult(degraded_reason="the head manifest has no compiled SQL")
    reason = _unscorable(_mutation(), result, use_execution=True)
    assert reason is not None and "grounding degraded" in reason


def test_a_head_that_does_not_build_is_an_invalid_mutation_unless_declared() -> None:
    result = _executed(
        mart=ExecutionDelta(model_name="mart", build_error="Binder Error", failed_revision="head")
    )
    assert "does not build" in (_unscorable(_mutation(), result, use_execution=True) or "")
    assert (
        _unscorable(_mutation(build_fails="the breakage is the defect"), result, use_execution=True)
        is None
    )


def test_a_mutation_declared_to_break_the_build_must_break_it() -> None:
    result = _executed(mart=ExecutionDelta(model_name="mart", rows_before=1, rows_after=2))
    reason = _unscorable(_mutation(build_fails="meant to"), result, use_execution=True)
    assert reason is not None and "but it built" in reason


def test_a_base_that_does_not_build_is_never_scorable() -> None:
    result = _executed(
        mart=ExecutionDelta(model_name="mart", build_error="x", failed_revision="base")
    )
    assert "base revision" in (
        _unscorable(_mutation(build_fails="meant to"), result, use_execution=True) or ""
    )


def test_without_execution_the_build_is_not_judged() -> None:
    assert (
        _unscorable(_mutation(build_fails="meant to"), ReviewResult(), use_execution=False) is None
    )


# --- the gate ------------------------------------------------------------------------------


def _outcome(
    kind: Kind = Kind.DEFECT,
    *,
    changed: bool = True,
    detected: bool = True,
    rules: tuple[str, ...] = ("F1001",),
    error: str | None = None,
    id: str = "m",
) -> MutationOutcome:
    return MutationOutcome(
        mutation=_mutation(kind, id=id),
        applied=True,
        changed_results=changed,
        detected=detected,
        families_fired=("F1",) if detected else (),
        expected_family_fired=detected,
        finding_count=1 if detected else 0,
        rules_fired=rules if detected else (),
        error=error,
    )


def test_a_clean_corpus_passes() -> None:
    report = EvalReport([_outcome(), _outcome(Kind.CONTROL, changed=False, detected=False, id="c")])
    assert report.gate_failures(full_corpus=False) == []


def test_a_missed_defect_fails() -> None:
    report = EvalReport([_outcome(detected=False)])
    assert any("not reported" in f for f in report.gate_failures(full_corpus=False))


def test_an_unscorable_case_fails() -> None:
    report = EvalReport([_outcome(error="grounding degraded — no compiled SQL")])
    assert any("could not be scored" in f for f in report.gate_failures(full_corpus=False))


def test_a_flagged_control_fails() -> None:
    report = EvalReport([_outcome(Kind.CONTROL, changed=False, detected=True)])
    assert any("control was flagged" in f for f in report.gate_failures(full_corpus=False))


def test_a_missed_latent_or_unruled_case_fails() -> None:
    report = EvalReport(
        [
            _outcome(Kind.LATENT, changed=False, detected=False, id="l"),
            _outcome(Kind.UNRULED, detected=False, id="u"),
        ]
    )
    failures = report.gate_failures(full_corpus=False)
    assert any(f.startswith("l:") for f in failures)
    assert any(f.startswith("u:") for f in failures)


def test_a_flagged_benign_case_does_not_fail_it() -> None:
    """Recall-first working as designed. Reported, never gated."""
    report = EvalReport([_outcome(Kind.BENIGN, changed=False, detected=True)])
    assert report.gate_failures(full_corpus=False) == []


def test_a_benign_case_that_moved_the_numbers_is_mislabelled() -> None:
    """Skipping benign cases in this check is how one that did not build went unseen."""
    report = EvalReport([_outcome(Kind.BENIGN, changed=True, detected=True)])
    assert [o.mutation.id for o in report.mislabelled] == ["m"]
    assert report.gate_failures(full_corpus=False)


def test_rule_coverage_is_gated_only_over_the_whole_corpus() -> None:
    report = EvalReport([_outcome(rules=("F1001",))])
    assert report.gate_failures(full_corpus=False) == []
    assert any("never fired" in f for f in report.gate_failures(full_corpus=True))


def test_a_generated_mutation_that_could_not_be_scored_is_reported_not_gated() -> None:
    report = EvalReport([_outcome(Kind.GENERATED, error="head does not build")])
    assert report.gate_failures(full_corpus=False) == []


def test_every_corpus_case_that_breaks_the_build_says_why() -> None:
    """A declared build failure is a claim about the case; it must carry its reason."""
    from themis.eval.mutations import ALL

    for mutation in ALL:
        if mutation.build_fails is not None:
            assert len(mutation.build_fails) > 20, mutation.id
