"""Git plumbing: what changed, and what the project looked like before.

Everything here is read-only. THEMIS reads two revisions of a repository it does not
own, so it never checks out in place — the base revision is materialised into a
temporary worktree instead, leaving the user's working tree untouched.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from themis.logging import get_logger

log = get_logger(__name__)


class GitError(RuntimeError):
    """A git invocation failed in a way the caller cannot sensibly continue past."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@dataclass(frozen=True)
class ChangedFile:
    """One file the diff touched, classified by what it means for dbt."""

    path: str
    status: str  # A, M, D, R

    @property
    def is_model(self) -> bool:
        return self.path.endswith(".sql") and "/models/" in f"/{self.path}"

    @property
    def is_macro(self) -> bool:
        return self.path.endswith(".sql") and "/macros/" in f"/{self.path}"

    @property
    def is_schema_yml(self) -> bool:
        return self.path.endswith((".yml", ".yaml"))

    @property
    def is_seed(self) -> bool:
        return self.path.endswith(".csv")

    @property
    def model_name(self) -> str:
        return Path(self.path).stem


def resolve_revision(repo: Path, revision: str) -> str:
    """Resolve a revision to a full SHA, so a run is reproducible after the fact."""
    return _git(repo, "rev-parse", revision).strip()


def changed_files(repo: Path, base: str, head: str) -> tuple[ChangedFile, ...]:
    """Files differing between two revisions.

    Uses the merge base rather than a direct comparison: a long-lived branch would
    otherwise report every change that landed on main since it forked, burying the
    reviewer's actual change in unrelated noise.
    """
    merge_base = _git(repo, "merge-base", base, head).strip()
    raw = _git(repo, "diff", "--name-status", "--find-renames", merge_base, head)
    changes: list[ChangedFile] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][:1]
        # A rename reports both paths; the new one is what the reviewer is looking at.
        path = parts[-1]
        changes.append(ChangedFile(path=path, status=status))
    log.debug("git.changed_files", count=len(changes), merge_base=merge_base[:8])
    return tuple(changes)


@contextmanager
def worktree_at(repo: Path, revision: str) -> Iterator[Path]:
    """Materialise a revision into a throwaway worktree.

    The base revision has to be compiled to get its manifest, and compiling requires
    real files on disk. A detached worktree gives us that without touching the
    checkout the user is working in.
    """
    with tempfile.TemporaryDirectory(prefix="themis-worktree-") as tmp:
        path = Path(tmp) / "tree"
        _git(repo, "worktree", "add", "--detach", "--quiet", str(path), revision)
        try:
            log.debug("git.worktree.created", revision=revision[:8], path=str(path))
            yield path
        finally:
            # Prune rather than remove: the temp dir is already going away, and this
            # keeps git's worktree registry from accumulating stale entries.
            try:
                _git(repo, "worktree", "remove", "--force", str(path))
            except GitError:
                _git(repo, "worktree", "prune")


def repo_root(start: Path) -> Path:
    """The git repository containing a path."""
    return Path(_git(start, "rev-parse", "--show-toplevel").strip())


def is_clean(repo: Path, path: Path | None = None) -> bool:
    """Whether a path has no uncommitted changes.

    The question behind this is whether a checkout is *described* by its SHA. A dirty
    tree is not: two runs at the same revision can compile to different SQL, so
    anything keyed on the revision would serve one for the other.
    """
    args = ["status", "--porcelain"]
    if path is not None:
        args += ["--", str(path)]
    try:
        return not _git(repo, *args).strip()
    except GitError:
        # Cannot establish cleanliness, so it must not be assumed.
        return False
