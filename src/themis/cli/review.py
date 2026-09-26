"""Reviewing a change: the product, and Stage 3 on its own."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis import __version__
from themis.boundary import detect as detect_boundary
from themis.cli._app import (
    _REVIEW_ERRORS,
    BaseOpt,
    DeferStateOpt,
    HeadOpt,
    ProdManifestOpt,
    ProjectOpt,
    VerboseOpt,
    app,
    log,
)
from themis.cli._shared import (
    EXIT_INCOMPLETE,
    _history,
    _persist,
    _review_exit_code,
)
from themis.config import load_settings
from themis.logging import conceal_values, configure_logging
from themis.report import markdown
from themis.report.conceal import conceal_execution, conceal_review


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
    save: Annotated[
        bool,
        typer.Option(
            "--save/--no-save",
            help="Record the run so it can be asked about later and compared with earlier runs.",
        ),
    ] = True,
    execute: Annotated[
        bool,
        typer.Option("--execute/--no-execute", help="Build base and head and diff real results."),
    ] = False,
    narrow: Annotated[
        bool,
        typer.Option(
            "--narrow/--no-narrow",
            help=(
                "With --execute, build only the models that read a column the change "
                "touched. Refuses to narrow unless it can prove the set, and names what "
                "it skipped."
            ),
        ),
    ] = False,
    pr_description: Annotated[
        str | None,
        typer.Option(
            "--pr-description",
            help="What the author says the change does. Enables the intent pass.",
        ),
    ] = None,
    target: Annotated[
        str, typer.Option("--target", help="dbt target to compile and build against.")
    ] = "dev",
    sarif: Annotated[
        Path | None,
        typer.Option(
            "--sarif",
            help="Also write a SARIF 2.1.0 log here, for inline annotations in CI.",
        ),
    ] = None,
    json_out: Annotated[
        Path | None,
        typer.Option(
            "--json",
            help="Also write the full run as JSON: findings, deltas, grains, and skips.",
        ),
    ] = None,
    prod_manifest: ProdManifestOpt = None,
    defer_state: DeferStateOpt = None,
    no_manifest_cache: Annotated[
        bool,
        typer.Option("--no-manifest-cache", help="Recompile every revision, ignoring .themis/."),
    ] = False,
    redact: Annotated[
        bool,
        typer.Option(
            "--redact",
            help=(
                "Write SARIF and JSON with no SQL, no measured values and hashed names — "
                "safe to share outside the team that owns the project."
            ),
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Review the dbt model changes between two revisions.

    Exit codes: 0 pass (or advisory), 1 a finding at or above THEMIS_FAIL_ON_SEVERITY,
    2 the review could not start, 3 blocking is on and the review is incomplete —
    grounding degraded, checks skipped, or execution requested and not run.

    `--prod-manifest` and `--defer-state` both take production build artifacts and can
    be given the same `target/` directory: the first reads the base from it instead of
    recompiling, the second stops Stage 3 rebuilding upstreams that already exist.
    """
    from themis.pipeline import review as run_review

    configure_logging(verbose=verbose)
    settings = load_settings()
    # Decided before the run: dbt's error text is logged during it.
    boundary = detect_boundary(project, target=target, settings=settings)
    conceal_values(boundary.conceal)
    log.info("review.start", project=str(project), base=base, head=head, llm=not no_llm)

    try:
        result = run_review(
            project,
            base=base,
            head=head,
            settings=settings,
            target=target,
            run_execution=execute or settings.execute_enabled,
            narrow_execution=narrow,
            run_llm=not no_llm,
            pr_description=pr_description,
            prod_manifest=prod_manifest,
            defer_state=defer_state,
            use_manifest_cache=not no_manifest_cache,
            history=_history(str(project), settings),
        )
    except _REVIEW_ERRORS as exc:
        typer.echo(f"Review could not run: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if result.execution is not None and not result.execution.ran:
        log.warning("review.execution_skipped", reason=result.execution.skipped_reason)

    # Everything below is what this reader sees. With real data and an AI assistant reading,
    # it is a copy with the warehouse's values withheld; the full review is what is stored.
    stored = result
    if boundary.conceal:
        result = conceal_review(result)
        typer.echo(f"> Measured values withheld: {boundary.reason}.\n")

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
            withheld=result.llm.withheld_for_planted_text,
            not_reviewed=result.llm.not_reviewed_for_budget,
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
            untested_grains=result.untested_grains,
            governed_models=result.governed_models,
            seed_affected=result.seed_affected,
        )
    )

    if sarif is not None:
        from themis.report import sarif as sarif_report

        sarif.parent.mkdir(parents=True, exist_ok=True)
        sarif.write_text(
            sarif_report.render(
                result.findings,
                governed_models=result.governed_models,
                incomplete=result.incomplete,
                redact=settings.redact_salt if redact else None,
            )
        )
        log.info("review.sarif_written", path=str(sarif), findings=len(result.findings))

    if json_out is not None:
        from themis.report import json_out as json_report

        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json_report.render(
                result.findings,
                skipped=result.skipped,
                grains=result.grains,
                deltas=result.execution.deltas if result.execution else None,
                models_reviewed=result.models_reviewed,
                executed=result.executed,
                degraded_reason=result.degraded_reason,
                governed_models=result.governed_models,
                untested_grains=result.untested_grains,
                llm=result.llm,
                seed_affected=result.seed_affected,
                narrowing=result.narrowing,
                incomplete=result.incomplete,
                redact=settings.redact_salt if redact else None,
            )
        )
        log.info("review.json_written", path=str(json_out), findings=len(result.findings))

    if save:
        _persist(
            stored,
            project=str(project),
            base=base,
            head=head,
            execute=execute or settings.execute_enabled,
        )

    code = _review_exit_code(result, settings.fail_on_severity)
    if code == EXIT_INCOMPLETE:
        typer.echo("Merge gate: the review is incomplete, so it cannot pass:", err=True)
        for reason in result.incomplete_reasons:
            typer.echo(f"  - {reason}", err=True)
    raise typer.Exit(code=code)


