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

import contextlib
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from themis.acquire import git
from themis.config import Settings
from themis.eval.mutations import Kind, Mutation
from themis.logging import get_logger
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
    # Individual rule ids, not just families. A family can look well covered while
    # three of its rules have never fired on a real case -- which has happened here,
    # to rules whose unit tests all passed.
    rules_fired: tuple[str, ...] = ()
    row_delta: int | None = None
    error: str | None = None
    # Populated only when the model layer ran, so its contribution can be separated
    # from the deterministic result rather than assumed.
    llm_calls: int = 0
    llm_tokens: int = 0
    llm_seconds: float = 0.0
    llm_suppressed: int = 0
    llm_rejected: int = 0
    llm_explained: int = 0

    # Set when the run had no execution oracle, so ground truth falls back to the
    # declared kind. Recorded explicitly because a score computed that way is a
    # different claim from one the oracle settled.
    oracle_available: bool = True

    @property
    def is_true_defect(self) -> bool:
        if not self.oracle_available:
            return self.mutation.kind is not Kind.CONTROL
        return self.changed_results

    @property
    def classification(self) -> str:
        """Confusion-matrix cell, using execution as truth.

        Latent defects sit outside the matrix: execution cannot judge them, so calling
        a correct flag a false positive would be actively misleading.
        """
        if self.mutation.kind is Kind.LATENT:
            return "latent_caught" if self.detected else "latent_missed"
        if self.mutation.kind is Kind.GENERATED:
            # Nobody chose these, so they are scored on the oracle alone: did the
            # numbers move, and was anything reported.
            if not self.changed_results:
                return "generated_inert" if not self.detected else "generated_noise"
            return "generated_caught" if self.detected else "generated_MISSED"
        if self.mutation.kind is Kind.UNRULED:
            # The measure that matters is whether anything reported it, not which
            # family did — by construction no family covers it.
            return "unruled_caught" if self.detected else "unruled_MISSED"
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


class DirtyRepositoryError(RuntimeError):
    """The working tree has uncommitted changes.

    Not a safety matter — the harness works in a throwaway worktree and cannot touch
    the caller's checkout. What remains is that uncommitted work is not *in* the
    revision being measured, so the numbers would describe committed code while the
    author believes they describe what is on disk.
    """


def assert_clean(repo: Path) -> None:
    """Refuse to run against a dirty working tree."""
    dirty = _git(repo, "status", "--porcelain").strip()
    if dirty:
        count = len(dirty.splitlines())
        raise DirtyRepositoryError(
            f"{count} uncommitted change(s) in {repo}. The harness measures a committed "
            "revision in an isolated worktree, so those changes would not be included "
            "and the numbers would not describe what is on disk. Commit them, or pass "
            "--allow-dirty to measure the committed state deliberately."
        )


