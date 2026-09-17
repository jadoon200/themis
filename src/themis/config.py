"""Configuration. All settings via ``THEMIS_*`` env vars, a ``.env`` file, or CLI flags."""

from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from themis import vocabulary


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
    # Only "ollama" is implemented. The setting exists so a second provider is a new
    # class plus a config edit, not a change to every caller.
    llm_provider: str = "ollama"
    llm_base_url: str = "http://127.0.0.1:11434"
    # High-volume, narrow, JSON-schema'd specialist calls.
    llm_specialist_model: str = "qwen3:8b"
    # The intent pass, once per review. Nominally the place for a larger model, but
    # qwen3:30b measured at 2.0 tok/s locally and timed out before returning anything,
    # so it cost 18GB and contributed nothing. Kept as a separate setting because a
    # deployment with the hardware to run a larger model should use one here — that is
    # a decision for the eval to make, not an assumption to ship.
    llm_supervisor_model: str = "qwen3:8b"
    llm_timeout_s: float = 120.0
    # Retries after a transient failure — timeout, dropped connection, 5xx, a body that is
    # not JSON — with a linear backoff. A 4xx is never retried.
    llm_retries: int = 2
    llm_retry_backoff_s: float = 1.0
    # Sampling. Zero by default because a verdict is not a creative task and two runs
    # of one review should agree; exposed so that claim can be measured rather than
    # assumed. `num_predict` caps the reply — too low truncates a quote mid-token and
    # the self-check then rejects a sound answer for looking fabricated.
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 400
    # Context packs are kept small on purpose; the LLM never sees a whole file.
    llm_max_context_tokens: int = 2000

    # --- execution (Stage 3) -------------------------------------------------
    execute_enabled: bool = False
    # Prefixes for the schemas the base and head builds land in. Never production. Each
    # run appends its own token, so no two runs — and no two workers — share one.
    execute_base_schema: str = "themis_base"
    execute_head_schema: str = "themis_head"
    # Leave a run's schemas in place instead of dropping them, to inspect what was built.
    execute_keep_schemas: bool = False
    execute_timeout_s: float = 900.0
    # Skip models above this many rows rather than blowing the time budget.
    execute_max_rows: int = 5_000_000
    # Pair base and head rows on the derived grain and count what changed, so values that
    # move between keys are measured even when every row count and total holds. Only runs
    # on a key Stage 3 has counted unique in both builds.
    execute_keyed_diff: bool = True
    # Columns never compared row by row: load timestamps and run identifiers that differ
    # between two builds of the same code by construction. Exact names, lowercase. Named
    # in the report whenever one is skipped, so a real change hiding here stays visible.
    execute_keyed_ignore_columns: tuple[str, ...] = (
        "_loaded_at",
        "loaded_at",
        "_etl_loaded_at",
        "_dbt_loaded_at",
        "dbt_updated_at",
        "dbt_valid_from",
        "dbt_valid_to",
        "dbt_scd_id",
        "_dbt_run_id",
        "invocation_id",
        "run_started_at",
    )
    # Any dbt target whose name is not in this set is refused outright. The guard is
    # deliberately an allowlist: a typo must fail closed, not run against prod.
    execute_allowed_targets: tuple[str, ...] = (
        "dev",
        "ci",
        "duckdb",
        "trino",
        "test",
        "local",
    )

    # --- vocabulary ----------------------------------------------------------
    # The names checks match on. Each replaces its default outright — set it as a JSON
    # list, e.g. THEMIS_MONEY_COLUMN_HINTS='["amount","ntnl","mtm","pnl"]' — and
    # `themis profile` shows how often each matches a project. See themis/vocabulary.py.
    money_column_hints: tuple[str, ...] = vocabulary.MONEY_HINTS
    sensitive_column_hints: tuple[str, ...] = vocabulary.SENSITIVE_HINTS
    governed_tags: tuple[str, ...] = vocabulary.GOVERNED_TAGS
    published_folders: tuple[str, ...] = vocabulary.PUBLISHED_FOLDERS

    # --- manifest cache ------------------------------------------------------
    # Compiled manifests are content-addressed by git revision, so the base compile a
    # review repeats every time is paid once. Refused automatically for projects whose
    # compiled SQL is built from query results, where the revision does not determine
    # the output. Lives under `.themis/`, which is gitignored.
    manifest_cache_enabled: bool = True

    # --- gate ----------------------------------------------------------------
    # Advisory by default. Blocking is opt-in, per severity.
    fail_on_severity: str | None = None

    @field_validator("fail_on_severity", mode="before")
    @classmethod
    def _known_severity(cls, value: object) -> str | None:
        """Refuse a severity the gate cannot apply, instead of silently never blocking.

        ``THEMIS_FAIL_ON_SEVERITY=HIGH`` used to leave every merge unblocked: the value
        did not parse, and an unparseable threshold returned exit code 0. A gate that
        fails open on a typo is advisory in a way nobody chose.
        """
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        normalised = str(value).strip().lower()
        allowed = ("critical", "high", "medium", "low", "info")
        if normalised not in allowed:
            raise ValueError(
                f"THEMIS_FAIL_ON_SEVERITY={value!r} is not a severity; "
                f"use one of {', '.join(allowed)}"
            )
        return normalised

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
    # A bearer token every endpoint but /health requires. Unset means no check, which is
    # only reasonable while the API is bound to the loopback interface.
    api_token: str | None = None
    # Directories a queued review's project must live under. Empty means relative paths
    # only, resolved against the worker's working directory. A review runs dbt — and so
    # the project's own macros and hooks — on whatever path it is given.
    project_roots: tuple[str, ...] = ()
    # Mixed into the hashes `--redact` puts in place of model and column names. Without one,
    # anyone holding a list of likely names can hash them and match. Keep it private.
    redact_salt: str = ""

    # --- learning from what reviewers decided ---------------------------------
    # How many past judgements on the same rule a specialist is shown. 0 turns the
    # retrieval off entirely and leaves the ranking's use of dispositions untouched —
    # the two are separate levers on purpose, because one changes what a model reads
    # and the other changes only the order of a list.
    prior_judgement_examples: int = 3


def load_settings() -> Settings:
    return Settings()
