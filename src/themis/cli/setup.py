"""Setting a project up: what is missing, starter configuration, conventions."""

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


@app.command()
def doctor(
    project: ProjectOpt = Path("demo_project"),
    target: Annotated[
        str, typer.Option("--target", help="The dbt target reviews will use.")
    ] = "dev",
    verbose: VerboseOpt = False,
) -> None:
    """Check everything a review on this project needs, and say how to fix what is missing.

    Python, dbt and the adapter the target uses, where the profile is, the target allowlist,
    git, the compiled manifest, the local model and its context window, the database and
    its migrations, conventions, and the redaction salt. Exits 1 if anything fails.
    """
    from themis.onboarding import run_checks

    configure_logging(verbose=verbose)
    checks = run_checks(project, load_settings(), target=target)
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "skip": "--  "}
    width = max(len(check.name) for check in checks)
    for check in checks:
        typer.echo(f"{marks[check.status]}  {check.name:<{width}}  {check.detail}")
        if check.fix and check.status in ("warn", "fail"):
            typer.echo(f"      {'':<{width}}  fix: {check.fix}")
    failed = sum(1 for check in checks if check.status == "fail")
    warned = sum(1 for check in checks if check.status == "warn")
    typer.echo("")
    typer.echo(f"{failed} failing, {warned} warning(s), {len(checks)} checked.")
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def init(
    project: ProjectOpt = Path("demo_project"),
    verbose: VerboseOpt = False,
) -> None:
    """Write the configuration a project needs: .env here, a conventions template there.

    Never overwrites an existing file. Proposes only targets whose names do not say
    production for the allowlist — confirm none of them is production before relying on it.
    """
    from themis.onboarding import initialise

    configure_logging(verbose=verbose)
    if not (project / "dbt_project.yml").exists():
        typer.echo(f"No dbt_project.yml in {project}.", err=True)
        raise typer.Exit(code=2)
    for written in initialise(project, Path.cwd()):
        action = "wrote" if written.created else "kept "
        typer.echo(f"{action} {written.path} — {written.note}")
    typer.echo("")
    typer.echo(f"Next: themis doctor --project {project}")


@app.command(name="conventions")
def conventions_cmd(
    project: ProjectOpt = Path("demo_project"),
    verbose: VerboseOpt = False,
) -> None:
    """Check the project's written-down conventions, and say which should be tests.

    Reads `themis_conventions.yml` at the project root. Exits 1 if any entry was refused,
    because a convention someone wrote that silently does nothing is worse than an error.
    """
    from themis import conventions

    configure_logging(verbose=verbose)
    loaded = conventions.load(project)
    path = project / conventions.FILENAME

    if not loaded.conventions and not loaded.rejected:
        typer.echo(f"No conventions: {path} does not exist.")
        raise typer.Exit(code=0)

    typer.echo(f"{len(loaded.conventions)} convention(s) loaded from {path}")
    for convention in loaded.conventions:
        scope = []
        if convention.rules:
            scope.append("rules " + ", ".join(convention.rules))
        if convention.models:
            scope.append("models " + ", ".join(convention.models))
        typer.echo(f"  {convention.id}: {'; '.join(scope) or 'every finding'}")
        if conventions.checkable(convention):
            # The upgrade path. A statement about a key can be declared and measured; as
            # prose it can only be believed, and it cannot ground a verdict.
            typer.echo(
                "    reads as a claim about a key — declare it as a uniqueness test and "
                "THEMIS will read it as a declared grain and measure it with --execute"
            )

    for label, reason in loaded.rejected:
        typer.echo(f"  REFUSED {label}: {reason}", err=True)
    raise typer.Exit(code=1 if loaded.rejected else 0)
