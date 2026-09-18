"""Getting THEMIS running on a real project: what is missing, and exactly how to fix it.

Every step of setting THEMIS up on a new machine fails in a way that surfaces much later
and much less clearly — a profile in ~/.dbt that dbt was never told about, a target name
the allowlist refuses, an adapter that is not installed, a model that was never pulled, a
database never migrated. Each became a failed review with a message about something else.

`themis doctor` checks them all up front and says what to run. `themis init` writes the
configuration a project needs, without overwriting anything and without ever adding a
production-looking target to the allowlist.
"""

from __future__ import annotations

import importlib.util
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import yaml

from themis.config import Settings

Status = Literal["ok", "warn", "fail", "skip"]

_ADAPTER_MODULES = {"trino": "dbt.adapters.trino", "duckdb": "dbt.adapters.duckdb"}
_ADAPTER_INSTALL = {
    "trino": 'uv pip install "dbt-trino>=1.10,<1.11"',
    "duckdb": "uv pip install dbt-duckdb",
}
# A target whose name says production is never proposed for the allowlist.
_PRODUCTION_WORDS = ("prod", "prd", "live", "production")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str | None = None


def _project_profile(project: Path) -> tuple[str | None, dict[str, object] | None, Path | None]:
    from themis.execute.profiles import project_profile_name, resolve_profiles_dir

    try:
        name = project_profile_name(project)
    except Exception:
        return None, None, None
    directory = resolve_profiles_dir(project)
    path = directory / "profiles.yml"
    if not path.exists():
        return name, None, None
    document = yaml.safe_load(path.read_text()) or {}
    block = document.get(name)
    return name, block if isinstance(block, dict) else None, path


def _check_python() -> Check:
    ok = sys.version_info >= (3, 12)
    return Check(
        "python",
        "ok" if ok else "fail",
        f"Python {sys.version_info.major}.{sys.version_info.minor}",
        None if ok else "THEMIS needs Python 3.12 or newer: conda create -n themis python=3.12",
    )


def _check_dbt() -> Check:
    from themis.acquire.dbt_runner import DbtError, dbt_executable

    try:
        executable = dbt_executable()
    except DbtError:
        return Check("dbt", "fail", "dbt is not installed here", "uv pip install dbt-core")
    try:
        output = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=60, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("dbt", "fail", f"dbt did not run: {exc}", None)
    version = next(
        (line.strip().lstrip("- ").strip() for line in output.splitlines() if "installed" in line),
        "",
    )
    return Check("dbt", "ok", version or executable)


def _check_project(project: Path) -> Check:
    if not (project / "dbt_project.yml").exists():
        return Check(
            "dbt project",
            "fail",
            f"no dbt_project.yml in {project}",
            "pass --project pointing at the directory that holds dbt_project.yml",
        )
    return Check("dbt project", "ok", str(project))


def _check_profile(project: Path, target: str) -> list[Check]:
    name, block, path = _project_profile(project)
    if name is None:
        return [Check("profile", "skip", "no dbt project to read a profile name from")]
    if path is None or block is None:
        return [
            Check(
                "profile",
                "fail",
                f"no profile named {name!r} in the project, DBT_PROFILES_DIR or ~/.dbt",
                "create profiles.yml where dbt looks for it, or set DBT_PROFILES_DIR",
            )
        ]
    outputs = block.get("outputs") if isinstance(block.get("outputs"), dict) else {}
    assert isinstance(outputs, dict)
    checks = [Check("profile", "ok", f"{name!r} in {path} (targets: {', '.join(outputs)})")]
    output = outputs.get(target)
    if not isinstance(output, dict):
        checks.append(
            Check(
                "target",
                "fail",
                f"profile {name!r} has no target {target!r}",
                f"pass --target with one of: {', '.join(outputs)}",
            )
        )
        return checks
    adapter = str(output.get("type", "unknown"))
    module = _ADAPTER_MODULES.get(adapter)
    if module is not None and importlib.util.find_spec(module) is None:
        checks.append(
            Check(
                "adapter",
                "fail",
                f"target {target!r} uses {adapter}, whose dbt adapter is not installed",
                _ADAPTER_INSTALL.get(adapter),
            )
        )
    else:
        checks.append(Check("adapter", "ok", f"target {target!r} uses {adapter}"))
    return checks


