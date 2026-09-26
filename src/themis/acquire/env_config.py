"""The values a scheduler hands dbt, read from the file it reads them from.

At work the Dagster project keeps an `env.config.ini`: the Trino environment and every
schema, written down once per environment — prod, pre-prod, UAT. Dagster reads it and
dbt sees the result, as `--vars` or as environment variables. THEMIS runs dbt itself, so
without the same values the first compile fails on a variable nobody set, or — worse —
succeeds against whatever default the project falls back to.

So THEMIS reads the ini too, one section of it, and gives dbt that section both ways: as
`--vars` and as environment variables (each key as written and upper-cased). Whichever the
project uses finds its value; the other is ignored. The section is named explicitly and a
production-looking one is refused, because these values can point dbt at a different
warehouse altogether, and the target allowlist only ever sees the target's name.

Values are never logged or written anywhere; only how many there are.
"""

from __future__ import annotations

import configparser
import json
from dataclasses import dataclass, field
from pathlib import Path

from themis.logging import get_logger

log = get_logger(__name__)

# A target or section whose name says production. Substring on purpose: `preprod` holds
# production-shaped data more often than not, and refusing it costs a rename.
PRODUCTION_WORDS = ("prod", "prd", "live", "production")

# The dbt commands that accept --vars.
_TAKES_VARS = frozenset(
    {"build", "compile", "run", "ls", "list", "parse", "seed", "snapshot", "test", "debug", "show"}
)
# An ini with no section header is read as one flat section under this name.
_FLAT = "__flat__"


def looks_like_production(name: str) -> bool:
    return any(word in name.lower() for word in PRODUCTION_WORDS)


class EnvConfigError(RuntimeError):
    """The environment file cannot be used as configured, and nothing should run."""


@dataclass(frozen=True)
class EnvConfig:
    path: Path
    # None for a file with no sections.
    section: str | None
    values: dict[str, str] = field(default_factory=dict)

    @property
    def environment(self) -> dict[str, str]:
        """Each key as written and upper-cased: a scheduler exporting `trino_env` as
        `TRINO_ENV` is the usual shape, and costing one extra variable is cheap."""
        out: dict[str, str] = {}
        for key, value in self.values.items():
            out[key] = value
            out[key.upper()] = value
        return out

    def vars_argument(self, command: str) -> list[str]:
        """`--vars <json>` for a dbt command that takes it, else nothing."""
        if command not in _TAKES_VARS or not self.values:
            return []
        return ["--vars", json.dumps(self.values, sort_keys=True)]

    def describe(self) -> str:
        where = f"[{self.section}]" if self.section else "(no sections)"
        return f"{self.path.name} {where}, {len(self.values)} value(s)"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def read_env_config(path: Path, section: str | None) -> EnvConfig:
    """One section of an ini file — or all of a file that has no sections."""
    if not path.exists():
        raise EnvConfigError(f"no environment file at {path}")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]  # keep the key's case
    text = path.read_text(errors="replace")
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        parser.read_string(f"[{_FLAT}]\n{text}")
    except configparser.Error as exc:
        raise EnvConfigError(f"{path.name} is not a readable ini file: {exc}") from exc

    sections = [name for name in parser.sections() if name != _FLAT]
    if section is None:
        if sections:
            raise EnvConfigError(
                f"{path.name} has a section per environment ({', '.join(sections)}); set "
                "THEMIS_DBT_ENV_SECTION to the non-production one THEMIS should use"
            )
        chosen = _FLAT
    else:
        if looks_like_production(section):
            raise EnvConfigError(
                f"section [{section}] looks like production and is refused: its values can "
                "point dbt at a production warehouse whatever the target is called"
            )
        if section not in parser.sections():
            raise EnvConfigError(
                f"{path.name} has no section [{section}]"
                + (f" (it has {', '.join(sections)})" if sections else "")
            )
        chosen = section
    values = {key: _unquote(value) for key, value in parser.items(chosen)}
    return EnvConfig(path=path, section=None if chosen == _FLAT else chosen, values=values)


def active_env_config() -> EnvConfig | None:
    """The environment file THEMIS is configured to use, or None when there is none."""
    from themis.config import Settings

    settings = Settings()
    if settings.dbt_env_config is None:
        return None
    config = read_env_config(settings.dbt_env_config, settings.dbt_env_section)
    log.debug("env_config.loaded", source=config.describe())
    return config
