"""Reading a compiled project: grain, lineage, suggested tests, profile, cache."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from themis.acquire.manifest import load_manifest
from themis.cli._app import (
    ProjectOpt,
    VerboseOpt,
    app,
)
from themis.config import load_settings
from themis.logging import configure_logging
from themis.models import Backend, GrainSource


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


@app.command(name="profile")
def profile_cmd(
    project: ProjectOpt = Path("demo_project"),
    as_json: Annotated[bool, typer.Option("--json", help="Print the profile as JSON.")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Describe a project's shape in counts, naming nothing in it.

    How much of its SQL parses, how deep it goes, how far its macros reach, how much grain
    and lineage THEMIS can derive, and how often the configured vocabulary matches its
    columns, tags and folders. Built to be shared from a project whose code cannot be.
    """

    from themis import vocabulary
    from themis.analyze.grain import infer_grains
    from themis.analyze.lineage import build_column_graph
    from themis.analyze.profile import profile

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
    result = profile(
        snapshot,
        infer_grains(snapshot, dialect=settings.dialect),
        build_column_graph(snapshot, dialect=settings.dialect),
        vocabulary.from_settings(settings),
        dialect=settings.dialect,
    )
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(code=0)

    def emit(section: dict[str, Any], indent: int = 0) -> None:
        for key, value in section.items():
            if isinstance(value, dict):
                typer.echo(f"{'  ' * indent}{key}:")
                emit(value, indent + 1)
            else:
                typer.echo(f"{'  ' * indent}{key}: {value}")

    emit(result)
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
        if model not in snapshot.models:
            # A typo must not read as an answer. Printing "feeds — / joined on —" for
            # a name the project has never heard of tells a reviewer the column is
            # safe to remove, which is the worst sentence this command can produce.
            typer.echo(
                f"No model named {model!r} in {project}. "
                "Run `themis lineage` with no arguments to list what there is.",
                err=True,
            )
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

    if model is not None and model not in snapshot.models:
        typer.echo(f"No model named {model!r} in {project}.", err=True)
        raise typer.Exit(code=2)
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
