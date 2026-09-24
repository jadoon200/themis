"""Exercise every component for real, and say what each one did.

The unit suite proves the pieces; it has repeatedly failed to prove the product. One
end-to-end pass once found a table property a real Trino rejects, a CLI answering about a
model that does not exist, and an incremental build that only worked the first time — all
invisible to hundreds of green tests. This script is that pass, made repeatable.

Each check runs the real CLI, database, API, worker, warehouse or model, and declares the
outcome it expects — a refusal that is supposed to happen passes; one that does not happen
fails. A check whose service is not running is reported SKIP, never counted as a pass.

Scenarios are built as commits in a throwaway worktree on top of HEAD, so the script needs
no local branches and never touches the caller's checkout:

    make demo-build                                   # the project, built
    make up                                           # Postgres 5436, Trino 8085
    ollama serve                                      # the model          (optional)
    python scripts/component_check.py [--quick]

`--quick` leaves out the slow ones: the model layer, the corpus subsets and Trino.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "demo_project"
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

PG_ADMIN = "postgresql+psycopg://themis:themis@127.0.0.1:5436/themis"
PG_CHECK_DB = "themis_component_check"


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


RESULTS: list[Result] = []
_COUNTER = itertools.count()


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append(Result(name, "PASS" if ok else "FAIL", detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def skip(name: str, why: str) -> None:
    RESULTS.append(Result(name, "SKIP", why))
    print(f"  SKIP  {name} — {why}", flush=True)


def themis(
    *args: str, env: dict[str, str] | None = None, timeout: float = 900
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "themis.cli", *args],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(SRC), **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def run_schemas() -> list[str]:
    """Every schema a Stage 3 run left behind, on either engine.

    Execution builds on Trino now, and this used to look in the DuckDB files alone — so
    "no run schemas left" passed whatever Trino was holding. A snapshot adds a second
    catalog to that: its run schema is in Iceberg, beside the models' in Hive.
    """
    found = duckdb_run_schemas()
    if port_open(8085):
        import trino

        cursor = trino.dbapi.connect(host="127.0.0.1", port=8085, user="themis").cursor()
        for catalog in ("hive", "iceberg"):
            cursor.execute(
                f"select schema_name from {catalog}.information_schema.schemata "
                "where schema_name like 'themis_base_%' or schema_name like 'themis_head_%'"
            )
            found += [f"{catalog}.{row[0]}" for row in cursor.fetchall()]
    return found


def trino_schema_exists(catalog: str, schema: str) -> bool:
    import trino

    cursor = trino.dbapi.connect(host="127.0.0.1", port=8085, user="themis").cursor()
    cursor.execute(
        f"select count(*) from {catalog}.information_schema.schemata where schema_name = '{schema}'"
    )
    return bool(cursor.fetchone()[0])


def duckdb_run_schemas() -> list[str]:
    import duckdb

    found: list[str] = []
    for name in ("themis_demo.duckdb", "reference.duckdb"):
        conn = duckdb.connect(str(PROJECT / name), read_only=True)
        try:
            found += [
                str(row[0])
                for row in conn.execute(
                    "select schema_name from information_schema.schemata "
                    "where schema_name like 'themis_base_%' or schema_name like 'themis_head_%'"
                ).fetchall()
            ]
        finally:
            conn.close()
    return found


# --- scenarios ---------------------------------------------------------------------------


CONVENTIONS = """conventions:
  - id: fx-rates-one-per-period
    rules: [F1001]
    models: ["int_*"]
    condition: A join onto stg_fx_rates in an intermediate model.
    guidance: stg_fx_rates holds one row per currency per month, by contract with treasury.
    implication: A join onto it multiplies rows unless both currency and period are matched.