@app.command()
def execute(
    project: ProjectOpt = Path("demo_project"),
    base: BaseOpt = "main",
    head: HeadOpt = "HEAD",
    defer_state: DeferStateOpt = None,
    explain: Annotated[
        bool, typer.Option("--explain", help="Show per-model deltas, not just the summary.")
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Stage 3 only: build base and head, then diff the actual results."""
    from themis.pipeline import review as run_review

    configure_logging(verbose=verbose)
    settings = load_settings()
    boundary = detect_boundary(project, target="dev", settings=settings)
    conceal_values(boundary.conceal)
    log.info("execute.start", project=str(project), base=base, head=head)

    try:
        result = run_review(
            project,
            base=base,
            head=head,
            settings=settings,
            run_execution=True,
            defer_state=defer_state,
        )
    except _REVIEW_ERRORS as exc:
        typer.echo(f"Execution could not run: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    run = result.execution
    if boundary.conceal and run is not None:
        run = conceal_execution(run)
        typer.echo(f"> Measured values withheld: {boundary.reason}.\n")
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
        if delta.concealed:
            for note in delta.withheld:
                typer.echo(f"{'':34s}{note}")
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
def backtest(
    project: ProjectOpt = Path("demo_project"),
    last: Annotated[
        int, typer.Option("--last", help="How many merged changes to replay, newest first.")
    ] = 20,
    ref: Annotated[
        str, typer.Option("--ref", help="Branch whose first-parent history to replay.")
    ] = "HEAD",
    target: Annotated[
        str, typer.Option("--target", help="dbt target to compile (and build) against.")
    ] = "dev",
    execute: Annotated[
        bool,
        typer.Option(
            "--execute/--no-execute",
            help="Also build both revisions of every change and measure. Slow; off by default.",
        ),
    ] = False,
    subjects: Annotated[
        bool,
        typer.Option(
            "--subjects/--no-subjects",
            help="Show commit subjects. Off by default: at work a subject can name a client.",
        ),
    ] = False,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Write every row and the summary as JSON.")
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """What THEMIS would have said about the changes that already merged.

    Replays the last N changes to the dbt project on a branch's first-parent history, each
    reviewed against its first parent — rules only unless --execute — and prints counts:
    findings by severity, which rules fired, what could not be reviewed. Nothing is
    written anywhere, and nothing printed names a model or a column, so the summary can
    be shared. The first thing to run on a project THEMIS has never seen.
    """
    import json

    from themis.backtest import backtest as run_backtest
    from themis.backtest import changes_to_replay, summarise

    configure_logging(verbose=verbose)
    settings = load_settings()
    changes = changes_to_replay(project, last=last, ref=ref)
    if not changes:
        typer.echo(f"No change to {project} on the first-parent history of {ref}.", err=True)
        raise typer.Exit(code=2)

    rows = run_backtest(project, changes, settings=settings, target=target, execute=execute)

    typer.echo(f"{'commit':<11} {'models':>6} {'crit':>4} {'high':>4} {'med':>4} {'low':>4}  rules")
    for row in rows:
        subject = f"  {row.change.subject[:60]}" if subjects else ""
        if row.error is not None:
            typer.echo(f"{row.change.commit[:10]:<11} could not be reviewed: {row.error[:90]}")
            continue
        s = row.severities
        typer.echo(
            f"{row.change.commit[:10]:<11} {row.models:>6} {s['critical']:>4} {s['high']:>4} "
            f"{s['medium']:>4} {s['low']:>4}  {', '.join(row.rules) or '-'}"
            + (f"  [{row.skipped} skipped]" if row.skipped else "")
            + subject
        )

    summary = summarise(rows)
    typer.echo("")
    typer.echo(
        f"{summary['reviewed']} of {summary['changes']} changes reviewed; "
        f"{summary['with_findings']} with at least one finding; "
        f"median {summary['findings_median']} finding(s), worst {summary['findings_max']}; "
        f"median {summary['seconds_median']}s each"
    )
    if summary["could_not_review"]:
        typer.echo(f"{summary['could_not_review']} could not be reviewed — see the rows above.")
    if summary["incomplete"]:
        typer.echo(f"{summary['incomplete']} reviewed with checks skipped or grounding degraded.")
    top = ", ".join(f"{rule} x{count}" for rule, count in list(summary["rules"].items())[:8])
    typer.echo(f"rules that fired most: {top or 'none'}")

    if json_out is not None:
        payload = {
            "summary": summary,
            "rows": [
                {
                    "commit": row.change.commit,
                    "parent": row.change.parent,
                    **({"subject": row.change.subject} if subjects else {}),
                    "models": row.models,
                    "severities": row.severities,
                    "rules": list(row.rules),
                    "skipped": row.skipped,
                    "incomplete": list(row.incomplete),
                    "seconds": round(row.seconds, 1),
                    "error": row.error,
                }
                for row in rows
            ],
        }
        json_out.write_text(json.dumps(payload, indent=2))
        typer.echo(f"wrote {json_out}")
