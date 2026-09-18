"""Build the before/after ``ProjectSnapshot`` pair — Stage 0.

Backend selection is automatic and, importantly, honest about what it got. The three
backends are not interchangeable: against a macro-heavy project, raw-file analysis is
close to blind, so the builder prefers a compiled manifest and says plainly when it
had to fall back.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from themis.acquire import git
from themis.acquire.cache import CacheKey, ManifestCache
from themis.acquire.dbt_runner import DbtError, compile_project, seed_partial_parse
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

    def _nodes_for(self, change: git.ChangedFile) -> list[str]:
        """Model or seed names a changed file defines, in either revision."""
        names: list[str] = []
        for snapshot in (self.after, self.before):
            node = snapshot.node_for_file(change.path)
            if node is not None and node.name not in names:
                names.append(node.name)
        return names

    def _is_seed(self, name: str) -> bool:
        node = self.after.models.get(name) or self.before.models.get(name)
        return node is not None and node.is_seed

    @property
    def changed_models(self) -> tuple[str, ...]:
        """SQL models whose own file changed.

        Resolved through the manifest, so a project whose ``model-paths`` is not
        ``models/`` is still reviewed. The folder convention is only a fallback for a file
        neither revision's manifest knows — which a compile failure can produce.
        """
        names: set[str] = set()
        for change in self.changed:
            nodes = self._nodes_for(change)
            if nodes:
                names.update(n for n in nodes if not self._is_seed(n))
            elif change.is_model:
                names.add(change.model_name)
        return tuple(sorted(names))

    @property
    def unanalysed_changes(self) -> tuple[str, ...]:
        """Changed dbt files this review did not look at, by path.

        A snapshot is the case that found this. `resource_type` is filtered to models and
        seeds at load, and a snapshot lives in `snapshots/`, so a change to one resolved to
        no node, matched no folder fallback, and fell out of the review without a word — a
        pull request that only touches snapshots would be reported as "No findings". In a
        bank a snapshot is slowly-changing reference data, which is exactly the kind of
        change somebody wants read.

        Analysing them is a feature with its own semantics and is not built. Saying that a
        change was not analysed costs nothing and is the difference between a blind spot
        and a silent one, so these are reported as skipped checks and make a review
        incomplete for the merge gate.
        """
        out: list[str] = []
        for change in self.changed:
            if change.status == "D":
                continue
            if not change.path.endswith(".sql"):
                continue
            if self._nodes_for(change) or change.is_model or change.is_macro:
                continue
            out.append(change.path)
        return tuple(sorted(out))

    @property
    def changed_seeds(self) -> tuple[str, ...]:
        """Seeds whose CSV changed.

        In a financial project these are reference data — FX rates, account mappings,
        cost-centre hierarchies — and editing one moves every figure built on it while
        changing no SQL at all. No rule can see that; execution can.
        """
        names: set[str] = set()
        for change in self.changed:
            nodes = self._nodes_for(change)
            if nodes:
                names.update(n for n in nodes if self._is_seed(n))
            elif change.is_seed:
                names.add(change.model_name)
        return tuple(sorted(names))

    @property
    def changed_macros(self) -> tuple[str, ...]:
        """Stems of changed macro files. Kept for display; routing uses the paths."""
        return tuple(sorted({Path(path).stem for path in self.changed_macro_files}))

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
        """Paths of changed files that define macros, in either revision.

        Routing must go through the path: one file defines several macros, and the
        filename identifies at most one of them. And it goes through the manifest rather
        than the ``macros/`` folder, for the same reason models do.
        """
        paths: set[str] = set()
        for change in self.changed:
            if not change.path.endswith(".sql"):
                continue
            defines_macros = bool(
                self.after.macros_in_file(change.path) or self.before.macros_in_file(change.path)
            )
            # The folder convention only as a fallback, for a file neither manifest knows.
            if defines_macros or (change.is_macro and not self._nodes_for(change)):
                paths.add(change.path)
        return tuple(sorted(paths))

    @property
    def changed_project_config(self) -> bool:
        """Whether ``dbt_project.yml`` itself changed.

        Folder-level configs and vars live there, so one line can re-materialize a whole
        directory of models without touching any of their files.
        """
        return any(Path(c.path).name == "dbt_project.yml" for c in self.changed)


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
    parse_cache_from: Path | None = None,
) -> ProjectSnapshot | None:
    """Compile a project revision into a snapshot, or None if it cannot be compiled.

    ``anchor_dir`` points relative database paths at the real project. Without it a
    base revision compiled in a worktree addresses an empty database beside itself,
    and any macro that queries at compile time fails.

    ``cache`` short-circuits the compile when this exact revision has been compiled
    before. Only callers that can honestly name the revision pass one — a working tree
    with uncommitted edits is described by no SHA, so it has no key.

    dbt writes into a directory this call owns rather than the project's ``target/``. A
    compile that dies before writing a manifest would otherwise hand back whichever
    manifest the previous compile left there, under this revision's name.
    """
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
            target_dir = Path(tmp) / "target"
            seed_partial_parse(parse_cache_from or project_dir, target_dir)
            compiled = compile_project(
                project_dir,
                target=target,
                allowed_targets=allowed_targets,
                timeout_s=timeout_s,
                profiles_dir=profiles_dir,
                target_path=target_dir,
            )
            snapshot = load_manifest(
                compiled.manifest_path, revision=revision, backend=Backend.MANIFEST
            )
            if compiled.error is not None:
                # Never cached: a partial compile is a fact about one attempt, not about
                # the revision, and serving it again would repeat the gap on every review.
                return snapshot.model_copy(update={"compile_error": compiled.error})
            if cache is not None and cache_key is not None:
                cache.put(cache_key, compiled.manifest_path, snapshot)
            return snapshot
    except (DbtError, ManifestError) as exc:
        log.warning("acquire.compile_failed", revision=revision[:8], error=str(exc)[:400])
        return None


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
            parse_cache_from=project_dir,
        )
    if snapshot is None:
        return False, f"{sha[:12]} could not be compiled"
    if snapshot.compile_error is not None:
        return (
            False,
            f"{sha[:12]} compiled only partly, so it was not cached: {snapshot.compile_error}",
        )
    if not cache.contains(key):
        return False, (
            f"{sha[:12]} compiled but was not cached — this project builds SQL from "
            "query results, so a revision does not determine the manifest"
        )
    return True, f"cached {sha[:12]}"


def _partial_compile_reason(snapshot: ProjectSnapshot, label: str) -> str | None:
    """Say how much of a revision has no compiled SQL, and why, when some of it does not."""
    missing = snapshot.models_without_compiled_sql
    total = sum(1 for m in snapshot.models.values() if not m.is_seed)
    if not missing:
        return None
    cause = f" (dbt: {snapshot.compile_error[:300]})" if snapshot.compile_error else ""
    if len(missing) == total:
        return f"the {label} manifest has no compiled SQL; most rules cannot run{cause}"
    shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
    return (
        f"{len(missing)} of {total} models in the {label} revision have no compiled SQL "
        f"({shown}), so checks on them could not run{cause}"
    )


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

    The head is compiled from the working tree when the working tree *is* the head —
    ``HEAD``, or a name for the checked-out commit with nothing modified. Any other head
    is compiled from a worktree at that commit. Compiling the working tree regardless
    reviewed whatever was checked out under the SHA of whatever was asked for, and a
    review of a real fan-out came back clean.

    The base is always reconstructed in a detached worktree so the user's checkout is
    never touched.

    ``data_anchor`` separates *where the code is* from *where the data is*. A caller
    reviewing a copy of the project — the eval harness works this way — has code in a
    throwaway directory and data only in the original. Without the distinction, any
    macro that queries at compile time reads an empty database, dbt aborts, and every
    model silently loses its compiled SQL.
    """
    repo = git.repo_root(project_dir)
    base_sha = git.resolve_revision(repo, base)
    head_sha = git.resolve_revision(repo, head)
    head_in_place = git.is_working_tree(repo, head, project_dir)
    changed = git.changed_files(repo, base_sha, head_sha, working_tree=head_in_place)
    relative = project_dir.resolve().relative_to(repo.resolve())

    cache = ManifestCache(cache_dir or repo / ".themis", enabled=use_cache)

    def key_for(revision: str) -> CacheKey:
        return CacheKey(revision=revision, target=target, project=str(relative))

    after: ProjectSnapshot | None
    if head_in_place:
        # A working tree with uncommitted edits is not described by its SHA — caching it
        # would serve one reviewer's unsaved work to the next run of that revision. Only
        # a clean checkout gets a key.
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
    else:
        with git.worktree_at(repo, head_sha) as tree:
            after = _compile_snapshot(
                tree / relative,
                revision=head_sha,
                target=target,
                allowed_targets=allowed_targets,
                timeout_s=timeout_s,
                anchor_dir=data_anchor or project_dir,
                cache=cache,
                cache_key=key_for(head_sha),
                parse_cache_from=project_dir,
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
                    parse_cache_from=project_dir,
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
    head_gap = _partial_compile_reason(after, "head")
    if head_gap:
        reasons.append(head_gap)
    if before is None:
        # A new project, or a base that no longer compiles. Every model reads as new,
        # which is noisy but honest — better than silently comparing against nothing.
        before = ProjectSnapshot(revision=base_sha, backend=after.backend)
        reasons.append("base revision could not be compiled; every model is treated as new")
    else:
        # The base matters as much as the head. A model whose base has no SQL looks new
        # to every rule, so every join in it reads as added.
        base_gap = _partial_compile_reason(before, "base")
        if base_gap:
            reasons.append(base_gap)
    degraded = "; ".join(reasons) or None

    log.info(
        "acquire.complete",
        base=base_sha[:8],
        head=head_sha[:8],
        head_from="working tree" if head_in_place else "worktree",
        changed_files=len(changed),
        # Both, because only the base can come from a production manifest; logging the
        # head's backend alone would report "manifest" whichever grounding the base got.
        head_backend=after.backend.value,
        base_backend=before.backend.value,
    )
    return AcquireResult(before=before, after=after, changed=changed, degraded_reason=degraded)
