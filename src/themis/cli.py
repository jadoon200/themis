"""THEMIS command line interface.

Every command is a thin wrapper over the pipeline stages so that CI, a developer's
shell, and the eval harness all exercise exactly the same code path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis import __version__
from themis.acquire.manifest import load_manifest
from themis.config import load_settings
from themis.logging import configure_logging, get_logger
from themis.models import Backend, Finding, GrainSource, Severity
from themis.report import markdown

app = typer.Typer(
    name="themis",
    help="Automated review of dbt model changes for financial data transformations.",
    no_args_is_help=True,
    add_completion=False,
)
log = get_logger(__name__)

ProjectOpt = Annotated[
    Path, typer.Option("--project", "-p", help="Path to the dbt project directory.")
]
BaseOpt = Annotated[str, typer.Option("--base", help="Base git revision to compare from.")]
HeadOpt = Annotated[str, typer.Option("--head", help="Head git revision to compare to.")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]


@app.callback()
def _root() -> None:
    """Shared entry point; per-command options configure logging themselves."""


@app.command()
def version() -> None:
    """Print the THEMIS version."""
    typer.echo(f"themis {__version__}")


@app.command()
def review(
    project: ProjectOpt = Path("demo_project"),
    base: BaseOpt = "main",
    head: HeadOpt = "HEAD",
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Deterministic analysis only. Free, fast, useful.")
    ] = False,
    execute: Annotated[
        bool,
        typer.Option("--execute/--no-execute", help="Build base and head and diff real results."),
    ] = False,
    pr_description: Annotated[
        str | None,
        typer.Option(
            "--pr-description",
            help="What the author says the change does. Enables the intent pass.",
        ),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Review the dbt model changes between two revisions."""
    from themis.pipeline import review as run_review

    configure_logging(verbose=verbose)
    settings = load_settings()
    log.info("review.start", project=str(project), base=base, head=head, llm=not no_llm)

    result = run_review(
        project,
        base=base,
        head=head,
        settings=settings,
        run_execution=execute or settings.execute_enabled,
        run_llm=not no_llm,
        pr_description=pr_description,
    )

    if result.execution is not None and not result.execution.ran:
        log.warning("review.execution_skipped", reason=result.execution.skipped_reason)

    if result.llm is not None and result.llm.undisclosed:
        typer.echo("")
        typer.echo("### Not mentioned in the description")
        typer.echo("")
        for item in result.llm.undisclosed:
            typer.echo(f"- {item}")
        typer.echo("")

    if result.llm is not None:
        usage = result.llm.usage
        log.info(
            "review.llm",
            adjudicated=result.llm.adjudicated,
            settled_without_llm=result.llm.settled_without_llm,
            suppressed=result.llm.suppressed,
            rejected=result.llm.rejected_by_selfcheck,
            calls=usage.calls,
            tokens=usage.prompt_tokens + usage.completion_tokens,
            seconds=round(usage.seconds, 1),
        )

    for macro, models in sorted(result.macro_affected.items()):
        log.info("review.macro_impact", macro=macro, models=len(models))

    typer.echo(
        markdown.render(
            result.findings,
            skipped=result.skipped,
            models_reviewed=len(result.models_reviewed),
            executed=result.executed,
            macro_affected=result.macro_affected,
            degraded_reason=result.degraded_reason,
        )
    )

    raise typer.Exit(code=_gate_exit_code(result.findings, settings.fail_on_severity))


