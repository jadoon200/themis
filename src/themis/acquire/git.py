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


def validate_revision(revision: str) -> str:
    """Refuse a revision string git would read as an option.

    Revisions arrive from the API as well as from a person at a terminal. One beginning
    with ``-`` is parsed by git as a flag — ``git diff`` accepts ``--output=<file>`` —
    so it is rejected before it reaches any command rather than escaped at each one.
    """
    if not revision or revision.startswith("-") or any(c in revision for c in "\x00\n\r"):
        raise GitError(f"not a usable revision: {revision!r}")
    return revision


def resolve_revision(repo: Path, revision: str) -> str:
    """Resolve a revision to a full commit SHA, so a run is reproducible after the fact."""
    validate_revision(revision)
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}").strip()
    except GitError as exc:
        # `--quiet` leaves stderr empty, so the default message would name nothing.
        raise GitError(f"{revision!r} does not name a commit in {repo}") from exc


def is_working_tree(repo: Path, revision: str, path: Path | None = None) -> bool:
    """Whether a revision names what is on disk, so the working tree can stand in for it.

    ``HEAD`` always does: reviewing the working tree against a base is what someone at a
    terminal means by it, uncommitted edits included. Any other name does only when it
    resolves to the checked-out commit *and* nothing is modified — otherwise the files on
    disk are not that revision, and compiling them in its place reviews the wrong code
    under the right SHA.
    """
    if revision == "HEAD":
        return True
    return resolve_revision(repo, revision) == resolve_revision(repo, "HEAD") and is_clean(
        repo, path
    )


def changed_files(
    repo: Path, base: str, head: str, *, working_tree: bool = False
) -> tuple[ChangedFile, ...]:
    """Files differing between two revisions.

    Uses the merge base rather than a direct comparison: a long-lived branch would
    otherwise report every change that landed on main since it forked, burying the
    reviewer's actual change in unrelated noise.

    ``working_tree`` compares against the files on disk rather than ``head``'s commit,
    untracked files included. It must match what gets compiled: comparing commits while
    compiling the working tree left uncommitted edits compiled into the snapshot and
    absent from the change set, so they were never reviewed.
    """
    merge_base = _git(repo, "merge-base", base, head).strip()
    if working_tree:
        raw = _git(repo, "diff", "--name-status", "--find-renames", merge_base)
    else:
        raw = _git(repo, "diff", "--name-status", "--find-renames", merge_base, head)
    changes: list[ChangedFile] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0][:1]
        # A rename reports both paths; the new one is what the reviewer is looking at.
        path = parts[-1]
        changes.append(ChangedFile(path=path, status=status))
        seen.add(path)
    if working_tree:
        # A new model not yet added to git is part of the working tree too.
        for path in _git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
            if path.strip() and path not in seen:
                changes.append(ChangedFile(path=path.strip(), status="A"))
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


def show_file(repo: Path, revision: str, path: str) -> str:
    """One file's contents at a revision, without checking anything out."""
    return _git(repo, "show", f"{validate_revision(revision)}:{path}")


def first_parent_changes(
    repo: Path, revision: str, path: str, *, limit: int
) -> list[tuple[str, str, str]]:
    """(commit, first parent, subject) for the last changes on a revision's first-parent
    history that touched `path`, newest first. The root commit, having no parent, is left
    out. The revision is validated like any other: it must never be read as an option."""
    validate_revision(revision)
    output = _git(
        repo,
        "log",
        "--first-parent",
        f"-n{max(limit, 0)}",
        "--format=%H%x1f%P%x1f%s",
        revision,
        "--",
        path,
    )
    changes: list[tuple[str, str, str]] = []
    for line in output.splitlines():
        commit, parents, subject = [*line.split("\x1f"), "", ""][:3]
        first = parents.split()[0] if parents.split() else ""
        if first:
            changes.append((commit, first, subject))
    return changes


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