def run_mutation(
    project_dir: Path,
    mutation: Mutation,
    *,
    settings: Settings,
    base_ref: str,
    use_llm: bool = False,
    use_execution: bool = True,
) -> MutationOutcome:
    """Apply one mutation in an isolated worktree, review it, and discard the worktree.

    The harness never touches the caller's working tree. An earlier version created a
    branch in place and restored with ``git checkout --force``, which discarded any
    uncommitted work — it destroyed changes twice during development, and the
    dirty-tree guard could not prevent it because edits made *during* a run are not
    visible at the start.

    Working in a throwaway worktree removes the possibility rather than guarding
    against it.
    """
    repo = git.repo_root(project_dir)
    relative = project_dir.resolve().relative_to(repo.resolve())
    base_sha = _git(repo, "rev-parse", base_ref).strip()

    with tempfile.TemporaryDirectory(prefix="themis-eval-") as tmp:
        tree = Path(tmp) / "tree"
        branch = f"themis-eval/{mutation.id}"
        _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), base_sha)
        try:
            mutated_project = tree / relative
            if not mutation.apply(mutated_project):
                return MutationOutcome(
                    mutation=mutation,
                    applied=False,
                    changed_results=False,
                    detected=False,
                    families_fired=(),
                    expected_family_fired=False,
                    finding_count=0,
                    error="anchor text not found — the mutation is stale",
                )

            # Commit inside the worktree, on a detached HEAD. Nothing here can reach
            # the caller's checkout.
            _git(tree, "add", "--", str((mutated_project / mutation.relative_path).resolve()))
            _git(
                tree,
                "-c",
                "user.email=eval@themis.invalid",
                "-c",
                "user.name=themis-eval",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                f"eval: {mutation.id}",
            )

            result = run_review(
                mutated_project,
                base=base_sha,
                head="HEAD",
                settings=settings,
                run_execution=use_execution,
                run_llm=use_llm,
                # The worktree holds the mutated code; the data lives only in the
                # original project. Without this a macro that queries at compile time
                # reads an empty database and every rule is skipped for want of
                # compiled SQL — which looks like a clean review.
                data_anchor=project_dir,
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
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            try:
                _git(repo, "worktree", "remove", "--force", str(tree))
            except RuntimeError:
                _git(repo, "worktree", "prune")
            # The branch name is unused now that the worktree is detached, but prune
            # any leftover from an interrupted earlier run.
            with contextlib.suppress(RuntimeError):
                _git(repo, "branch", "-q", "-D", branch)

    execution = result.execution
    changed = bool(
        execution and execution.ran and any(d.is_material for d in execution.deltas.values())
    )
    families = tuple(sorted({f.family for f in result.findings}))
    rules = tuple(sorted({f.rule_id for f in result.findings}))
    row_delta = None
    if execution and execution.ran:
        deltas = [d.row_delta for d in execution.deltas.values() if d.row_delta]
        row_delta = max(deltas, key=abs) if deltas else 0

    llm = result.llm
    return MutationOutcome(
        mutation=mutation,
        applied=True,
        oracle_available=use_execution,
        changed_results=changed,
        detected=bool(result.findings),
        families_fired=families,
        rules_fired=rules,
        expected_family_fired=mutation.expects_family in families,
        finding_count=len(result.findings),
        row_delta=row_delta,
        llm_calls=llm.usage.calls if llm else 0,
        llm_tokens=(llm.usage.prompt_tokens + llm.usage.completion_tokens) if llm else 0,
        llm_seconds=llm.usage.seconds if llm else 0.0,
        llm_suppressed=llm.suppressed if llm else 0,
        llm_explained=llm.explained if llm else 0,
        llm_rejected=llm.rejected_by_selfcheck if llm else 0,
    )


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

        Generated mutations are excluded too, and for a different reason: nobody chose
        them, so folding them into a precision figure alongside cases picked to exercise
        specific rules would mix two very different claims.
        """
        return [
            o
            for o in self.usable
            if o.mutation.kind not in (Kind.LATENT, Kind.UNRULED, Kind.GENERATED)
        ]

    @property
    def latent(self) -> list[MutationOutcome]:
        """Defects execution cannot see, scored on detection alone."""
        return [o for o in self.usable if o.mutation.kind is Kind.LATENT]

    @property
    def latent_detected(self) -> int:
        return sum(1 for o in self.latent if o.detected)

    @property
    def unruled(self) -> list[MutationOutcome]:
        """Defects outside every rule family — the safety net's test."""
        return [o for o in self.usable if o.mutation.kind is Kind.UNRULED]

    @property
    def unruled_detected(self) -> int:
        return sum(1 for o in self.unruled if o.detected)

    @property
    def stale(self) -> list[MutationOutcome]:
        return [o for o in self.outcomes if not o.applied or o.error is not None]

    def rule_coverage(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Which rules the corpus actually exercised, and which it never did.

        A rule that has never fired on a real case is unproven no matter how green its
        unit test is. Three rules here passed their tests and could not fire at all --
        one read the outermost SELECT when dbt puts the GROUP BY in a CTE, one matched
        a node type Trino never produces, one was written against SQL the macro does
        not compile to. Only the corpus found them, so the corpus reports this.
        """
        from themis.rules.registry import ALL_RULES

        fired: set[str] = set()
        for outcome in self.usable:
            fired.update(outcome.rules_fired)
        known = {rule.rule_id for rule in ALL_RULES}
        return tuple(sorted(fired & known)), tuple(sorted(known - fired))

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
    def llm_cost(self) -> tuple[int, int, float]:
        """Calls, tokens and seconds the model layer spent across the corpus."""
        calls = sum(o.llm_calls for o in self.usable)
        tokens = sum(o.llm_tokens for o in self.usable)
        seconds = sum(o.llm_seconds for o in self.usable)
        return calls, tokens, seconds

    @property
    def llm_suppressed_total(self) -> int:
        """Findings the model removed.

        The number that decides whether it earned its place: if it suppressed nothing
        and detection is unchanged, it cost time and changed no decision.
        """
        return sum(o.llm_suppressed for o in self.usable)

    @property
    def llm_rejected_total(self) -> int:
        return sum(o.llm_rejected for o in self.usable)

    @property
    def llm_explained_total(self) -> int:
        """Measured changes the model proposed a cause for.

        Counted apart from suppression because it is the one contribution the rules
        structurally cannot make: if the cause were anticipable there would be a rule.
        """
        return sum(o.llm_explained for o in self.usable)

    @property
    def generated(self) -> list[MutationOutcome]:
        """Mutations produced from the code rather than chosen."""
        return [o for o in self.usable if o.mutation.kind is Kind.GENERATED]

    @property
    def generated_missed(self) -> list[MutationOutcome]:
        """Generated mutations that moved the numbers and were not reported.

        The most useful output this corpus can produce: a real defect class nobody
        anticipated, found without anyone having to imagine it first.
        """
        return [o for o in self.generated if o.changed_results and not o.detected]

    @property
    def generated_noise(self) -> list[MutationOutcome]:
        """Generated changes that moved nothing and were still reported."""
        return [o for o in self.generated if not o.changed_results and o.detected]

    @property
    def mislabelled(self) -> list[MutationOutcome]:
        """Mutations whose declared kind disagrees with what execution found.

        Worth surfacing rather than hiding: a 'defect' that changes nothing is a bad
        test, and a 'control' that moves the numbers is a bug in the control.
        """
        return [
            o
            for o in self.scored
            # Meaningless without an oracle: nothing was measured, so every defect
            # trivially "did not change results" and the whole list is noise.
            if o.oracle_available and (o.mutation.kind is Kind.DEFECT) != o.changed_results
        ]


def run_corpus(
    project_dir: Path,
    mutations: tuple[Mutation, ...],
    *,
    settings: Settings,
    base_ref: str = "main",
    allow_dirty: bool = False,
    use_llm: bool = False,
    use_execution: bool = True,
) -> EvalReport:
    if not allow_dirty:
        assert_clean(git.repo_root(project_dir))
    outcomes: list[MutationOutcome] = []
    for index, mutation in enumerate(mutations, start=1):
        log.info("eval.mutation", n=f"{index}/{len(mutations)}", id=mutation.id)
        outcomes.append(
            run_mutation(
                project_dir,
                mutation,
                settings=settings,
                base_ref=base_ref,
                use_llm=use_llm,
                use_execution=use_execution,
            )
        )
    return EvalReport(outcomes=outcomes)
