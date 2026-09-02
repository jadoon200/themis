"""Generate a throwaway dbt profile that redirects a build into a chosen schema.

Both revisions must be built against **the same source data**, differing only in
where their output lands. Otherwise the comparison measures two different datasets and
every delta is noise.

Two adjustments make that true:

- ``schema`` is overridden, so base and head materialise side by side.
- For file-backed adapters the database path is made absolute. The base revision is
  built inside a temporary git worktree, and a relative path there would silently
  resolve to a fresh, empty database in the worktree — producing a confident diff
  against nothing at all.

The user's own ``profiles.yml`` is never modified.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from themis.logging import get_logger

log = get_logger(__name__)


class ProfileError(RuntimeError):
    """The project's profile could not be read or does not contain the target."""


def _profiles_path(project_dir: Path, profiles_dir: Path | None) -> Path:
    for candidate in (
        (profiles_dir / "profiles.yml") if profiles_dir else None,
        project_dir / "profiles.yml",
        Path.home() / ".dbt" / "profiles.yml",
    ):
        if candidate is not None and candidate.exists():
            return candidate
    raise ProfileError(
        f"no profiles.yml found for {project_dir} (looked in the project and ~/.dbt)"
    )


def project_profile_name(project_dir: Path) -> str:
    """The profile name ``dbt_project.yml`` expects.

    The generated profile must be written under this exact name — dbt looks the
    project's declared profile up by name, so anything else is simply not found.
    """
    project_yml = project_dir / "dbt_project.yml"
    if not project_yml.exists():
        raise ProfileError(f"no dbt_project.yml in {project_dir}")
    name = str((yaml.safe_load(project_yml.read_text()) or {}).get("profile", ""))
    if not name:
        raise ProfileError(f"dbt_project.yml in {project_dir} declares no profile")
    return name


def read_profile(
    project_dir: Path, *, target: str, profiles_dir: Path | None = None
) -> dict[str, Any]:
    """The resolved output block for one target."""
    path = _profiles_path(project_dir, profiles_dir)
    document: dict[str, Any] = yaml.safe_load(path.read_text()) or {}

    try:
        profile_name = project_profile_name(project_dir)
    except ProfileError:
        profile_name = ""

    block = document.get(profile_name) if profile_name else None
    if block is None:
        # Fall back to the sole profile in the file; a single-profile project is the
        # common case and failing on a name mismatch would be needlessly brittle.
        candidates = [v for k, v in document.items() if isinstance(v, dict) and "outputs" in v]
        if len(candidates) != 1:
            raise ProfileError(f"could not resolve profile {profile_name!r} in {path}")
        block = candidates[0]

    outputs = block.get("outputs") or {}
    if target not in outputs:
        raise ProfileError(
            f"target {target!r} not in profile; available: {', '.join(sorted(outputs))}"
        )
    resolved: dict[str, Any] = dict(outputs[target])
    return resolved


def write_anchored_profile(
    project_dir: Path,
    destination: Path,
    *,
    target: str,
    anchor_dir: Path,
    profiles_dir: Path | None = None,
) -> Path:
    """Write a profile identical to the project's, but with paths anchored elsewhere.

    Compiling a base revision happens inside a temporary worktree, where a relative
    database path resolves to an empty database beside the checkout. That is harmless
    until a macro queries at compile time — then the query fails, dbt aborts, and every
    model in the project loses its compiled SQL while the manifest still looks valid.
    """
    output = dict(read_profile(project_dir, target=target, profiles_dir=profiles_dir))
    raw_path = output.get("path")
    if isinstance(raw_path, str) and raw_path and raw_path != ":memory:":
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            output["path"] = str((anchor_dir / candidate).resolve())

    name = project_profile_name(project_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "profiles.yml").write_text(
        yaml.safe_dump({name: {"target": target, "outputs": {target: output}}}, sort_keys=False)
    )
    return destination


def write_profile_for_schema(
    project_dir: Path,
    destination: Path,
    *,
    target: str,
    schema: str,
    anchor_dir: Path,
    profiles_dir: Path | None = None,
) -> Path:
    """Write a profiles.yml that builds into ``schema``. Returns its directory.

    ``anchor_dir`` is where a relative database path resolves against, and it is
    deliberately separate from ``project_dir``. The base revision is built inside a
    temporary worktree, so resolving its path against the directory being built would
    point at a fresh database inside that worktree — which is then deleted, leaving
    nothing to compare against. Both revisions must anchor to the *original* project.
    """
    output = dict(read_profile(project_dir, target=target, profiles_dir=profiles_dir))
    output["schema"] = schema

    raw_path = output.get("path")
    if isinstance(raw_path, str) and raw_path and raw_path != ":memory:":
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            output["path"] = str((anchor_dir / candidate).resolve())

    name = project_profile_name(project_dir)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "profiles.yml").write_text(
        yaml.safe_dump(
            {name: {"target": target, "outputs": {target: output}}},
            sort_keys=False,
        )
    )
    log.debug("profiles.written", profile=name, schema=schema, dir=str(destination))
    return destination
