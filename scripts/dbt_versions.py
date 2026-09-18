"""Read the demo project's manifest as several dbt versions write it, and compare.

The work project will not be on the dbt release this one develops against, and a manifest
is the primary grounding backend: if a field moves between versions, every downstream stage
reads something subtly different and nothing says so. The previous answer to that question
was a reading of dbt's changelog, which is not evidence.

So this compiles the demo project with each dbt release in turn, in its own virtualenv and
its own `--target-path`, loads every manifest through THEMIS's own loader, and compares what
it made of each model — materialization, tags, incremental strategy, unique key,
dependencies, and the compiled SQL itself. Any difference is printed per model and per field.

    python scripts/dbt_versions.py                     # the default set
    python scripts/dbt_versions.py --versions 1.9 1.12

Needs `uv` and network access to build the environments; it never touches the caller's
environment, the project's `target/`, or the demo database beyond what a compile reads.
Exits 1 if any version parses differently from the one running, or fails to compile.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "demo_project"
sys.path.insert(0, str(REPO / "src"))

# dbt-duckdb tracks dbt-core's minor version, so both are pinned to the same series.
DEFAULT_VERSIONS = ("1.8", "1.9", "1.10")


def build_environment(version: str, root: Path) -> Path | None:
    """A virtualenv with that dbt series installed. None if it cannot be built."""
    venv = root / f"venv-{version}"
    nxt = f"1.{int(version.split('.')[1]) + 1}"
    subprocess.run(
        ["uv", "venv", "--python", "3.12", str(venv)],
        capture_output=True,
        text=True,
        check=False,
    )
    installed = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            f"dbt-core>={version},<{nxt}",
            f"dbt-duckdb>={version},<{nxt}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if installed.returncode != 0:
        print(f"  install failed: {installed.stderr.strip().splitlines()[-1][:160]}")
        return None
    return venv


def compile_manifest(venv: Path, version: str, root: Path) -> Path | None:
    """Compile the demo project with that dbt, into its own target path."""
    target = root / f"target-{version}"
    # Inside the project, as dbt must be: the profile's database path is relative to it.
    result = subprocess.run(
        [
            str(venv / "bin" / "dbt"),
            "compile",
            "--profiles-dir",
            ".",
            "--project-dir",
            ".",
            "--target",
            "dev",
            "--target-path",
            str(target),
        ],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=False,
    )
    manifest = target / "manifest.json"
    if not manifest.exists():
        tail = (result.stdout + result.stderr).strip().splitlines()[-3:]
        print(f"  compile failed: {' / '.join(line[:80] for line in tail)}")
        return None
    return manifest


def read(manifest: Path, label: str) -> tuple[dict[str, Any], str | None]:
    """What THEMIS makes of a manifest: the per-model facts every later stage reads."""
    from themis.acquire.manifest import _schema_version, load_manifest
    from themis.models import Backend

    snapshot = load_manifest(manifest, revision=label, backend=Backend.MANIFEST)
    metadata = json.loads(manifest.read_text()).get("metadata") or {}
    facts = {
        name: {
            "materialization": model.materialization,
            "tags": sorted(model.tags),
            "incremental_strategy": model.incremental_strategy,
            "unique_key": sorted(model.unique_key or ()),
            "depends_on": sorted(d.split(".")[-1] for d in model.depends_on_models),
            "compiled_sql": (model.compiled_sql or "").strip(),
            "file_path": model.file_path,
        }
        for name, model in snapshot.models.items()
    }
    return facts, _schema_version(metadata)


def compare(reference: dict[str, Any], other: dict[str, Any], label: str) -> list[str]:
    """Every difference, named. An empty list means the two parse identically."""
    problems: list[str] = []
    for missing in sorted(set(reference) - set(other)):
        problems.append(f"{label}: model {missing} is absent")
    for extra in sorted(set(other) - set(reference)):
        problems.append(f"{label}: model {extra} appears only here")
    for name in sorted(set(reference) & set(other)):
        for field, value in reference[name].items():
            if other[name][field] != value:
                problems.append(
                    f"{label}: {name}.{field} is {str(other[name][field])[:60]!r}, "
                    f"expected {str(value)[:60]!r}"
                )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", nargs="+", default=list(DEFAULT_VERSIONS))
    parser.add_argument("--keep", action="store_true", help="Keep the environments built.")
    args = parser.parse_args()

    if shutil.which("uv") is None:
        print("uv is needed to build the environments")
        return 2
    if not (PROJECT / "target" / "manifest.json").exists():
        print("compile the demo project first: (cd demo_project && dbt compile ...)")
        return 2

    reference, reference_schema = read(PROJECT / "target" / "manifest.json", "current")
    import dbt.version

    print(
        f"reference: dbt {dbt.version.__version__}, manifest {reference_schema}, "
        f"{len(reference)} models\n"
    )

    root = Path(tempfile.mkdtemp(prefix="themis-dbt-versions-"))
    problems: list[str] = []
    try:
        for version in args.versions:
            print(f"dbt {version}")
            venv = build_environment(version, root)
            if venv is None:
                problems.append(f"{version}: could not be installed")
                continue
            manifest = compile_manifest(venv, version, root)
            if manifest is None:
                problems.append(f"{version}: could not compile the demo project")
                continue
            facts, schema = read(manifest, version)
            differences = compare(reference, facts, version)
            problems += differences
            verdict = "identical" if not differences else f"{len(differences)} difference(s)"
            print(f"  manifest {schema}, {len(facts)} models — {verdict}")
            for line in differences[:10]:
                print(f"    {line}")
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)

    print("")
    if problems:
        print(f"{len(problems)} problem(s) — THEMIS does not read every version the same way")
        return 1
    print(f"every version parses identically to the reference ({', '.join(args.versions)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
