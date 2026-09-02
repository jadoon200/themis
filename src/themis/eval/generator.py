"""Mutations generated from the code, not chosen by the author.

Every hand-written mutation in this corpus is a defect class somebody wrote a rule for,
so the rules will always win on them. That circularity is the single biggest caveat on
the reported recall: it measures how well a patch fits the hole it was cut for.

These mutations break it. The generator walks a model's own AST and applies mechanical
transformations wherever they happen to fit — flipping a comparison, tightening a join,
swapping an aggregate. What gets produced is determined by what is *in the SQL*, not by
what anyone thought to check. Some will be defects, some will be harmless; the execution
oracle decides which, exactly as it does for the written ones.

A generated mutation that changes the numbers and is not reported is the most valuable
signal this project can produce: a real defect class nobody anticipated.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

from themis.eval.mutations import Kind, Mutation
from themis.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Transform:
    """One mechanical edit: a pattern, its replacement, and what it does."""

    name: str
    pattern: re.Pattern[str]
    replacement: str
    description: str


# Deliberately written against raw model source rather than the AST. The AST would give
# cleaner edits, but it would also mean only constructs sqlglot models can be mutated —
# and the point is to reach things nobody planned for, including Jinja.
TRANSFORMS: tuple[Transform, ...] = (
    Transform(
        "join_to_inner",
        re.compile(r"\bleft\s+join\b", re.IGNORECASE),
        "inner join",
        "an outer join tightened, dropping unmatched rows",
    ),
    Transform(
        "join_to_left",
        re.compile(r"\binner\s+join\b", re.IGNORECASE),
        "left join",
        "an inner join loosened, admitting NULLs into aggregates",
    ),
    Transform(
        "gte_to_gt",
        re.compile(r">="),
        ">",
        "a boundary made exclusive, dropping the edge of a range",
    ),
    Transform(
        "lte_to_lt",
        re.compile(r"<="),
        "<",
        "a boundary made exclusive at the other end",
    ),
    Transform(
        "sum_to_avg",
        re.compile(r"\bsum\s*\(", re.IGNORECASE),
        "avg(",
        "a total replaced by an average",
    ),
    Transform(
        "count_distinct_dropped",
        re.compile(r"\bcount\s*\(\s*distinct\s+", re.IGNORECASE),
        "count(",
        "a distinct count made non-distinct",
    ),
    Transform(
        "and_to_or",
        re.compile(r"\n(\s*)and\s+", re.IGNORECASE),
        r"\n\1or ",
        "a conjunction loosened to a disjunction",
    ),
    Transform(
        "union_all",
        re.compile(r"\bunion\b(?!\s+all)", re.IGNORECASE),
        "union all",
        "set deduplication removed",
    ),
    Transform(
        "coalesce_dropped",
        re.compile(r"\bcoalesce\s*\(\s*([\w.]+)\s*,\s*[^)]+\)", re.IGNORECASE),
        r"\1",
        "a NULL fallback removed",
    ),
    Transform(
        "not_removed",
        re.compile(r"\bnot\s+(?=[\w(])", re.IGNORECASE),
        "",
        "a negation dropped from a predicate",
    ),
)


def _model_files(project_dir: Path) -> list[Path]:
    return sorted(p for p in (project_dir / "models").rglob("*.sql") if p.is_file())


def generate(
    project_dir: Path,
    *,
    limit: int = 20,
    seed: int = 0,
) -> tuple[Mutation, ...]:
    """Produce mutations by applying each transform wherever it fits.

    Deterministic for a given seed, so a run is reproducible and a regression can be
    traced to a specific mutation rather than to luck.
    """
    rng = random.Random(seed)
    candidates: list[Mutation] = []

    for path in _model_files(project_dir):
        source = path.read_text()
        relative = path.relative_to(project_dir).as_posix()
        for transform in TRANSFORMS:
            for match in transform.pattern.finditer(source):
                original = match.group(0)
                replacement = match.expand(transform.replacement)
                if original == replacement:
                    continue
                # Anchor on enough surrounding text to make the edit unambiguous; the
                # same token often appears many times in one model.
                start = max(0, match.start() - 40)
                end = min(len(source), match.end() + 40)
                anchor = source[start:end]
                if source.count(anchor) != 1:
                    continue
                candidates.append(
                    Mutation(
                        id=f"gen_{path.stem}_{transform.name}_{match.start()}",
                        kind=Kind.GENERATED,
                        expects_family="",
                        description=f"{transform.description} in {path.stem}",
                        relative_path=relative,
                        find=anchor,
                        replace=anchor.replace(original, replacement, 1),
                    )
                )

    rng.shuffle(candidates)
    chosen = tuple(candidates[:limit])
    log.info("generator.produced", candidates=len(candidates), chosen=len(chosen))
    return chosen
