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
    make up                                           # Postgres on 5436   (optional)
    docker run -d -p 8085:8080 trinodb/trino:latest   # Trino              (optional)
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


def build_scenarios(tmp: Path) -> dict[str, str]:
    """Commit one change per scenario on top of HEAD. Returns name -> commit SHA."""
    from themis.eval.mutations import select

    tree = tmp / "scenarios"
    base = git("rev-parse", "HEAD")
    git("worktree", "add", "--detach", "--quiet", str(tree), base)
    shas: dict[str, str] = {}

    def commit(name: str, change: Callable[[Path], None]) -> None:
        git("checkout", "--detach", "--quiet", base, cwd=tree)
        change(tree / "demo_project")
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
        path = project / "dbt_project.yml"
        text = path.read_text()
        assert "    intermediate:\n      +materialized: view" in text
        path.write_text(
            text.replace(
                "    intermediate:\n      +materialized: view",
                "    intermediate:\n      +materialized: table",
            )
        )

    try:
        commit("fanout", mutation("fanout_drop_join_predicate"))
        commit("macro_double", mutation("money_cast_to_double"))
        commit("alias_refactor", mutation("control_rename_alias_in_filter"))
        commit("seed_change", edit_seed)
        commit("project_config", edit_project_config)
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
        import duckdb
        import yaml

        doc = yaml.safe_load(r.stdout)
        conn = duckdb.connect(str(PROJECT / "themis_demo.duckdb"), read_only=True)
        conn.execute(f"attach '{PROJECT / 'reference.duckdb'}' as reference (read_only)")
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
                    f"{c}.{s}.{name}"
                    for c, s in (("themis_demo", "main"), ("reference", "main_main"))
                    if conn.execute(
                        "select count(*) from information_schema.tables "
                        f"where table_catalog='{c}' and table_schema='{s}' and table_name='{name}'"
                    ).fetchone()[0]
                ),
                None,
            )
            if relation is None or not columns:
                failed += 1
                continue
            key = ", ".join(columns)
            rows, distinct = conn.execute(
                f"select count(*), count(distinct ({key})) from {relation}"
            ).fetchone()
            held += int(rows == distinct)
            failed += int(rows != distinct)
        conn.close()
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
        "dbt_project.yml edit reaches the models it reconfigured",
        r.returncode == 0 and {"int_gl_entries_converted", "int_revenue_recognized"} <= reviewed,
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
    print("\nexecution (Stage 3, DuckDB)")
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
    leftover = duckdb_run_schemas()
    record("no run schemas left in either DuckDB file", not leftover, f"{leftover}")

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
    record(
        "no run schemas left after deferral", not duckdb_run_schemas(), f"{duckdb_run_schemas()}"
    )


def check_persistence_and_models(
    shas: dict[str, str], tmp: Path, env: dict[str, str], *, ollama: bool
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
        skip("review with the model layer", "no Ollama on 11434")
        skip("ask answers from a stored review", "no Ollama on 11434")
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


def check_service(shas: dict[str, str], tmp: Path) -> None:
    print("\nservice: Postgres, API, worker")
    if not port_open(5436):
        for name in ("migrations on Postgres", "API", "worker"):
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
    finally:
        if api is not None:
            api.terminate()
            api.wait(timeout=10)
        with admin.connect() as conn:
            conn.execute(text(f"drop database if exists {PG_CHECK_DB} with (force)"))


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


def check_eval() -> None:
    print("\ncorpus harness")
    r = themis("eval", "--mutations", "defects", "--no-execute", timeout=1800)
    record(
        "corpus without execution: gate passes on the defects",
        r.returncode == 0 and "gate: pass" in r.stdout,
        r.stdout[-500:],
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
        r.stdout[-600:],
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
        check_persistence_and_models(shas, tmp, env, ollama=ollama and not args.quick)
        check_service(shas, tmp)
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
