"""The Typer application, and the options and error types every command shares."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from themis.logging import get_logger


def _review_errors() -> tuple[type[Exception], ...]:
    """Failures that mean a review could not start, as opposed to a bug in THEMIS.

    Reported as one line and exit code 2. A traceback for "that revision does not exist"
    reads as the tool crashing, and a CI log full of one hides the sentence that matters.
    """
    from themis.acquire.dbt_runner import DbtError, UnsafeTargetError
    from themis.acquire.git import GitError
    from themis.capabilities import CapabilityError

    return (GitError, DbtError, UnsafeTargetError, CapabilityError)


_REVIEW_ERRORS = _review_errors()

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
