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
ProdManifestOpt = Annotated[
    Path | None,
    typer.Option(
        "--prod-manifest",
        help=(
            "manifest.json from a production build, or the directory holding it. The "
            "base is read from it instead of being recompiled from git."
        ),
    ),
]
DeferStateOpt = Annotated[
    Path | None,
    typer.Option(
        "--defer-state",
        help=(
            "Directory holding a manifest.json from an existing build. Unselected "
            "upstreams resolve there instead of being rebuilt."
        ),
    ),
]


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
    prod_manifest: ProdManifestOpt = None,
    defer_state: DeferStateOpt = None,
    no_manifest_cache: Annotated[
        bool,
        typer.Option("--no-manifest-cache", help="Recompile every revision, ignoring .themis/."),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Review the dbt model changes between two revisions.

    `--prod-manifest` and `--defer-state` both take production build artifacts and can
    be given the same `target/` directory: the first reads the base from it instead of
    recompiling, the second stops Stage 3 rebuilding upstreams that already exist.
    """
    from themis.pipeline import review as run_review

    configure_logging(verbose=verbose)
    settings = load_settings()
    log.info("review.start", project=str(project), base=base, head=head, llm=not no_llm)

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
            untested_grains=result.untested_grains,
            governed_models=result.governed_models,
        )
    )

    if sarif is not None:
        from themis.report import sarif as sarif_report

        sarif.parent.mkdir(parents=True, exist_ok=True)
        sarif.write_text(
            sarif_report.render(result.findings, governed_models=result.governed_models)
        )
        log.info("review.sarif_written", path=str(sarif), findings=len(result.findings))

    if save:
        _persist(result, project=str(project), base=base, head=head, execute=execute)

    raise typer.Exit(code=_gate_exit_code(result.findings, settings.fail_on_severity))


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

    result = run_review(
        project,
        base=base,
        head=head,
        settings=settings,
        run_execution=True,
        defer_state=defer_state,
    )
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


@app.command(name="suggest-tests")
def suggest_tests_cmd(
    project: ProjectOpt = Path("demo_project"),
    emit_yaml: Annotated[
        bool, typer.Option("--yaml", help="Print a schema.yml fragment instead of a summary.")
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Emit the uniqueness tests the project never declared.

    THEMIS derives grain because nothing asserts it; this hands the derivation back as
    something the project can adopt. Only proven grains are offered — a suggested test
    that fails on first run teaches the reader that these are guesses.
    """
    from themis.analyze.grain import infer_grains
    from themis.analyze.lineage import build_column_graph
    from themis.analyze.suggest import render_yaml, suggest_tests

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
    # Column lineage supplies each model's real output columns, so a key naming
    # something the final SELECT never projects is dropped rather than printed.
    graph = build_column_graph(snapshot, dialect=settings.dialect)
    suggestions = suggest_tests(snapshot, grains, outputs=graph.outputs)

    if not suggestions:
        typer.echo("Nothing to suggest: every derivable grain is already asserted.")
        raise typer.Exit(code=0)

    if emit_yaml:
        typer.echo(render_yaml(suggestions))
        raise typer.Exit(code=0)

    for suggestion in suggestions:
        columns = ", ".join(suggestion.columns)
        typer.echo(f"{suggestion.model_name:32s} {suggestion.test_name}({columns})")
        typer.echo(f"{'':32s} {suggestion.evidence}")

    # Seeds are excluded from the denominator: their grain can only be measured, never
    # derived, so counting them as failures of derivation overstates the gap.
    unproven = sum(
        1
        for name, grain in grains.items()
        if not grain.is_proven and name in snapshot.models and not snapshot.models[name].is_seed
    )
    typer.echo(
        f"\n{len(suggestions)} test(s) suggested; {unproven} SQL model(s) have no "
        "derivable grain and get nothing — those need a human to say what the key is, "
        "or an `--execute` run to measure it."
    )
    typer.echo("Re-run with --yaml for a schema.yml fragment.")
    raise typer.Exit(code=0)


@app.command()
def cache(
    project: ProjectOpt = Path("demo_project"),
    clear: Annotated[bool, typer.Option("--clear", help="Delete every cached manifest.")] = False,
    warm: Annotated[
        str | None,
        typer.Option("--warm", help="Compile this revision into the cache ahead of a review."),
    ] = None,
    target: Annotated[str, typer.Option("--target", help="dbt target to compile against.")] = "dev",
    verbose: VerboseOpt = False,
) -> None:
    """Inspect or clear the compiled-manifest cache.

    dbt writes its manifest into `target/`, which every project gitignores, so a
    manifest is never something a review finds — it is something THEMIS compiles. The
    cache means it compiles each revision once instead of once per review.
    """
    from themis.acquire import git
    from themis.acquire.cache import ManifestCache

    configure_logging(verbose=verbose)
    root = git.repo_root(project) / ".themis"
    store = ManifestCache(root)

    if clear:
        removed = store.clear()
        typer.echo(f"Cleared {removed} cached manifest(s) from {root / 'manifests'}.")
        raise typer.Exit(code=0)

    if warm is not None:
        from themis.acquire.snapshot_builder import warm_cache

        settings = load_settings()
        ok, detail = warm_cache(
            project,
            revision=warm,
            target=target,
            allowed_targets=settings.execute_allowed_targets,
            timeout_s=settings.execute_timeout_s,
        )
        typer.echo(detail)
        raise typer.Exit(code=0 if ok else 1)

    entries = sorted((root / "manifests").glob("*.json")) if root.exists() else []
    for entry in entries:
        size_mb = entry.stat().st_size / 1_000_000
        typer.echo(f"{entry.name:48s} {size_mb:6.1f} MB")
    total = sum(e.stat().st_size for e in entries) / 1_000_000
    typer.echo(f"\n{len(entries)} cached manifest(s), {total:.1f} MB, in {root / 'manifests'}.")
    raise typer.Exit(code=0)


@app.command()
def lineage(
    project: ProjectOpt = Path("demo_project"),
    model: Annotated[
        str | None, typer.Option("--model", help="Restrict the report to one model.")
    ] = None,
    column: Annotated[
        str | None, typer.Option("--column", help="Trace one column, up and down.")
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Show column-level lineage: what feeds a column, and what would break without it.

    With no arguments this reports coverage — how many models resolved and which did
    not — because a lineage answer is only as trustworthy as the share of the project
    it could actually resolve. An unresolved model is unknown, never clean.
    """
    from themis.analyze.lineage import build_column_graph

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
    graph = build_column_graph(snapshot, dialect=settings.dialect)

    if column is not None:
        if model is None:
            typer.echo("--column needs --model: a column name alone is ambiguous.", err=True)
            raise typer.Exit(code=2)
        if not graph.is_traced(model):
            reason = graph.unresolved.get(model, "not traced")
            typer.echo(f"{model}: lineage unresolved ({reason}). Treat as unknown.", err=True)
            raise typer.Exit(code=2)
        sources = graph.sources_of(model, column)
        feeds = graph.consumers_of(model, column)
        referenced = graph.referencing_models(model, column)
        typer.echo(f"{model}.{column}")
        typer.echo(f"  reads from : {', '.join(str(r) for r in sources) or '—'}")
        typer.echo(f"  feeds      : {', '.join(str(r) for r in feeds) or '—'}")
        typer.echo(f"  joined on  : {', '.join(referenced) or '—'}")
        raise typer.Exit(code=0)

    names = [model] if model else sorted(graph.outputs)
    for name in names:
        if not graph.is_traced(name):
            typer.echo(f"{name:32s} unresolved — {graph.unresolved.get(name, 'not traced')}")
            continue
        columns = graph.outputs.get(name, ())
        readers = {reader for col in columns for reader in graph.consumer_models(name, col)}
        typer.echo(f"{name:32s} {len(columns):3d} column(s), read by {len(readers)} model(s)")

    # Seeds are CSV, not SQL: they are legitimate roots, not resolution failures, so
    # counting them in the denominator would understate coverage.
    analysable = sum(1 for m in snapshot.models.values() if not m.is_seed)
    typer.echo(
        f"\n{len(graph.outputs)} of {analysable} SQL model(s) resolved; "
        f"{len(graph.unresolved)} unresolved."
    )
    if graph.unresolved:
        typer.echo(
            "Unresolved models are reported as unknown rather than as having no "
            "consumers — a lineage tool that goes quiet is how a breaking change passes."
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
    execute: Annotated[
        bool,
        typer.Option(
            "--execute/--no-execute",
            help="Build both revisions. Without it there is no oracle and truth "
            "falls back to how each mutation was declared.",
        ),
    ] = True,
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
            allow_dirty=allow_dirty,
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

    if report.benign:
        suppressed = sum(o.llm_suppressed for o in report.benign)
        benign_flagged = sum(1 for o in report.benign if o.detected)
        typer.echo(
            f"benign changes (safe, and a rule flags them anyway — recall-first "
            f"working as designed): {benign_flagged}/{len(report.benign)} flagged"
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
        if critical_share > 0.15:
            typer.echo(
                f"  {critical_share:.0%} are critical — a level most findings reach "
                "stops saying which one to open first."
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


def _persist(result: object, *, project: str, base: str, head: str, execute: bool) -> None:
    """Record a CLI run alongside the ones the service records.

    Without this the store only ever held API-driven runs, so `themis ask` could not
    answer about a review someone had just run and finding history had a hole in it
    exactly where the tool is used most.
    """
    from themis.db.base import session_scope
    from themis.db.models import RunSource, RunStatus
    from themis.db.store import enqueue_run, save_result
    from themis.pipeline import ReviewResult

    if not isinstance(result, ReviewResult):
        return
    try:
        with session_scope() as session:
            run = enqueue_run(
                session,
                project=project,
                base_ref=base,
                head_ref=head,
                source=RunSource.CLI,
                execute=execute,
            )
            run.status = RunStatus.RUNNING
            save_result(session, run, result)
            log.info("review.saved", run_key=run.run_key)
    except Exception as exc:
        # Never fail a review because history could not be written. The findings the
        # reviewer needs are already on screen.
        log.warning("review.not_saved", error=str(exc)[:200])


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
