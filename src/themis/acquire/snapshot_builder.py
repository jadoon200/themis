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
from themis.acquire.cache import CacheKey, ManifestCache
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
    cache: ManifestCache | None = None,
    cache_key: CacheKey | None = None,
) -> ProjectSnapshot | None:
    """Compile a project revision into a snapshot, or None if it cannot be compiled.

    ``anchor_dir`` points relative database paths at the real project. Without it a
    base revision compiled in a worktree addresses an empty database beside itself,
    and any macro that queries at compile time fails.

    ``cache`` short-circuits the compile when this exact revision has been compiled
    before. Only callers that can honestly name the revision pass one — a working tree
    with uncommitted edits is described by no SHA, so it has no key.
    """
    import tempfile

    from themis.execute.profiles import ProfileError, write_anchored_profile

    if cache is not None and cache_key is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            try:
                return load_manifest(cached, revision=revision, backend=Backend.MANIFEST)
            except ManifestError as exc:
                # A cached manifest that will not load is a cache problem, not a
                # project problem. Fall through and compile it properly.
                log.warning("acquire.cached_manifest_unusable", error=str(exc)[:200])

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
            snapshot = load_manifest(manifest_path, revision=revision, backend=Backend.MANIFEST)
            if cache is not None and cache_key is not None:
                cache.put(cache_key, manifest_path, snapshot)
            return snapshot
    except (DbtError, ManifestError) as exc:
        log.warning("acquire.compile_failed", revision=revision[:8], error=str(exc)[:400])
        return None


def seed_partial_parse(source_project: Path, worktree_project: Path) -> bool:
    """Copy dbt's parse cache into a fresh worktree before compiling it.

    dbt keeps its parsed project in ``target/partial_parse.msgpack`` and, when it finds
    one, reparses only the files that changed since. A detached worktree never has one,
    which is why dbt's own documentation notes that partial parsing does not help a new
    branch or pull request — every base compile reparses the entire project from cold.

    Handing it the cache from the working copy fixes that: same project, a handful of
    files different, so dbt reparses those and reuses the rest. It is the only saving
    available to a project whose manifest cannot be cached at all — parsing is
    unaffected by compile-time queries, because it happens before any of them run.

    Failure is not an error. A missing or unreadable cache costs a full parse, which is
    what would have happened anyway, and a corrupt one is dbt's to detect: it validates
    the cache against the project and falls back on its own.
    """
    source = source_project / "target" / "partial_parse.msgpack"
    if not source.exists():
        return False
    destination = worktree_project / "target" / "partial_parse.msgpack"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
    except OSError as exc:
        log.debug("acquire.partial_parse_not_seeded", error=str(exc)[:200])
        return False
    log.debug("acquire.partial_parse_seeded", path=str(destination))
    return True


def manifest_file(given: Path) -> Path:
    """Resolve a manifest reference that may name the file or its directory.

    dbt's own `--state` takes a directory, so a caller holding one set of production
    artifacts should be able to pass the same path to both `--prod-manifest` and
    `--defer-state` and have each take what it needs.
    """
    return given / "manifest.json" if given.is_dir() else given


def warm_cache(
    project_dir: Path,
    *,
    revision: str,
    target: str = "dev",
    allowed_targets: tuple[str, ...] = ("dev", "ci", "duckdb", "test", "local"),
    timeout_s: float = 900.0,
    cache_dir: Path | None = None,
) -> tuple[bool, str]:
    """Compile a revision into the cache ahead of time.

    The base compile is the one cost every review of a branch pays, and it is the same
    work every time. A cheap scheduled job that warms `main` whenever it moves means no
    reviewer ever waits for it — which is the difference between a compile budget spent
    once a day and one spent once a pull request.

    Returns whether the revision is now cached, and why not when it is not.
    """
    repo = git.repo_root(project_dir)
    sha = git.resolve_revision(repo, revision)
    relative = project_dir.resolve().relative_to(repo.resolve())
    cache = ManifestCache(cache_dir or repo / ".themis")
    key = CacheKey(revision=sha, target=target, project=str(relative))

    if cache.contains(key):
        return True, f"already cached ({sha[:12]})"

    with git.worktree_at(repo, sha) as tree:
        snapshot = _compile_snapshot(
            tree / relative,
            revision=sha,
            target=target,
            allowed_targets=allowed_targets,
            timeout_s=timeout_s,
            anchor_dir=project_dir,
            cache=cache,
            cache_key=key,
        )
    if snapshot is None:
        return False, f"{sha[:12]} could not be compiled"
    if not cache.contains(key):
        return False, (
            f"{sha[:12]} compiled but was not cached — this project builds SQL from "
            "query results, so a revision does not determine the manifest"
        )
    return True, f"cached {sha[:12]}"


