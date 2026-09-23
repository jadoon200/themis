"""A real review on a real Trino, end to end.

Trino is the engine THEMIS is aimed at, and until this existed CI only ever tested the
Trino *client*. Nothing compiled the demo project with dbt-trino, reviewed a change with
execution against Trino, or checked that a run's schemas are dropped on a connector
that cannot `DROP SCHEMA ... CASCADE`. That path differs from DuckDB at every step, and
the one time it was exercised by hand is not evidence that it still works.

Runs from a throwaway worktree, so the caller's checkout is never touched:

1. seed and build the demo project on Trino, cold;
2. commit the fan-out mutation on top of HEAD;
3. review that commit with execution, against Trino;
4. assert the review is complete, the fan-out is reported and measured, every model
   that built was found where dbt put it — Hive marts and Iceberg reference data alike —
   and no schema the run built is left behind in either catalog.

    make up                     # Postgres, and Trino with Hive and Iceberg catalogs
    python scripts/trino_smoke.py

Exits non-zero, naming what failed, if any check does.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from themis.config import Settings  # noqa: E402
from themis.eval.mutations import select  # noqa: E402
from themis.logging import configure_logging  # noqa: E402
from themis.pipeline import review  # noqa: E402

# The demo project's `dev` is Trino with Hive and Iceberg — the engine of record.
TARGET = "dev"
CATALOGS = ("hive", "iceberg")
MUTATION = "fanout_drop_join_predicate"


def _run(args: list[str], cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"{' '.join(args)} failed:\n{result.stdout[-3000:]}\n{result.stderr[-2000:]}"
        )


def _trino_schemas() -> set[str]:
    """Every schema in every catalog a build writes to, qualified by catalog."""
    import trino

    cursor = trino.dbapi.connect(host="127.0.0.1", port=8085, user="themis").cursor()
    schemas: set[str] = set()
    for catalog in CATALOGS:
        cursor.execute(f"select schema_name from {catalog}.information_schema.schemata")
        schemas.update(f"{catalog}.{row[0]}" for row in cursor.fetchall())
    return schemas


def main() -> int:
    configure_logging()
    dbt = str(Path(sys.executable).parent / "dbt")
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="themis-trino-smoke-") as tmp:
        tree = Path(tmp) / "tree"
        _run(["git", "worktree", "add", "--detach", "--quiet", str(tree), "HEAD"], REPO)
        try:
            project = tree / "demo_project"
            dbt_args = ["--profiles-dir", ".", "--project-dir", ".", "--target", TARGET]
            _run([dbt, "seed", *dbt_args], project)
            _run([dbt, "build", "--full-refresh", *dbt_args], project)

            base = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=tree, capture_output=True, text=True, check=True
            ).stdout.strip()
            mutation = select(MUTATION)[0]
            if not mutation.apply(project):
                raise SystemExit(f"{MUTATION} did not apply — the mutation is stale")
            _run(
                [
                    "git",
                    "-c",
                    "user.email=smoke@themis.invalid",
                    "-c",
                    "user.name=themis-smoke",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-qam",
                    MUTATION,
                ],
                tree,
            )

            before = _trino_schemas()
            result = review(
                project,
                base=base,
                head="HEAD",
                settings=Settings(),
                target=TARGET,
                run_execution=True,
                run_llm=False,
                use_manifest_cache=False,
            )
            leftover = {s for s in _trino_schemas() - before if ".themis_" in s}

            rules = sorted({f.rule_id for f in result.findings})
            print(f"findings: {rules}")
            print(f"incomplete: {list(result.incomplete_reasons)}")

            if result.degraded_reason:
                failures.append(f"grounding degraded: {result.degraded_reason}")
            if not result.executed or result.execution is None:
                failures.append("execution did not run")
            else:
                revenue = result.execution.deltas.get("fct_revenue")
                if revenue is None or revenue.failed_revision is not None:
                    failures.append(f"fct_revenue was not measured: {revenue}")
                elif not (revenue.rows_before and revenue.rows_after):
                    failures.append(f"fct_revenue has no row counts: {revenue}")
                elif revenue.rows_after <= revenue.rows_before:
                    failures.append(
                        "the fan-out did not measure: "
                        f"{revenue.rows_before} -> {revenue.rows_after}"
                    )
                # Found where dbt put it, or it was never measured at all: a model that
                # built on both sides and has no row count on either was looked up in the
                # wrong place, which reads as "nothing moved".
                lost = sorted(
                    name
                    for name, delta in result.execution.deltas.items()
                    if delta.failed_revision is None
                    and delta.rows_before is None
                    and delta.rows_after is None
                )
                if lost:
                    failures.append(f"built but not found where dbt put them: {lost}")
            if "F1001" not in rules:
                failures.append("F1001 did not report the dropped join predicate")
            if "X0002" in rules:
                # Hive overwrites partitions, so the incremental model's second pass builds
                # on both revisions. A build failure here would be a regression in the
                # demo project, not the change's doing.
                failures.append("X0002 blamed the change for a build that should succeed")
            if leftover:
                failures.append(f"schemas left behind: {sorted(leftover)}")
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=REPO,
                capture_output=True,
                check=False,
            )

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
