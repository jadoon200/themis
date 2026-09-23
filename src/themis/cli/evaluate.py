"""The mutation corpus and its gate."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis.cli._app import (
    ProjectOpt,
    VerboseOpt,
    app,
)
from themis.config import load_settings
from themis.logging import configure_logging


@app.command(name="eval")
def eval_cmd(
    project: ProjectOpt = Path("demo_project"),
    mutations: Annotated[
        str, typer.Option("--mutations", help="all, defects, controls, or a mutation id.")
    ] = "all",
    variant: Annotated[
        str | None,
        typer.Option(
            "--variant",
            help=(
                "Run against a variant of the demo project, e.g. 'tested' — the same "
                "models with their keys declared, to measure what derivation costs."
            ),
        ),
    ] = None,
    base: Annotated[
        str,
        typer.Option(
            "--base",
            help=(
                "Revision the mutations are applied to. HEAD by default: the corpus measures "
                "the reviewer on the demo project as committed, and a branch that changes it "
                "cannot be measured against another branch's copy."
            ),
        ),
    ] = "HEAD",
    use_llm: Annotated[
        bool,
        typer.Option("--llm", help="Also run the model layer, and report what it added."),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the specialist model, to compare models."),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute/--no-execute",
            help="Build both revisions. Without it there is no oracle and truth "
            "falls back to how each mutation was declared.",
        ),
    ] = True,
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help=(
                "dbt target every case builds on. 'dev' is DuckDB, which is cheap and is "
                "not the engine THEMIS targets; 'trino' measures on Trino, where a defect "
                "DuckDB cannot show becomes visible."
            ),
        ),
    ] = "dev",
    narrow: Annotated[
        bool,
        typer.Option(
            "--narrow/--no-narrow",
            help="Build only the models a change can reach. Run the corpus with it on to "
            "prove the narrowing catches everything the full build catches.",
        ),
    ] = False,
    allow_dirty: Annotated[
        bool,
        typer.Option(
            "--allow-dirty",
            help="Measure the committed state even with uncommitted changes present.",
        ),
    ] = False,
    generated_limit: Annotated[
        int, typer.Option("--generated-limit", help="How many generated mutations to run.")
    ] = 15,
    generated_seed: Annotated[
        int, typer.Option("--generated-seed", help="Seed, so a run is reproducible.")
    ] = 0,
    verbose: VerboseOpt = False,
) -> None:
    """Run the mutation corpus and score the reviewer against it.

    Ground truth comes from execution, not from how each mutation was labelled: both
    revisions are built and the results compared, so a change that moves no number is
    treated as behaviour-preserving whatever it was called.

    Exits 1 when the gate fails: a case that could not be scored (degraded grounding, or
    a build that failed without being meant to), a missed defect, latent case or unruled
    case, a flagged control, a mislabelled mutation, or — over `--mutations all` — any
    rule that never fired. Exit 2 means the run could not start.
    """
    from themis.eval.harness import DirtyRepositoryError, run_corpus
    from themis.eval.mutations import Kind, select

    configure_logging(verbose=verbose)
    settings = load_settings()

    if mutations == "generated":
        from themis.eval.generator import generate

        corpus = generate(project, limit=generated_limit, seed=generated_seed)
        if not corpus:
            typer.echo("No mutations could be generated from this project.", err=True)
            raise typer.Exit(code=2)
    else:
        corpus = ()

    try:
        corpus = corpus or select(mutations)
    except KeyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    if model:
        settings = settings.model_copy(
            update={"llm_specialist_model": model, "llm_supervisor_model": model}
        )

    try:
        report = run_corpus(
            project,
            corpus,
            settings=settings,
            base_ref=base,
            use_llm=use_llm,
            use_execution=execute,
            narrow_execution=narrow,
            allow_dirty=allow_dirty,
            variant=variant,
            target=target,
        )
    except DirtyRepositoryError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    typer.echo("")
    header = (
        f"{'mutation':34s} {'truth':10s} {'flagged':8s} {'n':4s} "
        f"{'families':12s} {'why':10s} {'result':15s}"
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
        elif outcome.mutation.kind is Kind.UNRULED:
            truth = "unruled"
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
            f"{outcome.mutation.id:34s} {truth:10s} {flagged:8s} {outcome.finding_count:<4d} "
            f"{families:12s} {why:10s} {outcome.classification:15s}"
        )

    counts = report.counts()
    typer.echo("")
    if not execute:
        typer.echo(
            "No execution oracle in this run: truth is the declared kind, not a "
            "measurement. Treat the numbers as weaker than an --execute run."
        )
        typer.echo("")
    if report.generated:
        moved = [o for o in report.generated if o.changed_results]
        typer.echo(
            f"generated mutations (produced from the code, not chosen): "
            f"{len(report.generated)} run, {len(moved)} moved the numbers"
        )
        if report.generated_missed:
            typer.echo(
                "  MISSED — these changed results and nothing reported them. Each is a "
                "defect class no rule covers:"
            )
            for outcome in report.generated_missed:
                typer.echo(f"    {outcome.mutation.id}: {outcome.mutation.description}")
        else:
            typer.echo("  all of them were reported")
        if report.generated_noise:
            typer.echo("  reported but changed nothing:")
            for outcome in report.generated_noise:
                typer.echo(f"    {outcome.mutation.id}: {outcome.mutation.description}")
        typer.echo("")

    if report.unruled:
        typer.echo(
            f"unruled defects (outside every rule family — the safety net's test): "
            f"{report.unruled_detected}/{len(report.unruled)} detected"
        )
        for outcome in report.unruled:
            mark = "caught" if outcome.detected else "MISSED"
            typer.echo(f"  {mark:7s} {outcome.mutation.id}")
        typer.echo("")

    fixable = sum(o.fixable_findings for o in report.usable)
    if fixable and use_llm:
        proposed = sum(o.fixes_proposed for o in report.usable)
        typer.echo(
            f"proposed fixes: {proposed}/{fixable} findings came back with corrected SQL "
            "(a proposal that does not parse, or that echoes the original, is discarded "
            "before it is counted)"
        )
        typer.echo("")

    described = report.with_description
    if described and use_llm:
        misleading = [o for o in described if not o.mutation.description_is_honest]
        honest = [o for o in described if o.mutation.description_is_honest]
        caught = sum(1 for o in misleading if o.undisclosed)
        alarms = sum(1 for o in honest if o.undisclosed)
        typer.echo(
            "intent pass (the only reviewer with no rule behind it): "
            f"{caught}/{len(misleading)} misleading descriptions caught, "
            f"{alarms}/{len(honest)} false alarms on honest ones"
        )
        for outcome in described:
            label = "honest    " if outcome.mutation.description_is_honest else "MISLEADING"
            said = "; ".join(outcome.undisclosed)[:150] or "—"
            typer.echo(f"  {label} {outcome.mutation.id[:34]:36s} {said}")
        typer.echo("")

    if report.benign:
        suppressed = sum(o.llm_suppressed for o in report.benign)
        benign_flagged = sum(1 for o in report.benign if o.detected)
        typer.echo(
            f"benign changes (safe, and a rule flags them anyway — recall-first "
            f"working as designed): {benign_flagged}/{len(report.benign)} flagged"
        )
        typer.echo(
            "  This ratio is the number to read, not the precision below it. Every "
            "benign case added lowers precision mechanically, so that figure describes "
            "the corpus as much as the tool; this one does not."
        )
        for outcome in report.benign:
            mark = "flagged" if outcome.detected else "silent"
            typer.echo(f"  {mark:8s} {outcome.mutation.id}: {outcome.mutation.description}")
        if use_llm:
            typer.echo(
                f"  the model layer suppressed {suppressed} of them. This is the only "
                "part of the corpus where it can show anything: everywhere else the "
                "rules are right, so agreeing with them changes nothing."
            )
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
    # How much a reviewer is asked to read per change. Recall-first means over-flagging
    # is deliberate, but it is only tunable if it is visible: a defect reported once is
    # a finding, and the same defect reported four times is four things to dismiss.
    reported = [o for o in report.usable if o.detected]
    if reported:
        counts_per = sorted(o.finding_count for o in reported)
        median = counts_per[len(counts_per) // 2]
        worst = max(reported, key=lambda o: o.finding_count)
        typer.echo(
            f"findings per flagged change: median {median}, worst {worst.finding_count} "
            f"({worst.mutation.id})"
        )
        # A change reported by families beyond the one that owns it is the shape noise
        # takes here: a generic rule restating what a specific one already said.
        extra = [o for o in reported if o.mutation.expects_family and len(o.families_fired) > 1]
        if extra:
            typer.echo(f"  {len(extra)} of {len(reported)} were reported by more than one family")
        typer.echo("")

    levels: dict[str, int] = {}
    for outcome in report.usable:
        for level in outcome.severities:
            levels[level] = levels.get(level, 0) + 1
    total_findings = sum(levels.values())
    if total_findings:
        shown = ", ".join(f"{levels[k]} {k}" for k in sorted(levels) if levels[k])
        typer.echo(f"severity mix across {total_findings} finding(s): {shown}")
        critical_share = levels.get("critical", 0) / total_findings
        # Stated, not warned about. This corpus is roughly half injected defects, so a
        # high critical share is what a working calibration looks like here — the
        # number worth watching is the one from real reviews, where most changes are
        # fine. Warning on it every run would be this tool doing the thing it exists
        # to stop: raising an alarm that is always on and therefore says nothing.
        typer.echo(
            f"  {critical_share:.0%} critical. Expect that to be high on a corpus of "
            "injected defects; on real pull requests it is the number to watch, and "
            "critical is capped at findings execution demonstrated."
        )
        typer.echo("")

    fired, never = report.rule_coverage()
    typer.echo(
        f"rule coverage: {len(fired)}/{len(fired) + len(never)} rules fired on at least one case"
    )
    if never:
        typer.echo(
            "  never fired: "
            + ", ".join(never)
            + " — unproven regardless of unit tests; a rule that cannot fire looks "
            "exactly like one that found nothing."
        )
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
            f"  findings removed: {report.llm_suppressed_total}   "
            f"causes proposed: {report.llm_explained_total}   "
            f"answers rejected as ungrounded: {report.llm_rejected_total}"
        )
        if report.llm_suppressed_total == 0 and report.llm_explained_total == 0:
            typer.echo(
                "  It changed no decision on this corpus. That is the honest reading: "
                "the deterministic stages had already settled everything."
            )
        elif report.llm_suppressed_total == 0:
            typer.echo(
                "  It suppressed nothing, so detection is entirely the rules' work. "
                "What it added is explanation of measured changes no rule accounts "
                "for — the one contribution rules cannot make."
            )

    if report.mislabelled:
        typer.echo("")
        typer.echo("Declared kind disagrees with what execution measured:")
        for outcome in report.mislabelled:
            if outcome.mutation.kind is Kind.DEFECT:
                typer.echo(f"  {outcome.mutation.id}: should change results, but it did not")
            else:
                typer.echo(f"  {outcome.mutation.id}: should not change results, but it did")

    if report.not_measurable:
        typer.echo("")
        typer.echo(f"Says nothing on this engine ({target}), by declaration:")
        for outcome in report.not_measurable:
            typer.echo(f"  {outcome.mutation.id}: {outcome.declared_unmeasurable}")

    if report.stale:
        typer.echo("")
        typer.echo("Could not be scored:")
        for outcome in report.stale:
            typer.echo(f"  {outcome.mutation.id}: {outcome.error}")

    # The gate. It used to fail on stale mutations alone, so a corpus reporting 9/29
    # rule coverage and four missed defects passed CI for ten days.
    failures = report.gate_failures(full_corpus=mutations == "all")
    typer.echo("")
    if failures:
        typer.echo(f"gate: FAIL ({len(failures)})")
        for failure in failures:
            typer.echo(f"  {failure}")
        raise typer.Exit(code=1)
    typer.echo("gate: pass")
    raise typer.Exit(code=0)