"""


def build_scenarios(tmp: Path) -> dict[str, str]:
    """Commit one change per scenario on top of HEAD. Returns name -> commit SHA."""
    from themis.eval.mutations import select

    tree = tmp / "scenarios"
    base = git("rev-parse", "HEAD")
    git("worktree", "add", "--detach", "--quiet", str(tree), base)
    shas: dict[str, str] = {}

    def commit(name: str, change: Callable[[Path], None], parent: str | None = None) -> None:
        git("checkout", "--detach", "--quiet", parent or base, cwd=tree)
        change(tree / "demo_project")
        # New files are staged by name. `commit -a` only picks up tracked ones, and a
        # blanket add in a worktree is the habit that has destroyed work in this repo.
        conventions_file = tree / "demo_project" / "themis_conventions.yml"
        if conventions_file.exists():
            git("add", str(conventions_file.relative_to(tree)), cwd=tree)
        git(
            "-c",
            "user.email=check@themis.invalid",
            "-c",
            "user.name=themis-check",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qam",
            name,
            cwd=tree,
        )
        shas[name] = git("rev-parse", "HEAD", cwd=tree)

    def mutation(mutation_id: str) -> Callable[[Path], None]:
        def apply(project: Path) -> None:
            assert select(mutation_id)[0].apply(project), mutation_id

        return apply

    def edit_seed(project: Path) -> None:
        path = project / "seeds" / "raw_fx_rates.csv"
        lines = path.read_text().splitlines()
        header, first = lines[0], lines[1].split(",")
        first[2] = f"{float(first[2]) * 1.5:.8f}"  # one FX rate moves
        path.write_text("\n".join([header, ",".join(first), *lines[2:]]) + "\n")

    def edit_project_config(project: Path) -> None:
        # Tags, because they merge with a model's own config. Every demo model sets its
        # materialization in its own file, which overrides a folder-level one entirely —
        # a materialization edit here would correctly reach nothing.
        path = project / "dbt_project.yml"
        text = path.read_text()
        assert "    intermediate:\n      +materialized: view" in text
        path.write_text(
            text.replace(
                "    intermediate:\n      +materialized: view",
                "    intermediate:\n      +materialized: view\n      +tags: ['regulatory']",
            )
        )

    try:
        commit("fanout", mutation("fanout_drop_join_predicate"))
        commit("macro_double", mutation("money_cast_to_double"))
        commit("alias_refactor", mutation("control_rename_alias_in_filter"))
        commit("seed_change", edit_seed)
        commit("project_config", edit_project_config)
        commit("recognition", mutation("unruled_recognition_default_flipped"))

        def fanout_with_conventions(project: Path) -> None:
            mutation("fanout_drop_join_predicate")(project)
            (project / "themis_conventions.yml").write_text(CONVENTIONS)

        commit("fanout_conventions", fanout_with_conventions)

        # A project that stamps audit columns — the normal state of a real dbt project. The
        # values differ between any two builds, so a comment upstream must stay silent and a
        # real reclassification must still be caught beside them.
        def audit_columns(project: Path) -> None:
            for relative, anchor in (
                (
                    "models/marts/fct_account_period_summary.sql",
                    "        sum(amount_txn_ccy) as net_amount_txn_ccy\n",
                ),
                ("models/marts/fct_revenue.sql", "    amount_usd\n"),
            ):
                path = project / relative
                text = path.read_text()
                assert anchor in text, relative
                path.write_text(
                    text.replace(
                        anchor,
                        anchor.rstrip("\n")
                        # Cast, because Hive cannot store `timestamp with time
                        # zone` — "Unsupported Hive type" — so this is how an audit
                        # column on a Hive table at work is written.
                        + ",\n        cast(current_timestamp as timestamp(3)) as processed_at,"
                        + "\n        '{{ run_started_at }}' as loaded_at,"
                        + "\n        '{{ invocation_id }}' as batch_id\n",
                    )
                )

        def upstream_comment(project: Path) -> None:
            path = project / "models/staging/stg_gl_entries.sql"
            text = path.read_text()
            marker = "-- General ledger entries, typed and signed. One row per entry_id."
            assert marker in text
            path.write_text(text.replace(marker, marker + " Source: ERP export."))

        # A snapshot filtered while it records deletions: measured on Iceberg, and the
        # run's schema there has to be dropped as well as the one in Hive.
        commit("snapshot_filter", mutation("snapshot_filter_records_deletions"))

        # The legacy spelling: a fixed target_schema, which bypasses the schema macro, so
        # base and head would both write iceberg.snapshots. Refused before anything runs.
        def legacy_snapshot_location(project: Path) -> None:
            path = project / "snapshots" / "snap_accounts.sql"
            text = path.read_text()
            assert "    schema='history'," in text
            path.write_text(text.replace("    schema='history',", "    target_schema='snapshots',"))

        commit("snapshot_legacy_location", legacy_snapshot_location)

        commit("audit_base", audit_columns)
        commit("audit_comment", upstream_comment, parent=shas["audit_base"])
        commit(
            "audit_recognition",
            mutation("unruled_recognition_default_flipped"),
            parent=shas["audit_base"],
        )
    finally:
        git("worktree", "remove", "--force", str(tree))
    return shas


# --- checks ----------------------------------------------------------------------------------


def check_analysis(scratch_env: dict[str, str]) -> None:
    print("\nanalysis commands")
    r = themis("version")
    record("version", r.returncode == 0 and "themis" in r.stdout, r.stderr[-200:])

    r = themis("grain", "--project", "demo_project", "--explain")
    record("grain --explain", r.returncode == 0 and "proven" in r.stdout, r.stderr[-300:])

    r = themis("lineage", "--project", "demo_project")
    record("lineage coverage", r.returncode == 0 and "0 unresolved" in r.stdout, r.stdout[-300:])
    r = themis(
        "lineage", "--project", "demo_project", "--model", "stg_fx_rates", "--column", "rate"
    )
    record("lineage of one column", r.returncode == 0 and "feeds" in r.stdout, r.stderr[-300:])
    r = themis("lineage", "--project", "demo_project", "--model", "no_such_model", "--column", "x")
    record("lineage refuses an unknown model", r.returncode == 2, f"exit {r.returncode}")

    r = themis("suggest-tests", "--project", "demo_project", "--yaml")
    ok = r.returncode == 0
    held = failed = 0
    if ok:
        import trino
        import yaml

        doc = yaml.safe_load(r.stdout)
        # On Trino, where the project is built: marts in Hive, reference data in Iceberg
        # under the custom schema dbt gives it.
        cursor = trino.dbapi.connect(host="127.0.0.1", port=8085, user="themis").cursor()

        def scalar(sql: str) -> tuple:
            cursor.execute(sql)
            return tuple(cursor.fetchone())

        for model in doc.get("models", []):
            name = model["name"]
            columns: list[str] = []
            for test in model.get("tests") or model.get("data_tests") or []:
                if isinstance(test, dict):
                    body = next(iter(test.values()))
                    columns = list((body or {}).get("combination_of_columns", []))
            for column in model.get("columns", []):
                if any(
                    t == "unique" for t in column.get("tests", []) or column.get("data_tests", [])
                ):
                    columns = [column["name"]]
            relation = next(
                (
                    f"{c}.{sch}.{name}"
                    for c, sch in (("hive", "main"), ("iceberg", "main_main"))
                    if scalar(
                        f"select count(*) from {c}.information_schema.tables "
                        f"where table_schema='{sch}' and table_name='{name}'"
                    )[0]
                ),
                None,
            )
            if relation is None or not columns:
                failed += 1
                continue
            key = ", ".join(f"cast({c} as varchar)" for c in columns)
            rows, distinct = scalar(
                f"select count(*), count(distinct concat_ws(chr(31), {key})) from {relation}"
            )
            held += int(rows == distinct)
            failed += int(rows != distinct)
    record(
        "suggested tests hold on the built tables",
        ok and held > 0 and failed == 0,
        f"{held} held, {failed} failed or unresolvable",
    )

    r = themis("profile", "--project", "demo_project", "--json")
    ok = r.returncode == 0
    names_leaked: list[str] = []
    if ok:
        text = r.stdout
        names_leaked = [n for n in ("fct_revenue", "stg_fx_rates", "entity_code") if n in text]
        ok = not names_leaked and json.loads(text)["compiled_sql"]["parse_failures_as_trino"] == 0
    record("profile names nothing and parses everything", ok, f"leaked {names_leaked}")

    r = themis("cache", "--project", "demo_project")
    record("cache listing", r.returncode == 0, r.stderr[-200:])
    r = themis("cache", "--warm", "HEAD", "--project", "demo_project")
    record(
        "cache refuses a project that builds SQL from query results",
        r.returncode == 1 and "query results" in r.stdout,
        f"exit {r.returncode}: {r.stdout[-200:]}",
    )


def review_json(
    head: str, tmp: Path, *extra: str, env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict]:
    # A fresh name per call: reading a file an earlier call left would report that
    # call's review as this one's whenever this one failed before writing.
    out = tmp / f"review-{next(_COUNTER)}.json"
    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        head,
        "--no-save",
        "--json",
        str(out),
        *extra,
        env=env,
    )
    doc = json.loads(out.read_text()) if out.exists() else {}
    return r, doc


def rules_in(doc: dict) -> set[str]:
    return {f["rule_id"] for f in doc.get("findings", [])}


def check_reviews(shas: dict[str, str], tmp: Path, env: dict[str, str]) -> None:
    print("\nstatic reviews")
    r, doc = review_json(shas["fanout"], tmp, "--no-llm", env=env)
    record(
        "fan-out on another commit is found (head built from that commit)",
        r.returncode == 0 and "F1001" in rules_in(doc) and not doc.get("incomplete"),
        f"exit {r.returncode}, rules {sorted(rules_in(doc))}, {r.stderr[-300:]}",
    )

    r, doc = review_json(shas["macro_double"], tmp, "--no-llm", env=env)
    macro_models = {f["model"] for f in doc.get("findings", []) if f["rule_id"] == "F3001"}
    record(
        "macro edit reviewed as the models it reaches",
        r.returncode == 0 and len(macro_models) >= 2 and "Macro `money` changed" in r.stdout,
        f"F3001 on {sorted(macro_models)}",
    )

    r, doc = review_json(shas["alias_refactor"], tmp, "--no-llm", env=env)
    record(
        "pure refactor raises nothing",
        r.returncode == 0 and not doc.get("findings"),
        f"rules {sorted(rules_in(doc))}",
    )

    r, doc = review_json(shas["seed_change"], tmp, "--no-llm", env=env)
    record(
        "seed data change is named, with what it feeds",
        r.returncode == 0
        and doc.get("seeds_changed", {}).get("raw_fx_rates")
        and "Seed `raw_fx_rates` changed" in r.stdout,
        f"seeds_changed={doc.get('seeds_changed')}",
    )

    r, doc = review_json(shas["project_config"], tmp, "--no-llm", env=env)
    reviewed = set(doc.get("models_reviewed", []))
    record(
        "dbt_project.yml edit reaches exactly the models it reconfigured",
        r.returncode == 0
        and reviewed
        == {"int_account_activity", "int_gl_entries_converted", "int_revenue_recognized"},
        f"models_reviewed={sorted(reviewed)}",
    )

    print("\nguards and the merge gate")
    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--no-llm",
        "--no-save",
        "--target",
        "prod",
        env=env,
    )
    record(
        "a production target is refused",
        r.returncode == 2 and "refusing" in r.stderr,
        f"exit {r.returncode}",
    )

    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head=--output=/tmp/x",
        "--no-llm",
        "--no-save",
        env=env,
    )
    record(
        "a revision git would read as an option is refused",
        r.returncode == 2,
        f"exit {r.returncode}",
    )

    gate_env = {**env, "THEMIS_FAIL_ON_SEVERITY": "HIGH"}
    r, _ = review_json(shas["fanout"], tmp, "--no-llm", env=gate_env)
    record("blocking gate fails on a high finding", r.returncode == 1, f"exit {r.returncode}")

    r, _ = review_json(shas["alias_refactor"], tmp, "--no-llm", env=gate_env)
    record(
        "blocking gate passes a clean, complete review", r.returncode == 0, f"exit {r.returncode}"
    )

    r, doc = review_json(
        shas["alias_refactor"],
        tmp,
        "--no-llm",
        "--execute",
        "--defer-state",
        str(tmp / "nowhere"),
        env=gate_env,
    )
    record(
        "blocking gate fails an incomplete review (execution asked for, not run)",
        r.returncode == 3 and "execution_not_run" in {i["kind"] for i in doc.get("incomplete", [])},
        f"exit {r.returncode}, incomplete={doc.get('incomplete')}",
    )

    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--no-llm",
        "--no-save",
        "--sarif",
        str(tmp / "r.sarif"),
        env={**env, "THEMIS_FAIL_ON_SEVERITY": "banana"},
    )
    record(
        "an unknown gate severity is refused, not ignored",
        r.returncode != 0 and not (tmp / "r.sarif").exists(),
        f"exit {r.returncode}",
    )

    print("\nreports")
    sarif, redacted_json, redacted_sarif = tmp / "full.sarif", tmp / "red.json", tmp / "red.sarif"
    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--no-llm",
        "--no-save",
        "--sarif",
        str(sarif),
        env=env,
    )
    ok = r.returncode == 0 and sarif.exists()
    if ok:
        log = json.loads(sarif.read_text())
        ok = (
            log["version"] == "2.1.0"
            and log["runs"][0]["results"]
            and log["runs"][0]["invocations"][0]["executionSuccessful"]
        )
    record("SARIF is written, with results and a successful invocation", ok, r.stderr[-200:])

    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--no-llm",
        "--no-save",
        "--redact",
        "--json",
        str(redacted_json),
        "--sarif",
        str(redacted_sarif),
        env={**env, "THEMIS_REDACT_SALT": "component-check"},
    )
    leaks: list[str] = []
    if r.returncode == 0 and redacted_json.exists() and redacted_sarif.exists():
        for path in (redacted_json, redacted_sarif):
            text = path.read_text().lower()
            leaks += [
                f"{path.name}:{s}"
                for s in ("stg_fx_rates", "int_gl_entries", "select ", "models/", "currency_code")
                if s in text
            ]
    record(
        "redacted reports carry no names or SQL",
        r.returncode == 0 and not leaks and redacted_json.exists(),
        f"leaks {leaks}",
    )


def check_execution(shas: dict[str, str], tmp: Path, env: dict[str, str]) -> None:
    print("\nexecution (Stage 3, Trino)")
    r, doc = review_json(shas["fanout"], tmp, "--no-llm", "--execute", env=env)
    measured = [f for f in doc.get("findings", []) if f["confidence"] == "measured"]
    deltas = {d["model"]: d for d in doc.get("execution_deltas", [])}
    revenue = deltas.get("fct_revenue", {})
    record(
        "fan-out measured: rows grow and findings are MEASURED",
        r.returncode == 0
        and doc.get("executed")
        and measured
        and (revenue.get("row_delta") or 0) > 0
        and not doc.get("incomplete"),
        f"exit {r.returncode}, measured={len(measured)}, "
        f"fct_revenue={revenue.get('rows_before')}->{revenue.get('rows_after')}",
    )
    leftover = run_schemas()
    record("no run schemas left behind, on Trino or DuckDB", not leftover, f"{leftover}")

    r = themis(
        "execute",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--explain",
        env=env,
    )
    record(
        "execute --explain shows what moved",
        r.returncode == 0 and "CHANGED" in r.stdout and "rows" in r.stdout,
        r.stderr[-300:],
    )

    r, doc = review_json(shas["seed_change"], tmp, "--no-llm", "--execute", env=env)
    x0001 = [f for f in doc.get("findings", []) if f["rule_id"] == "X0001"]
    record(
        "seed data change measured and owned by the seed",
        r.returncode == 0 and any(f["model"] == "raw_fx_rates" for f in x0001),
        f"X0001 on {[f['model'] for f in x0001]}",
    )

    state = PROJECT / "target"
    r, doc = review_json(
        shas["fanout"],
        tmp,
        "--no-llm",
        "--execute",
        "--prod-manifest",
        str(state),
        "--defer-state",
        str(state),
        env=env,
    )
    record(
        "production manifest + deferral: base read from prod, fan-out still measured",
        r.returncode == 0
        and doc.get("executed")
        and "F1001" in rules_in(doc)
        and not doc.get("degraded_reason"),
        f"exit {r.returncode}, degraded={doc.get('degraded_reason')}, {r.stderr[-300:]}",
    )
    record("no run schemas left after deferral", not run_schemas(), f"{run_schemas()}")


def check_snapshots(shas: dict[str, str], tmp: Path, env: dict[str, str]) -> None:
    print("\nsnapshots (Iceberg)")
    if not port_open(8085):
        skip("snapshot reviews with execution", "no Trino on 8085")
        return
    r, doc = review_json(shas["snapshot_filter"], tmp, "--no-llm", "--execute", env=env)
    deltas = {d["model"]: d for d in doc.get("execution_deltas", [])}
    snap = deltas.get("snap_contracts", {})
    record(
        "a filter on a snapshot that records deletions: F9005, and the rows it drops measured",
        "F9005" in rules_in(doc)
        and doc.get("executed")
        and (snap.get("rows_after") or 0) < (snap.get("rows_before") or 0),
        f"exit {r.returncode}, rules {sorted(rules_in(doc))}, "
        f"snap_contracts {snap.get('rows_before')}->{snap.get('rows_after')}",
    )
    leftover = run_schemas()
    record("no run schemas left in Hive or Iceberg", not leftover, f"{leftover}")

    r, doc = review_json(shas["snapshot_legacy_location"], tmp, "--no-llm", "--execute", env=env)
    reasons = " ".join(i.get("reason", "") for i in doc.get("incomplete", []))
    record(
        "a legacy target_schema is refused before anything is written",
        not doc.get("executed")
        and "outside this run's schemas" in reasons
        and "F9006" in rules_in(doc)
        and not trino_schema_exists("iceberg", "snapshots"),
        f"exit {r.returncode}, executed={doc.get('executed')}, {reasons[:300]}",
    )


def check_persistence_and_models(
    shas: dict[str, str], tmp: Path, env: dict[str, str], *, ollama: bool, quick: bool = False
) -> None:
    print("\npersistence, the model layer and ask")
    db_env = {**env, "THEMIS_DATABASE_URL": f"sqlite:///{tmp / 'history.db'}"}
    m = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**os.environ, **db_env, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    record("migrations apply to a fresh SQLite", m.returncode == 0, m.stderr[-300:])

    if not ollama:
        why = "--quick" if quick else "no Ollama on 11434"
        skip("review with the model layer", why)
        skip("ask answers from a stored review", why)
        return

    out = tmp / "llm.json"
    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--json",
        str(out),
        "--pr-description",
        "Tidy the FX conversion CTE. No behaviour change.",
        env=db_env,
    )
    doc = json.loads(out.read_text()) if out.exists() else {}
    layer = doc.get("model_layer") or {}
    record(
        "review with the model layer: calls made, description checked, run saved",
        r.returncode == 0 and layer.get("calls", 0) > 0 and "review.not_saved" not in r.stderr,
        f"exit {r.returncode}, model_layer={layer}, {r.stderr[-300:]}",
    )
    record(
        "intent names what the description left out",
        "Not mentioned in the description" in r.stdout,
        "no undisclosed changes reported for a description that claims no behaviour change",
    )

    r = themis("ask", "What did the review find about the FX join, and how sure is it?", env=db_env)
    record(
        "ask answers from the stored review, with a quote",
        r.returncode == 0 and "based on:" in r.stdout,
        f"exit {r.returncode}: {r.stdout[-300:]}",
    )
    r = themis("ask", "Was the model dim_customer_segments checked for duplicates?", env=db_env)
    record(
        "ask refuses about something the review never saw",
        r.returncode == 1,
        f"exit {r.returncode}: {r.stdout[-300:]}",
    )


def _dismiss_all(db: Path, rule_id: str) -> int:
    """Rule every stored finding of this rule dismissed, the way the API would.

    The API path for recording a disposition is checked in the service section. What
    this needs is the *state* a few weeks of use would leave behind, and the honest way
    to get it in one run is to write it.
    """
    import sqlite3

    connection = sqlite3.connect(db)
    with connection:
        cursor = connection.execute(
            "update finding set disposition = 'dismissed', "
            "disposition_note = 'the join key is unique by contract upstream', "
            "disposition_at = datetime('now') where rule_id = ? and disposition is null",
            (rule_id,),
        )
        changed = cursor.rowcount
    connection.close()
    return changed


def check_prior_art(shas: dict[str, str], tmp: Path, env: dict[str, str], *, ollama: bool) -> None:
    """What was adapted from other tools, exercised for real (docs/PRIOR_ART.md)."""
    print("\nadapted from prior art: paired rows, line placement, conventions")
    db_env = {**env, "THEMIS_DATABASE_URL": f"sqlite:///{tmp / 'prior_art.db'}"}

    # Paired rows: the case that used to come back clean.
    out = tmp / "recognition.json"
    r = themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["recognition"],
        "--execute",
        "--no-llm",
        "--no-save",
        "--json",
        str(out),
        env=db_env,
    )
    doc = json.loads(out.read_text()) if out.exists() else {}
    x0001 = [f for f in doc.get("findings", []) if f.get("rule_id") == "X0001"]
    note = (x0001[0].get("note") or "") if x0001 else ""
    record(
        "values that move with every row and total held are measured",
        bool(x0001) and "paired on (entry_id)" in note and "recognition_method" in note,
        f"exit {r.returncode}, X0001={len(x0001)}, note={note[:200]!r}",
    )
    revenue = next(
        (d for d in doc.get("execution_deltas", []) if d.get("model") == "fct_revenue"), {}
    )
    keyed = revenue.get("keyed") or {}
    record(
        "the rows held, the totals held, and the paired values did not",
        revenue.get("row_delta") == 0
        and keyed.get("rows_changed", 0) > 0
        and all(before == after for before, after in revenue.get("sum_deltas", {}).values()),
        f"fct_revenue={revenue}",
    )
    record(
        "a regulatory mart that did not move is not called a reported figure",
        bool(x0001) and x0001[0].get("severity") == "high",
        f"severity={x0001[0].get('severity') if x0001 else None}",
    )

    # Line placement: the fan-out annotation lands on the join, not on line 1.
    sarif_path = tmp / "placed.sarif"
    themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
        "--no-llm",
        "--no-save",
        "--sarif",
        str(sarif_path),
        env=db_env,
    )
    sarif = json.loads(sarif_path.read_text()) if sarif_path.exists() else {}
    placed: list[tuple[str, int]] = []
    for result in (sarif.get("runs") or [{}])[0].get("results", []):
        if result.get("ruleId") != "F1001":
            continue
        location = result["locations"][0]["physicalLocation"]
        placed.append((location["artifactLocation"]["uri"], int(location["region"]["startLine"])))
    lines_ok = bool(placed)
    detail = []
    for uri, line in placed:
        path = uri if uri.startswith("demo_project/") else f"demo_project/{uri}"
        text = git("show", f"{shas['fanout']}:{path}").splitlines()
        at = text[line - 1].strip().lower() if 0 < line <= len(text) else ""
        detail.append(f"{uri}:{line} {at!r}")
        lines_ok = lines_ok and line > 1 and ("join" in at or at.startswith("on "))
    record("findings are placed on the line they are about", lines_ok, "; ".join(detail))

    # Conventions: validated by the command, read at the reviewed commit.
    scratch = tmp / "conventions_project"
    scratch.mkdir(exist_ok=True)
    (scratch / "themis_conventions.yml").write_text(CONVENTIONS)
    c = themis("conventions", "--project", str(scratch), env=db_env)
    record(
        "conventions validate and key claims are pointed at tests",
        c.returncode == 0 and "declare it as a uniqueness test" in c.stdout,
        f"exit {c.returncode}: {c.stdout[-200:]}",
    )

    if not ollama:
        skip("a commit's conventions reach the specialist", "no Ollama on 11434")
        return
    m = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**os.environ, **db_env, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    themis(
        "review",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout_conventions"],
        env=db_env,
    )
    exported = tmp / "conventions_calls.jsonl"
    themis("dataset", "--out", str(exported), env=db_env)
    rows = (
        [json.loads(line) for line in exported.read_text().splitlines()]
        if exported.exists()
        else []
    )
    shown = [row for row in rows if "fx-rates-one-per-period" in row.get("context", "")]
    record(
        "a commit's conventions reach the specialist",
        m.returncode == 0 and bool(shown),
        f"{len(shown)} of {len(rows)} captured call(s) carried the convention",
    )


def check_volatile_values(shas: dict[str, str], tmp: Path, env: dict[str, str]) -> None:
    """Audit columns that differ between any two builds must not read as a change."""
    print("\nvalues that differ between any two builds")

    def reviewed(head: str, out: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = themis(
            "review",
            "--project",
            "demo_project",
            "--base",
            shas["audit_base"],
            "--head",
            head,
            "--execute",
            "--no-llm",
            "--no-save",
            "--json",
            str(out),
            env=env,
        )
        return result, (json.loads(out.read_text()) if out.exists() else {})

    def delta(doc: dict, model: str) -> dict:
        return next((d for d in doc.get("execution_deltas", []) if d.get("model") == model), {})

    quiet, doc = reviewed(shas["audit_comment"], tmp / "audit_comment.json")
    summary = delta(doc, "fct_account_period_summary").get("keyed") or {}
    record(
        "a comment upstream of audit columns raises nothing",
        quiet.returncode == 0 and doc.get("findings") == [],
        f"exit {quiet.returncode}, findings={[f.get('rule_id') for f in doc.get('findings', [])]}",
    )
    record(
        "the audit columns were found from the SQL and named, not compared",
        set(summary.get("volatile_columns", [])) == {"processed_at", "loaded_at", "batch_id"}
        and not summary.get("columns_changed"),
        f"keyed={summary}",
    )

    caught, doc = reviewed(shas["audit_recognition"], tmp / "audit_recognition.json")
    notes = " ".join((f.get("note") or "") for f in doc.get("findings", []))
    revenue = delta(doc, "fct_revenue").get("keyed") or {}
    record(
        "a real reclassification beside audit columns is still caught",
        "recognition_method" in notes and "processed_at" not in notes,
        f"exit {caught.returncode}, notes={notes[:200]!r}, keyed={revenue}",
    )


def mcp_session(env: dict[str, str]) -> tuple[bool, str]:
    """Drive `themis mcp` the way an IDE assistant does: handshake, list, call.

    The adapter's own functions are unit-tested and the SDK wiring has a live test; this
    is the third thing neither covers — the installed console command, serving a real
    compiled project, over a pipe a client opened.
    """
    import anyio
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def session() -> tuple[bool, str]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "themis.cli", "mcp", "--project", "demo_project"],
            env={**os.environ, "PYTHONPATH": str(SRC), **env},
            cwd=str(REPO),
        )
        async with (
            stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as client,
        ):
            with anyio.fail_after(120):
                await client.initialize()
                listed = await client.list_tools()
                answer = await client.call_tool("model_details", {"model": "fct_revenue"})
        text = answer.content[0].text if answer.content else ""
        ok = len(listed.tools) == 12 and not answer.is_error and "fct_revenue" in text
        return ok, f"{len(listed.tools)} tools; {text.splitlines()[0][:120] if text else 'no text'}"

    try:
        return anyio.run(session)  # type: ignore[arg-type]
    except Exception as exc:  # the point of the check is that this does not happen
        return False, f"{type(exc).__name__}: {exc}"


def check_agent_and_setup(
    shas: dict[str, str], tmp: Path, env: dict[str, str], *, ollama: bool
) -> None:
    """Setting a project up, and the agent answering from tools it must quote."""
    print("\nsetup and the agent")

    d = themis("doctor", "--project", "demo_project", env=env)
    record(
        "doctor finds nothing failing on the demo project",
        d.returncode == 0 and "0 failing" in d.stdout,
        d.stdout[-400:],
    )

    project = tmp / "init_project"
    workdir = tmp / "init_workdir"
    project.mkdir()
    workdir.mkdir()
    for name in ("dbt_project.yml", "profiles.yml"):
        shutil.copy(PROJECT / name, project / name)
    first = subprocess.run(
        [sys.executable, "-m", "themis.cli", "init", "--project", str(project)],
        cwd=workdir,
        env={**os.environ, **env, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    written = (workdir / ".env").read_text() if (workdir / ".env").exists() else ""
    allow = next(
        (line for line in written.splitlines() if line.startswith("THEMIS_EXECUTE_ALLOWED")), ""
    )
    record(
        "init writes an allowlist of non-production targets and a conventions template",
        first.returncode == 0
        and '"dev"' in allow
        and "prod" not in allow
        and (project / "themis_conventions.yml").exists(),
        f"exit {first.returncode}: {allow} {first.stdout[-200:]}",
    )
    (workdir / ".env").write_text("kept\n")
    subprocess.run(
        [sys.executable, "-m", "themis.cli", "init", "--project", str(project)],
        cwd=workdir,
        env={**os.environ, **env, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    record("init never overwrites", (workdir / ".env").read_text() == "kept\n")

    try:
        import mcp  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    if installed:
        # Never invoke `themis mcp` through themis(): with the SDK present it serves, and
        # a server reading an inherited stdin blocks until the timeout. Speak the protocol.
        served, detail = mcp_session(env)
        record("an MCP client lists the tools and gets a real answer", served, detail)
    else:
        m = themis("mcp", "--project", "demo_project", env=env, timeout=60)
        record(
            "mcp without the SDK says how to install it",
            m.returncode == 2 and "themis[mcp]" in m.stderr,
            f"exit {m.returncode}: {m.stderr[-200:]}",
        )

    if not ollama:
        skip("the agent answers a project question from a tool it quotes", "no Ollama on 11434")
        skip("the agent answers about a review from its findings", "no Ollama on 11434")
        return

    def ask(*arguments: str) -> dict:
        result = themis("agent", *arguments, "--json", env=env)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"exit": result.returncode, "stdout": result.stdout[-300:]}

    grain = ask("What is the grain of fct_account_period_summary?", "--project", "demo_project")
    record(
        "the agent answers a project question from a tool it quotes",
        bool(grain.get("grounded"))
        and "period_month" in (grain.get("answer") or "")
        and any(c.get("tool") == "grain" for c in grain.get("citations", [])),
        str(grain)[:300],
    )
    finding = ask(
        "Which rule fired on int_gl_entries_converted in this review?",
        "--project",
        "demo_project",
        "--base",
        "HEAD",
        "--head",
        shas["fanout"],
    )
    record(
        "the agent answers about a review from its findings",
        bool(finding.get("grounded")) and "F1001" in (finding.get("answer") or ""),
        str(finding)[:300],
    )
    refused = ask("Who is the business owner of fct_revenue?", "--project", "demo_project")
    record(
        "the agent refuses what no tool can answer",
        refused.get("grounded") is False and refused.get("refusal_reason"),
        str(refused)[:300],
    )


def check_learning_loop(
    shas: dict[str, str], tmp: Path, env: dict[str, str], *, ollama: bool
) -> None:
    """A finding is raised, a reviewer rules on it, and the next review knows.

    Every part of this is unit-tested; none of that proves the four pieces are wired to
    each other. The loop only exists if a disposition written through one component
    reaches the ranking, the specialist's pack and the exported dataset in another.
    """
    print("\nthe learning loop")
    db = tmp / "loop.db"
    db_env = {**env, "THEMIS_DATABASE_URL": f"sqlite:///{db}"}
    m = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env={**os.environ, **db_env, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    record("migrations create the model-call table", m.returncode == 0, m.stderr[-300:])

    def fanout_review(out: Path, *extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        result = themis(
            "review",
            "--project",
            "demo_project",
            "--base",
            "HEAD",
            "--head",
            shas["fanout"],
            "--json",
            str(out),
            *extra,
            env=db_env,
        )
        return result, (json.loads(out.read_text()) if out.exists() else {})

    first, doc = fanout_review(tmp / "loop1.json", "--no-llm")
    before = {f["rule_id"]: f["triage"]["score"] for f in doc.get("findings", [])}
    record(
        "a first review stores its findings with no history",
        "F1001" in before and all(f["history"] is None for f in doc.get("findings", [])),
        f"exit {first.returncode}, rules={sorted(before)}",
    )

    # Two earlier runs, both dismissed: one judgement is an opinion, two are a pattern.
    fanout_review(tmp / "loop2.json", "--no-llm")
    dismissed = _dismiss_all(db, "F1001")
    record(
        "dispositions are recorded against the stored findings",
        dismissed >= 2,
        f"{dismissed} rows",
    )

    third, doc = fanout_review(tmp / "loop3.json", "--no-llm")
    after = {f["rule_id"]: f["triage"]["score"] for f in doc.get("findings", [])}
    history = next((f["history"] for f in doc.get("findings", []) if f["rule_id"] == "F1001"), None)
    record(
        "the next review reads the judgements back",
        bool(history) and history.get("dismissed", 0) >= 2,
        f"history={history}",
    )
    record(
        "a repeatedly dismissed finding ranks lower than it did",
        "F1001" in after and after["F1001"] < before.get("F1001", 0),
        f"{before.get('F1001')} -> {after.get('F1001')}",
    )
    record(
        "it is still in the report, and says why it moved",
        "F1001" in after and "Seen before:" in third.stdout and "dismissed" in third.stdout,
        f"rules={sorted(after)}",
    )

    if not ollama:
        skip("past judgements reach the specialist", "no Ollama on 11434")
        skip("every model call is captured with its context", "no Ollama on 11434")
        return

    fanout_review(tmp / "loop4.json")
    exported = tmp / "dataset.jsonl"
    d = themis("dataset", "--out", str(exported), env=db_env)
    rows = (
        [json.loads(line) for line in exported.read_text().splitlines()]
        if exported.exists()
        else []
    )
    record(
        "every model call is captured with its context",
        d.returncode == 0 and len(rows) > 0 and all(r["context"] and r["system"] for r in rows),
        f"exit {d.returncode}, {len(rows)} call(s), {d.stdout[-200:]}",
    )
    precedent = [r for r in rows if "How reviewers ruled on findings like this one" in r["context"]]
    record(
        "past judgements reach the specialist",
        bool(precedent) and any("unique by contract" in r["context"] for r in precedent),
        f"{len(precedent)} of {len(rows)} packs carried precedent",
    )
    record(
        "the export joins each call to the judgement that settled it",
        any(r["human_disposition"] == "dismissed" for r in rows),
        f"dispositions={sorted({str(r['human_disposition']) for r in rows})}",
    )
    judged = themis("dataset", "--judged-only", env=db_env)
    record(
        "the dataset says how far it is from being enough to tune on",
        judged.returncode == 0 and "Too few to tune on" in judged.stdout,
        judged.stdout[-200:],
    )


def check_service(shas: dict[str, str], tmp: Path, *, ollama: bool = False) -> None:
    print("\nservice: Postgres, API, worker, pages")
    if not port_open(5436):
        for name in ("migrations on Postgres", "API", "worker", "pages"):
            skip(name, "no Postgres on 5436 (make up)")
        return

    from sqlalchemy import create_engine, text

    admin = create_engine(PG_ADMIN, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"drop database if exists {PG_CHECK_DB}"))
        conn.execute(text(f"create database {PG_CHECK_DB}"))
    url = PG_ADMIN.rsplit("/", 1)[0] + f"/{PG_CHECK_DB}"
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "THEMIS_DATABASE_URL": url,
        "THEMIS_API_TOKEN": "check-token",
    }
    api: subprocess.Popen[str] | None = None
    try:
        m = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        record("migrations apply to Postgres", m.returncode == 0, m.stderr[-300:])

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "themis.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        import httpx

        base = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        health = httpx.get(f"{base}/health", timeout=5).json()
        record("API health reports the database", health.get("database") is True, f"{health}")

        auth = {"Authorization": "Bearer check-token"}
        body = {
            "project": "demo_project",
            "base_ref": "HEAD",
            "head_ref": shas["fanout"],
            "execute": True,
        }
        record(
            "API refuses a request without the token",
            httpx.post(f"{base}/reviews", json=body, timeout=5).status_code == 401,
        )
        record(
            "API refuses a revision git would read as an option",
            httpx.post(
                f"{base}/reviews",
                json={**body, "head_ref": "--output=/tmp/x"},
                headers=auth,
                timeout=5,
            ).status_code
            == 422,
        )
        record(
            "API refuses a project outside the roots",
            httpx.post(
                f"{base}/reviews", json={**body, "project": "../elsewhere"}, headers=auth, timeout=5
            ).status_code
            == 422,
        )
        created = httpx.post(f"{base}/reviews", json=body, headers=auth, timeout=5)
        key = created.json().get("run_key")
        record("API queues a review", created.status_code == 202 and bool(key), created.text[-200:])

        worker = (
            "from themis.worker import serve; "
            "from themis.capabilities import parse_capabilities; "
            "serve(once=True, capabilities=parse_capabilities({caps!r}))"
        )
        w = subprocess.run(
            [sys.executable, "-c", worker.format(caps="analyse")],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        record(
            "a worker that cannot compile refuses to start",
            w.returncode != 0 and "compile" in w.stderr,
            f"exit {w.returncode}",
        )

        w = subprocess.run(
            [sys.executable, "-c", worker.format(caps="analyse,compile,review")],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        status = httpx.get(f"{base}/reviews/{key}", headers=auth, timeout=5).json()["status"]
        record(
            "a worker without execute leaves an execution run queued",
            w.returncode == 0 and status == "queued",
            f"status {status}",
        )

        w = subprocess.run(
            [sys.executable, "-c", worker.format(caps="all")],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
        detail = httpx.get(f"{base}/reviews/{key}", headers=auth, timeout=10).json()
        rules = {f["rule_id"] for f in detail.get("findings", [])}
        record(
            "a capable worker runs it: succeeded, executed, fan-out found",
            detail.get("status") == "succeeded" and detail.get("executed") and "F1001" in rules,
            f"status={detail.get('status')} executed={detail.get('executed')} "
            f"rules={sorted(rules)} error={detail.get('error')} {w.stderr[-300:]}",
        )

        finding = next((f for f in detail.get("findings", []) if f["rule_id"] == "F1001"), None)
        if finding is not None:
            d = httpx.post(
                f"{base}/findings/{finding['id']}/disposition",
                json={"disposition": "accepted", "note": "real fan-out"},
                headers=auth,
                timeout=5,
            )
            record(
                "a disposition is recorded",
                d.status_code == 200 and d.json()["disposition"] == "accepted",
                d.text[-200:],
            )
        history = httpx.get(f"{base}/models/fct_revenue/deltas", headers=auth, timeout=5)
        record(
            "model history is served",
            history.status_code == 200 and history.json(),
            history.text[-200:],
        )
        check_pages(base, key, finding, ollama=ollama)
    finally:
        if api is not None:
            api.terminate()
            api.wait(timeout=10)
        with admin.connect() as conn:
            conn.execute(text(f"drop database if exists {PG_CHECK_DB} with (force)"))


def check_pages(base: str, key: str, finding: dict[str, Any] | None, *, ollama: bool) -> None:
    """The pages, against the review the worker just stored on real Postgres.

    What a unit test cannot see: that a review stored by a real worker keeps the snapshots
    the chat needs, that the API's decision and the page's decision land in one record, and
    that the chat streams from a real model.
    """
    import httpx

    overview = httpx.get(f"{base}/ui", timeout=10)
    record(
        "pages: overview renders under its security policy",
        overview.status_code == 200
        and "script-src 'self'" in overview.headers.get("content-security-policy", ""),
        f"status {overview.status_code}",
    )
    page = httpx.get(f"{base}/ui/pr/{key}", timeout=10)
    record(
        "pages: the stored review renders, with its snapshots for the chat",
        page.status_code == 200
        and "F1001" in page.text
        and "stored without its project snapshots" not in page.text,
        f"status {page.status_code}",
    )
    record(
        "pages: the API's decision is in the record, authored as api",
        finding is not None and ">api<" in httpx.get(f"{base}/ui/decisions", timeout=10).text,
    )
    if finding is not None:
        refused = httpx.post(
            f"{base}/ui/findings/{finding['id']}/decision",
            json={"disposition": "fixed"},
            cookies={"themis_user": "check"},
            timeout=5,
        )
        decided = httpx.post(
            f"{base}/ui/findings/{finding['id']}/decision",
            json={"disposition": "fixed", "note": "component check"},
            cookies={"themis_user": "check"},
            headers={"X-Themis-UI": "1"},
            timeout=5,
        )
        record(
            "pages: a decision needs the page's header, then lands with its author",
            refused.status_code == 403
            and decided.status_code == 200
            and decided.json().get("actor") == "check",
            f"{refused.status_code} {decided.text[-160:]}",
        )
    if not ollama:
        skip("pages: the chat streams a grounded answer from the local model", "no Ollama")
        return
    with httpx.stream(
        "POST",
        f"{base}/ui/pr/{key}/chat",
        json={"question": "What does rule F1001 check for?"},
        headers={"X-Themis-UI": "1"},
        timeout=httpx.Timeout(10, read=320),
    ) as stream:
        events = [json.loads(line[6:]) for line in stream.iter_lines() if line.startswith("data: ")]
    kinds = [e.get("type") for e in events]
    record(
        "pages: the chat streams a grounded answer from the local model",
        "step" in kinds and kinds[-1] in ("answer", "refusal"),
        f"events {kinds} last {events[-1] if events else None}",
    )


def check_trino() -> None:
    print("\nTrino")
    if not port_open(8085):
        skip("review with execution on Trino", "no Trino on 8085")
        return
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "trino_smoke.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    record(
        "review with execution on Trino (scripts/trino_smoke.py)",
        r.returncode == 0 and "pass" in r.stdout,
        r.stdout[-400:],
    )


def _why(result: subprocess.CompletedProcess[str], chars: int) -> str:
    """A failing check has to say what happened.

    Both corpus checks once failed with a blank detail: the harness refuses a dirty
    working tree and says so on stderr, which nothing was reading. The reason for a
    failure is exactly what a check exists to hand back.
    """
    return (result.stdout[-chars:] + result.stderr[-chars:]).strip()


def check_eval() -> None:
    print("\ncorpus harness")
    r = themis("eval", "--mutations", "defects", "--no-execute", timeout=1800)
    record(
        "corpus without execution: gate passes on the defects",
        r.returncode == 0 and "gate: pass" in r.stdout,
        _why(r, 500),
    )
    r = themis(
        "eval",
        "--mutations",
        "generated",
        "--generated-limit",
        "8",
        "--generated-seed",
        "1",
        timeout=2400,
    )
    missed = "MISSED" in r.stdout
    record(
        "generated mutations run, and nothing that moved went unreported",
        r.returncode == 0 and not missed,
        _why(r, 600),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--quick", action="store_true", help="skip the model layer, corpus subsets and Trino"
    )
    args = parser.parse_args()

    if git("status", "--porcelain"):
        print("The working tree has uncommitted changes; the corpus checks measure committed code.")
    if not (PROJECT / "target" / "manifest.json").exists():
        print("demo_project has no manifest — run `make demo-build` first.")
        return 2
    # Recompiled, whatever is on disk. A manifest written by `dbt build --select m` has
    # compiled SQL for m alone, and the checks that read it — lineage, the agent, the
    # production-manifest review — would fail on that and look like regressions.
    compiled = subprocess.run(
        [
            str(Path(sys.executable).parent / "dbt"),
            "compile",
            "--profiles-dir",
            ".",
            "--project-dir",
            ".",
        ],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        check=False,
    )
    if compiled.returncode != 0:
        print("demo_project does not compile — is Trino up (`make up`) and the demo built?")
        print(compiled.stdout[-1500:])
        return 2

    ollama = port_open(11434)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="themis-check-") as tmp_name:
        tmp = Path(tmp_name)
        env = {"THEMIS_DATABASE_URL": f"sqlite:///{tmp / 'cli.db'}"}
        print("building scenarios on top of HEAD")
        shas = build_scenarios(tmp)

        check_analysis(env)
        check_reviews(shas, tmp, env)
        check_execution(shas, tmp, env)
        check_snapshots(shas, tmp, env)
        check_persistence_and_models(
            shas, tmp, env, ollama=ollama and not args.quick, quick=args.quick
        )
        check_prior_art(shas, tmp, env, ollama=ollama and not args.quick)
        check_volatile_values(shas, tmp, env)
        check_agent_and_setup(shas, tmp, env, ollama=ollama and not args.quick)
        check_learning_loop(shas, tmp, env, ollama=ollama and not args.quick)
        check_service(shas, tmp, ollama=ollama and not args.quick)
        if args.quick:
            skip("Trino", "--quick")
            skip("corpus harness", "--quick")
        else:
            check_trino()
            check_eval()
        shutil.rmtree(tmp / "scenarios", ignore_errors=True)

    passed = sum(r.status == "PASS" for r in RESULTS)
    failed = [r for r in RESULTS if r.status == "FAIL"]
    skipped = [r for r in RESULTS if r.status == "SKIP"]
    print(
        f"\n{passed} passed, {len(failed)} failed, {len(skipped)} skipped "
        f"in {time.monotonic() - started:.0f}s"
    )
    for r in failed:
        print(f"  FAIL {r.name}: {r.detail}")
    for r in skipped:
        print(f"  SKIP {r.name}: {r.detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
