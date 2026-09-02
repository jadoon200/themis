"""Record model responses for the end-to-end replay test.

Run against a real Ollama, on a machine that has one:

    make record-cassette

Re-run after changing any prompt. A cassette key includes the prompt, so an edited
prompt no longer matches its recording and the replay test fails — which is the point:
a recorded answer to the old question says nothing about the new one.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from themis.config import load_settings
from themis.eval.mutations import select
from themis.llm.cassette import Cassette, RecordingProvider
from themis.llm.provider import build_provider
from themis.logging import configure_logging, get_logger

log = get_logger(__name__)

CASSETTE = Path("tests/cassettes/review.json")

# The two shapes worth recording: a finding the model must adjudicate, and a measured
# change with no rule behind it, which is the only thing the model uniquely does.
SCENARIOS = (
    ("fanout_drop_join_predicate", False),
    ("unruled_fx_inverted", True),
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def main() -> int:
    from themis.acquire import git
    from themis.pipeline import review

    configure_logging()
    settings = load_settings()
    project = Path("demo_project")
    repo = git.repo_root(project)
    base = _git(repo, "rev-parse", "main").strip()
    relative = project.resolve().relative_to(repo.resolve())

    cassette = Cassette(CASSETTE)
    provider = RecordingProvider(build_provider(settings), cassette)

    for mutation_id, execute in SCENARIOS:
        mutation = select(mutation_id)[0]
        with tempfile.TemporaryDirectory(prefix="themis-record-") as tmp:
            tree = Path(tmp) / "tree"
            _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), base)
            try:
                target = tree / relative
                if not mutation.apply(target):
                    log.warning("record.stale_mutation", id=mutation_id)
                    continue
                _git(tree, "add", "-A")
                _git(
                    tree,
                    "-c",
                    "user.email=record@themis.invalid",
                    "-c",
                    "user.name=themis",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-q",
                    "-m",
                    f"record: {mutation_id}",
                )
                review(
                    target,
                    base=base,
                    head="HEAD",
                    settings=settings,
                    run_execution=execute,
                    run_llm=True,
                    pr_description=("Simplify the FX join; the period predicate was redundant."),
                    provider=provider,
                )
            finally:
                _git(repo, "worktree", "remove", "--force", str(tree))

    cassette.save()
    log.info("record.complete", entries=len(cassette), path=str(CASSETTE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
