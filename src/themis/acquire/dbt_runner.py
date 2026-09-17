"""Shell out to the dbt CLI.

Used by two stages for different ends: ACQUIRE needs ``dbt compile`` to get a manifest
with macro-expanded SQL, and EXECUTE needs ``dbt build`` to materialise both revisions
so their results can be compared. Both go through here so the target guard is
enforced in exactly one place.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from themis.execute.profiles import resolve_profiles_dir
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
    target_path: Path | None = None,
) -> DbtResult:
    """Invoke dbt in a project directory, with the target guard applied first.

    ``target_path`` sends dbt's artefacts somewhere other than the project's own
    ``target/``. Every caller that reads an artefact back should pass one: a manifest
    or ``run_results.json`` already sitting in ``target/`` from an earlier run is
    indistinguishable from the one this invocation was meant to write, so a run that
    failed before writing reads as though it succeeded.
    """
    assert_target_allowed(target, allowed_targets)

    # Absolute throughout. The subprocess runs with cwd set to the project, so a
    # relative --project-dir would resolve against itself and quietly address the
    # wrong directory — or, worse, an existing one.
    project_dir = project_dir.resolve()
    # Where dbt itself would find the profile — not the project, unless it is there.
    profiles = resolve_profiles_dir(project_dir, profiles_dir).resolve()
    artefacts = target_path.resolve() if target_path is not None else project_dir / "target"

    args = [
        dbt_executable(),
        *command,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(profiles),
        "--target",
        target,
    ]
    if target_path is not None:
        args += ["--target-path", str(artefacts)]
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
            # Run from the project directory. Relative paths in a profile — a DuckDB
            # file, a keyfile — resolve against the working directory, so invoking dbt
            # from elsewhere silently points it at a different database. That stayed
            # invisible until a macro used run_query at compile time: the query then
            # hit an empty database, dbt aborted, and *every* model lost its compiled
            # SQL while the manifest still looked valid.
            cwd=str(project_dir),
        )
    except subprocess.TimeoutExpired as exc:
        raise DbtError(f"dbt {' '.join(command)} timed out after {timeout_s}s") from exc
    except OSError as exc:
        raise DbtError(f"could not run dbt: {exc}") from exc

    manifest = artefacts / "manifest.json"
    result = DbtResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        manifest_path=manifest if manifest.exists() else None,
    )
    if not result.ok:
        log.warning("dbt.failed", command=" ".join(command), tail=proc.stdout[-2000:])
    return result


def extract_dbt_error(output: str) -> str:
    """Pull the actual error out of a dbt log.

    dbt writes a few hundred lines of progress around the one that matters, wrapped in
    ANSI colour. Surfacing the raw tail buries the cause in noise, and this text goes
    into a report a human is meant to read.
    """
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    lines = [line.strip() for line in clean.splitlines()]
    collected: list[str] = []
    capturing = False
    for line in lines:
        if "Error in model" in line or "Runtime Error" in line or "Compilation Error" in line:
            capturing = True
        if capturing and line:
            # Drop dbt's leading timestamps so the message reads as a message.
            collected.append(re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", line))
        if capturing and len(collected) >= 6:
            break
    return " ".join(collected).strip()


def node_statuses(target_dir: Path) -> dict[str, str]:
    """Per-node status from the ``run_results.json`` dbt left in a target directory.

    Keyed by node name, for models and seeds only. Empty when the file is missing or
    unreadable — the caller must then treat the whole build as its exit code says,
    because "no statuses" is not "everything succeeded".

    This is what separates a model that built from one whose relation merely exists.
    A table left by an earlier pass, or by an earlier run into the same schema, looks
    identical to a fresh one from the warehouse's side.
    """
    path = target_dir / "run_results.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    statuses: dict[str, str] = {}
    for result in payload.get("results") or []:
        unique_id = str(result.get("unique_id", ""))
        if not unique_id.startswith(("model.", "seed.")):
            continue
        statuses[unique_id.split(".")[-1]] = str(result.get("status", "")).lower()
    return statuses


def seed_partial_parse(source_project: Path, target_dir: Path) -> bool:
    """Copy dbt's parse cache into a target directory before a run uses it.

    dbt keeps its parsed project in ``partial_parse.msgpack`` under the target path and
    reparses only what changed when it finds one. A fresh worktree or a fresh
    ``--target-path`` never has one, so every such run reparses the project from cold.

    Failure is not an error. A missing cache costs a full parse, which is what would
    have happened anyway, and a stale one is dbt's to detect: it validates the cache
    against the project and falls back on its own.
    """
    source = source_project / "target" / "partial_parse.msgpack"
    if not source.exists():
        return False
    destination = target_dir / "partial_parse.msgpack"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    except OSError as exc:
        log.debug("dbt.partial_parse_not_seeded", error=str(exc)[:200])
        return False
    return True


@dataclass(frozen=True)
class CompileOutput:
    """A compiled manifest, and what dbt said if the compile did not fully succeed."""

    manifest_path: Path
    # dbt emits a manifest even when compilation aborts part-way, with compiled SQL for
    # whatever it reached first. Carried so the report can name the cause rather than
    # just observing that most checks could not run.
    error: str | None = None


def compile_project(
    project_dir: Path,
    *,
    target: str,
    allowed_targets: tuple[str, ...],
    profiles_dir: Path | None = None,
    timeout_s: float = 900.0,
    target_path: Path | None = None,
) -> CompileOutput:
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
        target_path=target_path,
    )
    if result.manifest_path is None:
        raise DbtError(
            "dbt compile produced no manifest.\n"
            f"stdout tail:\n{result.stdout[-2000:]}\n"
            f"stderr tail:\n{result.stderr[-1000:]}"
        )
    # A compile can fail on some models and still emit a manifest for the rest. That is
    # more useful than nothing, so it is not a hard stop — but it is not a success
    # either, and the error travels with the manifest so the report can say why.
    if not result.ok:
        error = extract_dbt_error(result.stdout) or "dbt compile failed"
        log.warning("dbt.compile.partial", error=error[:300])
        return CompileOutput(manifest_path=result.manifest_path, error=error)
    return CompileOutput(manifest_path=result.manifest_path)