def _check_allowlist(settings: Settings, target: str) -> Check:
    if target in settings.execute_allowed_targets:
        return Check("target allowlist", "ok", f"{target!r} is allowed")
    looks_production = any(word in target.lower() for word in _PRODUCTION_WORDS)
    if looks_production:
        return Check(
            "target allowlist",
            "fail",
            f"{target!r} looks like production and is refused — as it should be",
            "use a non-production target; THEMIS never runs against production",
        )
    return Check(
        "target allowlist",
        "fail",
        f"{target!r} is not in THEMIS_EXECUTE_ALLOWED_TARGETS, so every dbt call is refused",
        f"if {target!r} is certainly not production: "
        f"THEMIS_EXECUTE_ALLOWED_TARGETS='[\"{target}\"]' (or run `themis init`)",
    )


def _check_git(project: Path) -> Check:
    from themis.acquire import git

    try:
        root = git.repo_root(project)
    except git.GitError:
        return Check(
            "git", "fail", f"{project} is not in a git repository", "reviews compare git revisions"
        )
    clean = git.is_clean(root, project)
    return Check(
        "git",
        "ok" if clean else "warn",
        f"repository {root}" + ("" if clean else " — uncommitted changes in the project"),
        None if clean else "reviewing HEAD includes them; commit to review a revision exactly",
    )


def _check_manifest(project: Path) -> Check:
    path = project / "target" / "manifest.json"
    if not path.exists():
        return Check(
            "compiled manifest",
            "warn",
            "no target/manifest.json — reviews compile their own, but profile, lineage, "
            "agent --manifest and mcp read this one",
            f"(cd {project} && dbt compile)",
        )
    return Check("compiled manifest", "ok", str(path))


