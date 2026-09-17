"""Reviewing a change: the product, and Stage 3 on its own."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis import __version__
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
from themis.logging import configure_logging
from themis.report import markdown


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
    log.info("review.start", project=str(project), base=base, head=head, llm=not no_llm)

    try:
        result = run_review(
            project,
            base=base,
            head=head,
            settings=settings,
            target=target,
            run_execution=execute or settings.execute_enabled,
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
                incomplete=result.incomplete,
                redact=settings.redact_salt if redact else None,
            )
        )
        log.info("review.json_written", path=str(json_out), findings=len(result.findings))

    if save:
        _persist(
            result,
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
