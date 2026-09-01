"""Configuration. All settings via ``THEMIS_*`` env vars, a ``.env`` file, or CLI flags."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Defaults are the free local profile: Ollama on the loopback, DuckDB under the demo
    project, no execution unless asked. Nothing here should ever require a paid service.
    """

    model_config = SettingsConfigDict(env_prefix="THEMIS_", env_file=".env", extra="ignore")

    # --- dialect -------------------------------------------------------------
    # Starburst is Trino. This drives sqlglot parsing everywhere, independently of
    # whatever engine actually executes the demo project.
    dialect: str = "trino"

    # --- LLM -----------------------------------------------------------------
    llm_provider: str = "ollama"  # ollama | openai_compatible
    llm_base_url: str = "http://127.0.0.1:11434"
    # High-volume, narrow, JSON-schema'd specialist calls.
    llm_specialist_model: str = "qwen3:8b"
    # Low-volume calls that need actual judgement.
    llm_supervisor_model: str = "qwen3:30b"
    llm_timeout_s: float = 120.0
    # Context packs are kept small on purpose; the LLM never sees a whole file.
    llm_max_context_tokens: int = 2000

    # --- execution (Stage 3) -------------------------------------------------
    execute_enabled: bool = False
    # Schemas the base and head builds land in. Never production.
    execute_base_schema: str = "themis_base"
    execute_head_schema: str = "themis_head"
    execute_timeout_s: float = 900.0
    # Skip models above this many rows rather than blowing the time budget.
    execute_max_rows: int = 5_000_000
    # Any dbt target whose name is not in this set is refused outright. The guard is
    # deliberately an allowlist: a typo must fail closed, not run against prod.
    execute_allowed_targets: tuple[str, ...] = ("dev", "ci", "duckdb", "test", "local")

    # --- gate ----------------------------------------------------------------
    # Advisory by default. Blocking is opt-in, per severity.
    fail_on_severity: str | None = None

    # --- artifacts -----------------------------------------------------------
    run_dir: str = ".themis/runs"

    # --- persistence and service ---------------------------------------------
    # SQLite by default so the CLI and the tests need no container. Production is
    # Postgres; docker-compose brings it up on 5436, clear of the sibling projects.
    database_url: str = "sqlite:///data/themis.db"
    # How long a claimed run may go without a heartbeat before another worker may
    # reclaim it. Stage 3 builds are slow, so this is generous.
    worker_claim_timeout_s: float = 1800.0
    worker_poll_interval_s: float = 5.0
    api_host: str = "127.0.0.1"
    api_port: int = 8040


def load_settings() -> Settings:
    return Settings()
