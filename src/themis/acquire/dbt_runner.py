"""Shell out to the dbt CLI.

Used by two stages for different ends: ACQUIRE needs ``dbt compile`` to get a manifest
with macro-expanded SQL, and EXECUTE needs ``dbt build`` to materialise both revisions
so their results can be compared. Both go through here so the target guard is
enforced in exactly one place.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from themis.logging import get_logger

log = get_logger(__name__)


class DbtError(RuntimeError):
    """A dbt invocation failed."""


class UnsafeTargetError(RuntimeError):
    """Refused to run against a target that is not clearly a development target.

    Deliberately an allowlist. A typo, a missing environment variable, or an inherited
    profile must fail closed — running a build against production is not a mistake
    this tool gets to make once.
    """


@dataclass(frozen=True)
class DbtResult:
    ok: bool
    stdout: str
    stderr: str
    manifest_path: Path | None = None


def dbt_executable() -> str:
    """Locate the dbt that matches the installed dbt-core.

    Preferring the interpreter's own bin directory over PATH is not just robustness:
    a different dbt on PATH could resolve a different adapter or profile, and the
    manifest THEMIS analyses would then not be the one the project actually builds.
    """
    candidate = Path(sys.executable).parent / "dbt"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("dbt")
    if found:
        return found
    raise DbtError(
        "dbt executable not found. Install it into this environment with "
        "`uv pip install dbt-core dbt-duckdb`."
    )


def assert_target_allowed(target: str, allowed: tuple[str, ...]) -> None:
    """Refuse anything outside the allowlist, before dbt is invoked."""
    if target not in allowed:
        raise UnsafeTargetError(
            f"refusing to run against dbt target {target!r}; "
            f"allowed targets are {', '.join(sorted(allowed))}. "
            "Set THEMIS_EXECUTE_ALLOWED_TARGETS only if you are certain this is not production."
        )


def run_dbt(
    project_dir: Path,
    command: list[str],
    *,
    target: str,
    allowed_targets: tuple[str, ...],
    profiles_dir: Path | None = None,
    timeout_s: float = 900.0,
    env_overrides: dict[str, str] | None = None,
) -> DbtResult:
    """Invoke dbt in a project directory, with the target guard applied first."""
    assert_target_allowed(target, allowed_targets)

    args = [
        dbt_executable(),
        *command,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(profiles_dir or project_dir),
        "--target",
        target,
    ]
    env = {**os.environ, **(env_overrides or {})}
    log.debug("dbt.run", command=" ".join(command), project=str(project_dir), target=target)
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise DbtError(f"dbt {' '.join(command)} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise DbtError(f"could not run dbt: {exc}") from exc

    manifest = project_dir / "target" / "manifest.json"
    result = DbtResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        manifest_path=manifest if manifest.exists() else None,
    )
    if not result.ok:
        log.warning("dbt.failed", command=" ".join(command), tail=proc.stdout[-2000:])
    return result


def compile_project(
    project_dir: Path,
    *,
    target: str,
    allowed_targets: tuple[str, ...],
    profiles_dir: Path | None = None,
    timeout_s: float = 900.0,
) -> Path:
    """Compile a project and return its manifest path.

    ``dbt compile`` rather than ``dbt parse`` on purpose: only compile expands macros
    into the ``compiled_code`` the analysis stages actually read.
    """
    result = run_dbt(
        project_dir,
        ["compile"],
        target=target,
        allowed_targets=allowed_targets,
        profiles_dir=profiles_dir,
        timeout_s=timeout_s,
    )
    if result.manifest_path is None:
        raise DbtError(
            "dbt compile produced no manifest.\n"
            f"stdout tail:\n{result.stdout[-2000:]}\n"
            f"stderr tail:\n{result.stderr[-1000:]}"
        )
    # A compile can fail on some models and still emit a manifest for the rest. That is
    # more useful than nothing, so it is a warning rather than a hard stop.
    if not result.ok:
        log.warning("dbt.compile.partial", hint="manifest emitted despite compile errors")
    return result.manifest_path
