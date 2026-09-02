"""Build the before/after ``ProjectSnapshot`` pair — Stage 0.

Backend selection is automatic and, importantly, honest about what it got. The three
backends are not interchangeable: against a macro-heavy project, raw-file analysis is
close to blind, so the builder prefers a compiled manifest and says plainly when it
had to fall back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from themis.acquire import git
from themis.acquire.dbt_runner import DbtError, compile_project
from themis.acquire.manifest import ManifestError, load_manifest
from themis.logging import get_logger
from themis.models import Backend
from themis.snapshot import ProjectSnapshot

log = get_logger(__name__)


@dataclass(frozen=True)
class AcquireResult:
    before: ProjectSnapshot
    after: ProjectSnapshot
    changed: tuple[git.ChangedFile, ...]
    # Set when the grounding is weaker than the analysis really wants. Surfaced in the
    # report rather than swallowed.
    degraded_reason: str | None = None

    @property
    def changed_models(self) -> tuple[str, ...]:
        return tuple(sorted({c.model_name for c in self.changed if c.is_model}))

    @property
    def changed_macros(self) -> tuple[str, ...]:
        """Stems of changed macro files. Kept for display; routing uses the paths."""
        return tuple(sorted({c.model_name for c in self.changed if c.is_macro}))

    @property
    def changed_schema_files(self) -> tuple[str, ...]:
        """Schema YAML files the change touched.

        Not cosmetic: in many projects the materialization, partitioning and hooks are
        declared here rather than in the model, so this is where a change of write
        behaviour actually appears.
        """
        return tuple(sorted({c.path for c in self.changed if c.is_schema_yml}))

    @property
    def changed_macro_files(self) -> tuple[str, ...]:
        """Paths of changed macro files.

        Routing must go through the path: one file defines several macros, and the
        filename identifies at most one of them.
        """
        return tuple(sorted({c.path for c in self.changed if c.is_macro}))


def _compile_snapshot(
    project_dir: Path,
    *,
    revision: str,
    target: str,
    allowed_targets: tuple[str, ...],
    timeout_s: float,
    anchor_dir: Path | None = None,
) -> ProjectSnapshot | None:
    """Compile a project revision into a snapshot, or None if it cannot be compiled.

    ``anchor_dir`` points relative database paths at the real project. Without it a
    base revision compiled in a worktree addresses an empty database beside itself,
    and any macro that queries at compile time fails.
    """
    import tempfile

    from themis.execute.profiles import ProfileError, write_anchored_profile

    try:
        profiles_dir: Path | None = None
        with tempfile.TemporaryDirectory(prefix="themis-compile-") as tmp:
            if anchor_dir is not None:
                try:
                    profiles_dir = write_anchored_profile(
                        project_dir, Path(tmp), target=target, anchor_dir=anchor_dir
                    )
                except ProfileError as exc:
                    log.warning("acquire.profile_unreadable", error=str(exc)[:200])
            manifest_path = compile_project(
                project_dir,
                target=target,
                allowed_targets=allowed_targets,
                timeout_s=timeout_s,
                profiles_dir=profiles_dir,
            )
            return load_manifest(manifest_path, revision=revision, backend=Backend.MANIFEST)
    except (DbtError, ManifestError) as exc:
        log.warning("acquire.compile_failed", revision=revision[:8], error=str(exc)[:400])
        return None


def acquire(
    project_dir: Path,
    *,
    base: str,
    head: str,
    target: str = "dev",
    allowed_targets: tuple[str, ...] = ("dev", "ci", "duckdb", "test", "local"),
    timeout_s: float = 900.0,
    prod_manifest: Path | None = None,
) -> AcquireResult:
    """Produce the snapshot pair for a review.

    The head revision is compiled from the working tree — that is what the reviewer is
    actually proposing. The base is reconstructed in a detached worktree so the user's
    checkout is never touched.
    """
    repo = git.repo_root(project_dir)
    base_sha = git.resolve_revision(repo, base)
    head_sha = git.resolve_revision(repo, head)
    changed = git.changed_files(repo, base, head)

    after = _compile_snapshot(
        project_dir,
        revision=head_sha,
        target=target,
        allowed_targets=allowed_targets,
        timeout_s=timeout_s,
    )

    # Backend A: a production manifest removes the need to rebuild the base at all.
    before: ProjectSnapshot | None = None
    if prod_manifest is not None and prod_manifest.exists():
        try:
            before = load_manifest(prod_manifest, revision=base_sha, backend=Backend.DUAL_MANIFEST)
        except ManifestError as exc:
            log.warning("acquire.prod_manifest_unusable", error=str(exc)[:300])

    if before is None:
        # Backend B: rebuild the base in a throwaway worktree.
        relative = project_dir.resolve().relative_to(repo.resolve())
        with git.worktree_at(repo, base_sha) as tree:
            before = _compile_snapshot(
                tree / relative,
                revision=base_sha,
                target=target,
                allowed_targets=allowed_targets,
                timeout_s=timeout_s,
                # Anchor to the real project so a compile-time query reaches the
                # actual database rather than an empty one in the worktree.
                anchor_dir=project_dir,
            )

    degraded: str | None = None
    if after is None:
        raise DbtError(
            "could not compile the head revision; THEMIS needs a compiled manifest to "
            "analyse macro-expanded SQL"
        )
    if before is None:
        # A new project, or a base that no longer compiles. Every model reads as new,
        # which is noisy but honest — better than silently comparing against nothing.
        before = ProjectSnapshot(revision=base_sha, backend=after.backend)
        degraded = "base revision could not be compiled; every model is treated as new"
    elif not after.has_compiled_sql:
        degraded = "manifest has no compiled SQL; most rules cannot run"

    log.info(
        "acquire.complete",
        base=base_sha[:8],
        head=head_sha[:8],
        changed_files=len(changed),
        backend=after.backend.value,
    )
    return AcquireResult(before=before, after=after, changed=changed, degraded_reason=degraded)