@app.command()
def execute(
    project: ProjectOpt = Path("demo_project"),
    base: BaseOpt = "main",
    head: HeadOpt = "HEAD",
    explain: Annotated[
        bool, typer.Option("--explain", help="Show per-model deltas, not just the summary.")
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Stage 3 only: build base and head, then diff the actual results."""
    from themis.pipeline import review as run_review

    configure_logging(verbose=verbose)
    settings = load_settings()
    log.info("execute.start", project=str(project), base=base, head=head)

    result = run_review(project, base=base, head=head, settings=settings, run_execution=True)
    run = result.execution
    if run is None or not run.ran:
        reason = run.skipped_reason if run else "execution did not run"
        typer.echo(f"Execution did not run: {reason}", err=True)
        raise typer.Exit(code=2)

    if not run.deltas:
        typer.echo("No models built — nothing changed in this diff.")
        raise typer.Exit(code=0)

    for name in sorted(run.deltas):
        delta = run.deltas[name]
        marker = "CHANGED" if delta.is_material else "no change"
        typer.echo(f"{name:32s} {marker}")
        if not explain:
            continue
        if delta.build_error:
            typer.echo(f"{'':34s}build failed: {delta.build_error.strip()[:200]}")
            continue
        if delta.row_delta is not None:
            typer.echo(
                f"{'':34s}rows {delta.rows_before:,} -> {delta.rows_after:,} ({delta.row_delta:+,})"
            )
        for column, (before_sum, after_sum) in sorted(delta.sum_deltas.items()):
            flag = "  <-- moved" if before_sum != after_sum else ""
            typer.echo(f"{'':34s}sum({column}) {before_sum:,.2f} -> {after_sum:,.2f}{flag}")
        for column, (before_type, after_type) in sorted(delta.columns_retyped.items()):
            typer.echo(f"{'':34s}{column} retyped {before_type} -> {after_type}")

    measured = run.measured_grains
    if measured:
        typer.echo("\nGrain, measured rather than inferred:")
        for name in sorted(measured):
            typer.echo(f"  {name:30s} {measured[name].note}")

    material = run.material_models
    typer.echo(
        f"\n{len(material)} of {len(run.deltas)} model(s) changed materially."
        + (f" ({', '.join(material)})" if material else "")
    )
    raise typer.Exit(code=0)


@app.command()
def grain(
    project: ProjectOpt = Path("demo_project"),
    explain: Annotated[
        bool, typer.Option("--explain", help="Show which of the grain sources produced each key.")
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Show the derived grain for every model, and how it was derived.

    Verifiable independently of the rules, which matters because the fan-out family
    rests entirely on this and many projects declare no uniqueness tests to check it
    against.
    """
    from themis.analyze.grain import infer_grains

    configure_logging(verbose=verbose)
    settings = load_settings()

    manifest_path = project / "target" / "manifest.json"
    if not manifest_path.exists():
        typer.echo(
            f"No manifest at {manifest_path}. Run `dbt compile` in {project} first — "
            "`dbt parse` is not enough, it leaves Jinja unexpanded.",
            err=True,
        )
        raise typer.Exit(code=2)

    snapshot = load_manifest(manifest_path, revision="HEAD", backend=Backend.MANIFEST)
    grains = infer_grains(snapshot, dialect=settings.dialect)

    proven = sum(1 for g in grains.values() if g.is_proven)
    unknown = sum(1 for g in grains.values() if g.source is GrainSource.UNKNOWN)

    for name, grain in sorted(grains.items()):
        columns = ", ".join(grain.columns) or "—"
        line = f"{name:32s} {grain.source.value:12s} ({columns})"
        if explain and grain.note:
            line += f"\n{'':32s} {grain.note}"
        typer.echo(line)

    typer.echo(
        f"\n{len(grains)} model(s): {proven} proven, "
        f"{len(grains) - proven - unknown} weak, {unknown} unknown."
    )
    if unknown:
        typer.echo(
            "Unknown grain escalates rather than being assumed safe. "
            "Run with --execute to measure it instead of inferring."
        )
    raise typer.Exit(code=0)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What to ask about a completed review.")],
    run: Annotated[str, typer.Option("--run", help="Run id, or 'latest'.")] = "latest",
    verbose: VerboseOpt = False,
) -> None:
    """Ask a grounded follow-up question about a completed review.

    Answers come only from the persisted run artifact. A question the artifact cannot
    answer gets a refusal, never an inference.
    """
    from themis.ask.answer import answer_question
    from themis.ask.retrieval import gather, latest_run, run_by_key
    from themis.db.base import session_scope
    from themis.llm.provider import build_provider

    configure_logging(verbose=verbose)
    settings = load_settings()

    with session_scope() as session:
        stored = latest_run(session) if run == "latest" else run_by_key(session, run)
        if stored is None:
            typer.echo(
                "No completed review to ask about."
                if run == "latest"
                else f"No review with key {run}.",
                err=True,
            )
            raise typer.Exit(code=2)

        facts = gather(session, stored, question)
        context_is_empty = facts.is_empty
        run_key = stored.run_key

        # Answering happens inside the session because the facts are ORM rows; nothing
        # is written, and the model is never given a database handle.
        result = answer_question(
            question, facts, provider=build_provider(settings), settings=settings
        )

    typer.echo(f"[{run_key}]")
    typer.echo("")

    if result.grounded:
        typer.echo(result.text)
        if result.evidence_quote:
            typer.echo("")
            typer.echo(f"  based on: {result.evidence_quote}")
        raise typer.Exit(code=0)

    typer.echo(f"Cannot answer from this review: {result.refusal_reason}")
    if context_is_empty:
        typer.echo("")
        typer.echo(
            "This review recorded nothing about what you asked. That may itself be the "
            "answer — or the question is about something the review did not cover."
        )
    raise typer.Exit(code=1)


@app.command(name="eval")
def eval_cmd(
    project: ProjectOpt = Path("demo_project"),
    mutations: Annotated[
        str, typer.Option("--mutations", help="all, defects, controls, or a mutation id.")
    ] = "all",
    base: BaseOpt = "main",
    use_llm: Annotated[
        bool,
        typer.Option("--llm", help="Also run the model layer, and report what it added."),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the specialist model, to compare models."),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Run the mutation corpus and score the reviewer against it.

    Ground truth comes from execution, not from how each mutation was labelled: both
    revisions are built and the results compared, so a change that moves no number is
    treated as behaviour-preserving whatever it was called.
    """
    from themis.eval.harness import DirtyRepositoryError, run_corpus
    from themis.eval.mutations import Kind, select

    configure_logging(verbose=verbose)
    settings = load_settings()

    try:
        corpus = select(mutations)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if model:
        settings = settings.model_copy(
            update={"llm_specialist_model": model, "llm_supervisor_model": model}
        )

    try:
        report = run_corpus(project, corpus, settings=settings, base_ref=base, use_llm=use_llm)
    except DirtyRepositoryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo("")
    header = (
        f"{'mutation':34s} {'truth':10s} {'flagged':8s} {'families':12s} {'why':10s} {'result':15s}"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for outcome in report.outcomes:
        if outcome.error:
            typer.echo(
                f"{outcome.mutation.id:34s} {'—':10s} {'—':8s} {'—':12s} {outcome.error[:40]}"
            )
            continue
        if outcome.mutation.kind is Kind.LATENT:
            truth = "latent"
        else:
            truth = "defect" if outcome.changed_results else "no-change"
        families = ",".join(outcome.families_fired) or "—"
        flagged = "yes" if outcome.detected else "no"
        # Distinguish a defect caught by the family designed for it from one caught
        # incidentally by another. Both count as detected, but only the first means the
        # rule that was supposed to see it actually did.
        if not outcome.detected or not outcome.mutation.expects_family:
            why = "—"
        elif outcome.expected_family_fired:
            why = "expected"
        else:
            why = "incidental"
        typer.echo(
            f"{outcome.mutation.id:34s} {truth:10s} {flagged:8s} "
            f"{families:12s} {why:10s} {outcome.classification:15s}"
        )

    counts = report.counts()
    typer.echo("")
    if report.latent:
        detected = report.latent_detected
        typer.echo(
            f"latent defects (real, but produce no data change so the oracle cannot "
            f"judge them): {detected}/{len(report.latent)} detected"
        )
        for outcome in report.latent:
            mark = "caught" if outcome.detected else "MISSED"
            typer.echo(f"  {mark:7s} {outcome.mutation.id}")
        typer.echo("")
    typer.echo(
        f"true positives {counts['true_positive']}   "
        f"false negatives {counts['false_negative']}   "
        f"false positives {counts['false_positive']}   "
        f"true negatives {counts['true_negative']}"
    )

    def _pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    typer.echo(
        f"recall {_pct(report.recall)}   "
        f"precision {_pct(report.precision)}   "
        f"false-positive rate {_pct(report.false_positive_rate)}"
    )

    incidental = [
        o
        for o in report.scored
        if o.detected and o.mutation.expects_family and not o.expected_family_fired
    ]
    if incidental:
        typer.echo("")
        typer.echo("Caught, but not by the family meant to catch them:")
        for outcome in incidental:
            typer.echo(
                f"  {outcome.mutation.id}: expected {outcome.mutation.expects_family}, "
                f"fired {','.join(outcome.families_fired)}"
            )

    if use_llm:
        calls, tokens, seconds = report.llm_cost
        typer.echo("")
        typer.echo(
            f"model layer ({settings.llm_specialist_model}): {calls} call(s), "
            f"{tokens:,} tokens, {seconds:.0f}s"
        )
        typer.echo(
            f"  findings it removed: {report.llm_suppressed_total}   "
            f"answers rejected as ungrounded: {report.llm_rejected_total}"
        )
        if report.llm_suppressed_total == 0:
            typer.echo(
                "  It changed no decision on this corpus. That is the honest reading: "
                "the deterministic stages had already settled everything."
            )

    if report.mislabelled:
        typer.echo("")
        typer.echo("Declared kind disagrees with what execution measured:")
        for outcome in report.mislabelled:
            expected = (
                "should change results" if outcome.mutation.kind is Kind.DEFECT else "should not"
            )
            typer.echo(f"  {outcome.mutation.id}: {expected}, but it did not")

    if report.stale:
        typer.echo("")
        typer.echo("Could not be applied (the demo project has moved on):")
        for outcome in report.stale:
            typer.echo(f"  {outcome.mutation.id}: {outcome.error}")

    # A stale corpus silently measuring nothing is the failure mode worth guarding.
    raise typer.Exit(code=1 if report.stale else 0)


def _gate_exit_code(findings: list[Finding], fail_on: str | None) -> int:
    """Advisory by default: a review that blocks every merge stops being read.

    Blocking is opt-in per severity, and only findings at or above that severity
    fail the build.
    """
    if not fail_on:
        return 0
    order = [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
        Severity.INFO,
    ]
    try:
        threshold = order.index(Severity(fail_on))
    except ValueError:
        return 0

    return int(any(order.index(f.severity) <= threshold for f in findings))


if __name__ == "__main__":
    app()