def acquire(
    project_dir: Path,
    *,
    base: str,
    head: str,
    target: str = "dev",
    allowed_targets: tuple[str, ...] = ("dev", "ci", "duckdb", "test", "local"),
    timeout_s: float = 900.0,
    prod_manifest: Path | None = None,
    data_anchor: Path | None = None,
    cache_dir: Path | None = None,
    use_cache: bool = True,
) -> AcquireResult:
    """Produce the snapshot pair for a review.

    The head revision is compiled from the working tree — that is what the reviewer is
    actually proposing. The base is reconstructed in a detached worktree so the user's
    checkout is never touched.

    ``data_anchor`` separates *where the code is* from *where the data is*. A caller
    reviewing a copy of the project — the eval harness works this way — has code in a
    throwaway directory and data only in the original. Without the distinction, any
    macro that queries at compile time reads an empty database, dbt aborts, and every
    model silently loses its compiled SQL.
    """
    repo = git.repo_root(project_dir)
    base_sha = git.resolve_revision(repo, base)
    head_sha = git.resolve_revision(repo, head)
    changed = git.changed_files(repo, base, head)
    relative = project_dir.resolve().relative_to(repo.resolve())

    cache = ManifestCache(cache_dir or repo / ".themis", enabled=use_cache)

    def key_for(revision: str) -> CacheKey:
        return CacheKey(revision=revision, target=target, project=str(relative))

    # The head is normally the working tree, and a working tree with uncommitted edits
    # is not described by its SHA — caching it would serve one reviewer's unsaved work
    # to the next run of that revision. Only a clean checkout gets a key.
    head_key = key_for(head_sha) if git.is_clean(repo, project_dir) else None
    after = _compile_snapshot(
        project_dir,
        revision=head_sha,
        target=target,
        allowed_targets=allowed_targets,
        timeout_s=timeout_s,
        anchor_dir=data_anchor,
        cache=cache,
        cache_key=head_key,
    )

    # Backend A: a production manifest removes the need to rebuild the base at all.
    before: ProjectSnapshot | None = None
    prod_backend_failed: str | None = None
    if prod_manifest is not None:
        path = manifest_file(prod_manifest)
        if not path.exists():
            # Asked for and not delivered. Falling through quietly would mean the
            # review silently compares against a different revision than the caller
            # believes, which is worse than either backend on its own.
            prod_backend_failed = f"no manifest at {path}"
        else:
            try:
                before = load_manifest(path, revision=base_sha, backend=Backend.DUAL_MANIFEST)
            except ManifestError as exc:
                prod_backend_failed = str(exc)
        if prod_backend_failed:
            log.warning("acquire.prod_manifest_unusable", error=prod_backend_failed[:300])

    if before is None:
        # Backend B: rebuild the base in a throwaway worktree. This is the compile the
        # cache exists for — a detached worktree at a SHA is exactly the content the
        # SHA names, and the base rarely moves between reviews of the same branch.
        base_key = key_for(base_sha)
        cached_base = cache.get(base_key)
        if cached_base is not None:
            try:
                before = load_manifest(cached_base, revision=base_sha, backend=Backend.MANIFEST)
            except ManifestError as exc:
                log.warning("acquire.cached_manifest_unusable", error=str(exc)[:200])
        if before is None:
            with git.worktree_at(repo, base_sha) as tree:
                # Only worth doing for a project the manifest cache refuses; for any
                # other the compile is skipped entirely on the second review.
                seed_partial_parse(project_dir, tree / relative)
                before = _compile_snapshot(
                    tree / relative,
                    revision=base_sha,
                    target=target,
                    allowed_targets=allowed_targets,
                    timeout_s=timeout_s,
                    # Anchor to the real project so a compile-time query reaches the
                    # actual database rather than an empty one in the worktree.
                    anchor_dir=data_anchor or project_dir,
                    cache=cache,
                    cache_key=base_key,
                )

    if after is None:
        raise DbtError(
            "could not compile the head revision; THEMIS needs a compiled manifest to "
            "analyse macro-expanded SQL"
        )

    # Every reason the grounding is weaker than asked for, not just the last one — a
    # report that names one degradation reads as though the rest did not happen.
    reasons: list[str] = []
    if prod_backend_failed:
        reasons.append(
            f"a production manifest was given but could not be used ({prod_backend_failed}); "
            "the base was rebuilt from git instead"
        )
    if before is None:
        # A new project, or a base that no longer compiles. Every model reads as new,
        # which is noisy but honest — better than silently comparing against nothing.
        before = ProjectSnapshot(revision=base_sha, backend=after.backend)
        reasons.append("base revision could not be compiled; every model is treated as new")
    elif not after.has_compiled_sql:
        reasons.append("manifest has no compiled SQL; most rules cannot run")
    degraded = "; ".join(reasons) or None

    log.info(
        "acquire.complete",
        base=base_sha[:8],
        head=head_sha[:8],
        changed_files=len(changed),
        # Both, because only the base varies. The head is always compiled from the
        # working tree, so logging its backend alone reports "manifest" whichever
        # grounding the base actually got — including the one the caller asked for.
        head_backend=after.backend.value,
        base_backend=before.backend.value,
    )
    return AcquireResult(before=before, after=after, changed=changed, degraded_reason=degraded)