def _check_model(settings: Settings) -> list[Check]:
    base = settings.llm_base_url.rstrip("/")
    try:
        tags = httpx.get(f"{base}/api/tags", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return [
            Check(
                "local model",
                "warn",
                f"no Ollama at {base} — reviews still run, without the model layer (--no-llm)",
                "install Ollama and start it, or keep using --no-llm",
            )
        ]
    names = {str(m.get("name")) for m in tags.get("models", []) if isinstance(m, dict)}
    checks: list[Check] = []
    for model in sorted({settings.llm_specialist_model, settings.llm_supervisor_model}):
        present = model in names or f"{model}:latest" in names
        checks.append(
            Check(
                f"model {model}",
                "ok" if present else "warn",
                "pulled" if present else "not pulled",
                None if present else f"ollama pull {model}",
            )
        )
        if present:
            try:
                info = httpx.post(f"{base}/api/show", json={"model": model}, timeout=10).json()
                trained = next(
                    (
                        int(v)
                        for k, v in (info.get("model_info") or {}).items()
                        if k.endswith("context_length")
                    ),
                    None,
                )
            except (httpx.HTTPError, ValueError):
                trained = None
            if trained is not None and settings.llm_context_window > trained:
                checks.append(
                    Check(
                        "context window",
                        "warn",
                        f"THEMIS_LLM_CONTEXT_WINDOW={settings.llm_context_window} exceeds the "
                        f"{trained} {model} was trained on",
                        f"THEMIS_LLM_CONTEXT_WINDOW={trained}",
                    )
                )
    return checks


def _check_database(settings: Settings) -> Check:
    try:
        from alembic.config import Config
        from alembic.runtime.migration import MigrationContext
        from alembic.script import ScriptDirectory
        from sqlalchemy import create_engine

        repo = Path(__file__).resolve().parents[2]
        config = Config(str(repo / "alembic.ini"))
        config.set_main_option("script_location", str(repo / "migrations"))
        head = ScriptDirectory.from_config(config).get_current_head()
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:
        return Check(
            "database",
            "warn",
            f"could not check {settings.database_url}: {type(exc).__name__}",
            "history, ask and dataset need it; reviews run without it",
        )
    if current == head:
        return Check("database", "ok", f"migrated to {head}")
    return Check(
        "database",
        "warn",
        f"at {current or 'no migrations'}, latest is {head}",
        "make migrate  (or: alembic upgrade head)",
    )


def _check_conventions(project: Path) -> Check:
    from themis import conventions

    loaded = conventions.load(project)
    if not loaded.conventions and not loaded.rejected:
        return Check("conventions", "skip", "none written (optional)")
    if loaded.rejected:
        reasons = "; ".join(f"{label}: {reason}" for label, reason in loaded.rejected)
        return Check("conventions", "fail", reasons, f"themis conventions --project {project}")
    return Check("conventions", "ok", f"{len(loaded.conventions)} loaded")


def _check_redaction(settings: Settings) -> Check:
    if settings.redact_salt:
        return Check("redaction salt", "ok", "set")
    return Check(
        "redaction salt",
        "warn",
        "THEMIS_REDACT_SALT is empty — redacted names could be matched by hashing guesses",
        "set it before sharing any --redact output (themis init generates one)",
    )


def _check_mcp() -> Check:
    """Whether `themis mcp` can serve — optional, so absence is a skip and never a fail."""
    if importlib.util.find_spec("mcp") is None:
        return Check(
            "mcp server",
            "skip",
            "the optional SDK is not installed (only needed to serve an MCP client)",
            "uv pip install 'themis[mcp]'",
        )
    return Check("mcp server", "ok", "SDK present; results carry SQL, connect a local-model client")


def run_checks(project: Path, settings: Settings, *, target: str) -> list[Check]:
    project = project.resolve()
    checks = [_check_python(), _check_dbt(), _check_project(project)]
    checks += _check_profile(project, target)
    checks += [_check_allowlist(settings, target), _check_git(project), _check_manifest(project)]
    checks += _check_model(settings)
    checks += [_check_database(settings), _check_conventions(project), _check_redaction(settings)]
    checks.append(_check_mcp())
    return checks


# --- init ------------------------------------------------------------------------------------

CONVENTIONS_TEMPLATE = """\
# What this project's reviewers know, for THEMIS's specialists. Context, never evidence.
# Each entry needs a condition, the guidance, and what it implies; scope with rules/models.
# Check with: themis conventions --project .
conventions: []
#  - id: fx-rates-one-per-period
#    rules: [F1001]
#    models: ["int_*"]
#    condition: A join onto stg_fx_rates in an intermediate model.
#    guidance: stg_fx_rates holds one row per currency per month, by contract with treasury.
#    implication: A join onto it multiplies rows unless currency and period are both matched.
#    owner: data-platform
"""


def safe_targets(project: Path) -> list[str]:
    """The profile's targets whose names do not say production."""
    _, block, _ = _project_profile(project.resolve())
    outputs = block.get("outputs") if block else None
    if not isinstance(outputs, dict):
        return []
    return [
        name for name in outputs if not any(word in str(name).lower() for word in _PRODUCTION_WORDS)
    ]


def env_file(project: Path) -> str:
    targets = safe_targets(project)
    allow = ", ".join(f'"{t}"' for t in targets)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"""# THEMIS configuration for {project.resolve()} — written by `themis init` on {stamp}.
# Contains a secret (THEMIS_REDACT_SALT). Keep this file out of version control.

# dbt targets THEMIS may compile and build against. Proposed from the profile's targets
# whose names do not say production — CONFIRM none of them is production before relying
# on this. THEMIS refuses every target not listed here.
THEMIS_EXECUTE_ALLOWED_TARGETS=[{allow}]

# Hashes model and column names in --redact output. Keep it private.
THEMIS_REDACT_SALT={secrets.token_hex(16)}

# The names checks match on. Uncomment and replace with this project's conventions;
# `themis profile --json` shows how often the defaults match.
# THEMIS_MONEY_COLUMN_HINTS=["amount","ntnl","mtm","pnl","balance"]
# THEMIS_SENSITIVE_COLUMN_HINTS=["email","phone","ssn","dob","iban"]
# THEMIS_GOVERNED_TAGS=["regulatory","recon","control"]

# Load-time columns the SQL cannot show as volatile (stamped upstream of dbt).
# THEMIS_EXECUTE_KEYED_IGNORE_COLUMNS=["_loaded_at","etl_load_ts"]

# The local model and its context window.
# THEMIS_LLM_SPECIALIST_MODEL=qwen3:8b
# THEMIS_LLM_CONTEXT_WINDOW=16384

# Where reviews, dispositions and captured model calls are stored.
# THEMIS_DATABASE_URL=sqlite:///data/themis.db
"""


@dataclass(frozen=True)
class Written:
    path: Path
    created: bool
    note: str


def initialise(project: Path, destination: Path) -> list[Written]:
    """Write .env and a conventions template. Never overwrites an existing file."""
    results: list[Written] = []
    env_path = destination / ".env"
    if env_path.exists():
        results.append(Written(env_path, False, "exists; left untouched"))
    else:
        env_path.write_text(env_file(project))
        results.append(Written(env_path, True, _ignored_note(env_path)))

    conventions_path = project / "themis_conventions.yml"
    if conventions_path.exists():
        results.append(Written(conventions_path, False, "exists; left untouched"))
    else:
        conventions_path.write_text(CONVENTIONS_TEMPLATE)
        results.append(Written(conventions_path, True, "empty template; commit it with the models"))
    return results


def _ignored_note(path: Path) -> str:
    if shutil.which("git") is None:
        return "check it is not committed"
    result = subprocess.run(
        ["git", "-C", str(path.parent), "check-ignore", "-q", str(path)],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return "ignored by git"
    if result.returncode == 1:
        return "NOT ignored by git — add .env to .gitignore before committing anything"
    return "not in a git repository"
