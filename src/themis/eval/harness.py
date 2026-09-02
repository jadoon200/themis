"""Run the mutation corpus and score the reviewer against it.

The scoring rests on one decision: **execution decides the ground truth, not the
author.** Each mutation is applied, both revisions are built, and the results are
compared. If the row counts or the monetary totals move, the change was a real defect.
If they are identical, it was behaviour-preserving — whatever the mutation was *called*
when it was written.

That removes the circularity in the obvious alternative. A hand-labelled corpus
encodes the author's belief about what should be caught and then measures the tool
against that belief; both can be wrong together, and the numbers still look fine.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from themis.acquire import git
from themis.config import Settings
from themis.eval.mutations import Kind, Mutation
from themis.logging import get_logger
from themis.models import Confidence
from themis.pipeline import review as run_review

log = get_logger(__name__)


@dataclass
class MutationOutcome:
    """What happened for one mutation."""

    mutation: Mutation
    applied: bool
    # Ground truth, from execution rather than from the label on the mutation.
    changed_results: bool
    detected: bool
    families_fired: tuple[str, ...]
    expected_family_fired: bool
    finding_count: int
    measured_count: int
    row_delta: int | None = None
    largest_sum_shift_pct: float | None = None
    error: str | None = None

    @property
    def is_true_defect(self) -> bool:
        return self.changed_results

    @property
    def classification(self) -> str:
        """Confusion-matrix cell, using execution as truth."""
        if self.is_true_defect:
            return "true_positive" if self.detected else "false_negative"
        return "false_positive" if self.detected else "true_negative"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _largest_sum_shift(deltas: dict[str, object]) -> float | None:
    """The biggest relative move in any monetary total, as a percentage."""
    from themis.models import ExecutionDelta

    worst: float | None = None
    for delta in deltas.values():
        if not isinstance(delta, ExecutionDelta):
            continue
        for before, after in delta.sum_deltas.values():
            if before == 0:
                continue
            shift = abs((after - before) / before) * 100
            worst = shift if worst is None else max(worst, shift)
    return worst


class DirtyRepositoryError(RuntimeError):
    """The working tree has uncommitted changes.

    The harness commits and discards branches, so anything uncommitted is at risk. It
    is also meaningless to measure a reviewer against a tree that does not match any
    revision. Refusing is both the safe answer and the correct one.
    """


def assert_clean(repo: Path) -> None:
    """Refuse to run against a dirty working tree."""
    dirty = _git(repo, "status", "--porcelain").strip()
    if dirty:
        count = len(dirty.splitlines())
        raise DirtyRepositoryError(
            f"{count} uncommitted change(s) in {repo}. The eval harness creates and "
            "deletes branches, so commit or stash first — otherwise that work is at "
            "risk and the measurement does not correspond to any revision."
        )


def run_mutation(
    project_dir: Path,
    mutation: Mutation,
    *,
    settings: Settings,
    base_ref: str,
) -> MutationOutcome:
    """Apply one mutation on a scratch branch, review it, then restore the repo.

    The branch is always cleaned up, including on failure. Leaving a scratch branch
    behind would poison every later mutation in the run.
    """
    repo = git.repo_root(project_dir)
    branch = f"themis-eval/{mutation.id}"
    original = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()

    try:
        _git(repo, "checkout", "-q", "-B", branch, base_ref)

        if not mutation.apply(project_dir):
            return MutationOutcome(
                mutation=mutation,
                applied=False,
                changed_results=False,
                detected=False,
                families_fired=(),
                expected_family_fired=False,
                finding_count=0,
                measured_count=0,
                error="anchor text not found — the mutation is stale",
            )

        # Stage ONLY the mutated file. `git add -A` here would sweep every
        # uncommitted change in the repository into a scratch commit, and the branch
        # is deleted in the finally block below — destroying that work irrecoverably
        # except through the reflog. This harness did exactly that once.
        _git(repo, "add", "--", str((project_dir / mutation.relative_path).resolve()))
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"eval: {mutation.id}")

        result = run_review(
            project_dir,
            base=base_ref,
            head="HEAD",
            settings=settings,
            run_execution=True,
        )

        execution = result.execution
        changed = bool(
            execution and execution.ran and any(d.is_material for d in execution.deltas.values())
        )
        families = tuple(sorted({f.family for f in result.findings}))
        row_delta = None
        if execution and execution.ran:
            deltas = [d.row_delta for d in execution.deltas.values() if d.row_delta]
            row_delta = max(deltas, key=abs) if deltas else 0

        return MutationOutcome(
            mutation=mutation,
            applied=True,
            changed_results=changed,
            detected=bool(result.findings),
            families_fired=families,
            expected_family_fired=mutation.expects_family in families,
            finding_count=len(result.findings),
            measured_count=sum(1 for f in result.findings if f.confidence is Confidence.MEASURED),
            row_delta=row_delta,
            largest_sum_shift_pct=(
                _largest_sum_shift(dict(execution.deltas)) if execution and execution.ran else None
            ),
        )
    except Exception as exc:
        log.warning("eval.mutation_failed", mutation=mutation.id, error=str(exc)[:300])
        return MutationOutcome(
            mutation=mutation,
            applied=False,
            changed_results=False,
            detected=False,
            families_fired=(),
            expected_family_fired=False,
            finding_count=0,
            measured_count=0,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        # Restore unconditionally. A stranded scratch branch or a dirty tree would
        # silently corrupt every subsequent mutation in the run.
        try:
            _git(repo, "checkout", "-q", "--force", original)
            _git(repo, "branch", "-q", "-D", branch)
        except RuntimeError as exc:
            log.warning("eval.cleanup_failed", branch=branch, error=str(exc)[:200])


@dataclass
class EvalReport:
    outcomes: list[MutationOutcome]

    @property
    def usable(self) -> list[MutationOutcome]:
        """Mutations that applied. A stale one is excluded, and reported."""
        return [o for o in self.outcomes if o.applied and o.error is None]

    @property
    def scored(self) -> list[MutationOutcome]:
        """Mutations the execution oracle can judge.

        Latent defects are excluded. Scoring a cost, lineage or not-yet-triggered
        defect against "did the numbers move" counts a correct flag as a false
        positive, which would push the tool towards not reporting them at all.
        """
        return [o for o in self.usable if o.mutation.kind is not Kind.LATENT]

    @property
    def latent(self) -> list[MutationOutcome]:
        """Defects execution cannot see, scored on detection alone."""
        return [o for o in self.usable if o.mutation.kind is Kind.LATENT]

    @property
    def latent_detected(self) -> int:
        return sum(1 for o in self.latent if o.detected)

    @property
    def stale(self) -> list[MutationOutcome]:
        return [o for o in self.outcomes if not o.applied or o.error is not None]

    def counts(self) -> dict[str, int]:
        tally = {"true_positive": 0, "false_negative": 0, "false_positive": 0, "true_negative": 0}
        for outcome in self.scored:
            tally[outcome.classification] += 1
        return tally

    @property
    def recall(self) -> float | None:
        c = self.counts()
        total = c["true_positive"] + c["false_negative"]
        return c["true_positive"] / total if total else None

    @property
    def precision(self) -> float | None:
        c = self.counts()
        total = c["true_positive"] + c["false_positive"]
        return c["true_positive"] / total if total else None

    @property
    def false_positive_rate(self) -> float | None:
        """Share of behaviour-preserving changes that were flagged.

        The number that decides whether anyone leaves the tool switched on.
        """
        c = self.counts()
        total = c["false_positive"] + c["true_negative"]
        return c["false_positive"] / total if total else None

    @property
    def mislabelled(self) -> list[MutationOutcome]:
        """Mutations whose declared kind disagrees with what execution found.

        Worth surfacing rather than hiding: a 'defect' that changes nothing is a bad
        test, and a 'control' that moves the numbers is a bug in the control.
        """
        return [o for o in self.scored if (o.mutation.kind is Kind.DEFECT) != o.changed_results]


def run_corpus(
    project_dir: Path,
    mutations: tuple[Mutation, ...],
    *,
    settings: Settings,
    base_ref: str = "main",
    allow_dirty: bool = False,
) -> EvalReport:
    if not allow_dirty:
        assert_clean(git.repo_root(project_dir))
    outcomes: list[MutationOutcome] = []
    for index, mutation in enumerate(mutations, start=1):
        log.info("eval.mutation", n=f"{index}/{len(mutations)}", id=mutation.id)
        outcomes.append(run_mutation(project_dir, mutation, settings=settings, base_ref=base_ref))
    return EvalReport(outcomes=outcomes)
