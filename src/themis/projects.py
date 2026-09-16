"""Which dbt projects a queued review may name.

A review runs ``dbt compile`` and, with execution, ``dbt build`` on the project path it
is given — and dbt runs whatever the project's macros and hooks say. A path taken
verbatim from an HTTP request is therefore a request to run someone else's code with
the worker's credentials. The API is loopback-only by default, which is the main
defence; this is the second one, and it applies wherever the request came from.
"""

from __future__ import annotations

from pathlib import Path, PurePath

from themis.config import Settings


class ProjectNotAllowedError(ValueError):
    """A project path outside what this deployment is configured to review."""


def validate_project_ref(ref: str, settings: Settings) -> str:
    """Check a project reference textually, without touching the filesystem.

    The API may not share a filesystem with the workers, so it can only judge the shape
    of the path: never a parent-directory escape, and absolute only under a configured
    root. The worker re-checks against the real filesystem before running anything.
    """
    if not ref or "\x00" in ref:
        raise ProjectNotAllowedError("project path is empty or malformed")
    path = PurePath(ref)
    if ".." in path.parts:
        raise ProjectNotAllowedError(f"project path {ref!r} may not contain '..'")
    if path.is_absolute():
        roots = [PurePath(root) for root in settings.project_roots]
        if not any(path == root or root in path.parents for root in roots):
            raise ProjectNotAllowedError(
                f"absolute project path {ref!r} is outside THEMIS_PROJECT_ROOTS"
                + ("" if roots else " (none configured, so only relative paths are accepted)")
            )
    return ref


def resolve_project(ref: str, settings: Settings) -> Path:
    """Resolve a project reference on the worker, refusing anything outside the roots.

    Relative paths resolve against the worker's working directory, which counts as a
    root. Resolution follows symlinks, so a link pointing out of a root is refused too.
    """
    validate_project_ref(ref, settings)
    resolved = Path(ref).resolve()
    roots = [Path(root).resolve() for root in settings.project_roots] or [Path.cwd().resolve()]
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ProjectNotAllowedError(
            f"project {ref!r} resolves to {resolved}, outside the configured roots"
        )
    return resolved
