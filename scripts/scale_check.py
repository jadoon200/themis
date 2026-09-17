"""How THEMIS's analysis scales with the size of a dbt project.

Generates synthetic compiled projects (themis.eval.synthetic) at several sizes, edits a
few staging models the way a real change does, and times every analysis stage a review
runs — plus the whole-project commands, which read every model rather than the changed
region. No dbt and no warehouse: this measures THEMIS, not the tools around it.

    python scripts/scale_check.py                 # 250, 1000, 3000 models
    python scripts/scale_check.py --sizes 5000    # one size
    python scripts/scale_check.py --profile       # cProfile the largest size

Stage 3 is not timed here: its cost is the warehouse's, and the part THEMIS owns — how
many models it asks the warehouse to build — is reported as `build selection`.
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import resource
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from themis.analyze.grain import infer_grains  # noqa: E402
from themis.analyze.lineage import build_column_graph  # noqa: E402
from themis.analyze.positioning import position_findings  # noqa: E402
from themis.analyze.volatility import volatile_columns  # noqa: E402
from themis.eval import synthetic  # noqa: E402
from themis.logging import configure_logging  # noqa: E402
from themis.pipeline import build_contexts  # noqa: E402
from themis.rules.registry import run_rules  # noqa: E402
from themis.triage.rubric import triage  # noqa: E402


def _timed(label: str, timings: dict[str, float], work: Callable[[], Any]) -> Any:
    started = time.perf_counter()
    result = work()
    timings[label] = time.perf_counter() - started
    return result


def measure(models: int, *, changes: int) -> dict[str, Any]:
    before = synthetic.project(models)
    acquired = synthetic.changed(before, count=changes)
    timings: dict[str, float] = {}

    grains = _timed("grain", timings, lambda: infer_grains(acquired.after))
    contexts = _timed(
        "contexts", timings, lambda: build_contexts(acquired, grains, dialect="trino")
    )
    findings, skipped = _timed("rules (+lineage)", timings, lambda: run_rules(contexts))
    _timed(
        "volatility",
        timings,
        lambda: (volatile_columns(acquired.before), volatile_columns(acquired.after)),
    )

    def selection() -> set[str]:
        targets = set(c.model_name for c in contexts)
        for name in list(targets):
            targets.update(acquired.after.downstream_of(name))
        return targets

    targets = _timed("build selection", timings, selection)
    _timed("positioning", timings, lambda: position_findings(findings, acquired.after))
    _timed("triage", timings, lambda: triage(findings))
    review_total = sum(timings.values())
    _timed("whole-project lineage", timings, lambda: build_column_graph(acquired.after))
    return {
        "models": len([m for m in acquired.after.models.values() if not m.is_seed]),
        "changed": len(acquired.changed_models),
        "contexts": len(contexts),
        "findings": len(findings),
        "skipped": len(skipped),
        "selection": len(targets),
        "review_total": review_total,
        "timings": timings,
        "rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sizes", type=int, nargs="+", default=[250, 1000, 3000])
    parser.add_argument("--changes", type=int, default=3, help="staging models edited")
    parser.add_argument("--profile", action="store_true", help="cProfile the largest size")
    args = parser.parse_args()
    configure_logging()

    rows = []
    for size in args.sizes:
        print(f"measuring {size} models ...", flush=True)
        rows.append(measure(size, changes=args.changes))

    stages = list(rows[0]["timings"])
    header = f"{'stage':24s}" + "".join(f"{row['models']:>12,d}" for row in rows)
    print("\n" + header)
    print("-" * len(header))
    for stage in stages:
        print(f"{stage:24s}" + "".join(f"{row['timings'][stage]:>11.2f}s" for row in rows))
    print("-" * len(header))
    for label, key, fmt in (
        ("review analysis total", "review_total", "{:>11.2f}s"),
        ("models reviewed", "contexts", "{:>12,d}"),
        ("findings", "findings", "{:>12,d}"),
        ("checks skipped", "skipped", "{:>12,d}"),
        ("build selection", "selection", "{:>12,d}"),
        ("peak memory (MB)", "rss_mb", "{:>12,.0f}"),
    ):
        print(f"{label:24s}" + "".join(fmt.format(row[key]) for row in rows))

    if args.profile:
        largest = max(args.sizes)
        print(f"\nprofiling {largest} models")
        profiler = cProfile.Profile()
        profiler.enable()
        measure(largest, changes=args.changes)
        profiler.disable()
        stats = pstats.Stats(profiler).sort_stats("cumulative")
        stats.print_stats(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
