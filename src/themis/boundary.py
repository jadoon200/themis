"""Real data, and a reader that must not see it.

At work the warehouse is Trino and its rows are the bank's. The demo's warehouse is DuckDB,
or THEMIS's own local Trino container, and every row in it was generated. THEMIS's own model
may read real values — the agent reasons over them, and captured calls are what tuning is
made from — but a hosted AI assistant driving THEMIS, Claude Code among them, must not.
Everything THEMIS prints, writes or returns to such a reader is the only way those values
would reach it, so that is where they are withheld.

Two questions, each answered conservatively:

- **Is this real data?** DuckDB is not. Trino is, unless it is one of the warehouses listed
  as synthetic — by default only THEMIS's own compose Trino on port 8085. Any other adapter
  is real. `THEMIS_TREAT_ALL_DATA_AS_REAL=true` makes everything real, whatever it looks like.
- **Is an AI assistant reading?** Claude Code marks every command it runs with `CLAUDECODE`.
  `THEMIS_READER=assistant` says so for any other assistant. There is no way to say
  "not an assistant" that an assistant could use on itself: the marker always wins.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from themis.config import Settings

# Set in the environment of every command Claude Code runs.
_ASSISTANT_MARKERS = {
    "CLAUDECODE": "Claude Code",
    "CLAUDE_CODE_ENTRYPOINT": "Claude Code",
}
_SYNTHETIC_ADAPTERS = frozenset({"duckdb"})


@dataclass(frozen=True)
class Boundary:
    real_data: bool
    # Where the data is, in words: "trino at trino.internal:443", "duckdb".
    warehouse: str
    # The AI assistant reading THEMIS's output, or None for a person.
    assistant: str | None

    @property
    def conceal(self) -> bool:
        """Withhold values read from the warehouse from everything THEMIS outputs."""
        return self.real_data and self.assistant is not None

    @property
    def reason(self) -> str:
        return (
            f"real data ({self.warehouse}) and the reader is an AI assistant "
            f"({self.assistant}): values from the warehouse are withheld here, and kept "
            "in full in the stored review for people and THEMIS's own model"
        )


def assistant_reading(environ: Mapping[str, str] | None = None) -> str | None:
    """The AI assistant this process's output goes to, if one can be told."""
    environ = os.environ if environ is None else environ
    for marker, name in _ASSISTANT_MARKERS.items():
        if environ.get(marker):
            return name
    if environ.get("THEMIS_READER", "").strip().lower() == "assistant":
        return "an AI assistant (THEMIS_READER)"
    return None


def classify_warehouse(profile: Mapping[str, Any], settings: Settings) -> tuple[bool, str]:
    """(real, description) for one dbt profile target. Unknown means real."""
    adapter = str(profile.get("type", "")).lower() or "an unknown adapter"
    if settings.treat_all_data_as_real:
        return True, f"{adapter}, and all data is treated as real"
    if adapter in _SYNTHETIC_ADAPTERS:
        return False, adapter
    host = _rendered_host(profile)
    port = profile.get("port")
    where = f"{host}:{port}" if host and port else (host or "an unresolved host")
    listed = {entry.strip().lower() for entry in settings.synthetic_warehouses}
    if adapter == "trino" and where.lower() in listed:
        return False, f"trino at {where}, listed as synthetic"
    return True, f"{adapter} at {where}"


def _rendered_host(profile: Mapping[str, Any]) -> str | None:
    """The host as dbt would see it. A template that cannot be rendered stays unknown."""
    host = profile.get("host")
    if not isinstance(host, str):
        return None
    if "{{" not in host:
        return host.strip()
    try:
        from themis.execute.profiles import render_profile

        rendered = render_profile(dict(profile)).get("host")
    except Exception:
        return None
    return str(rendered).strip() if rendered else None


def detect(
    project: Path,
    *,
    target: str,
    settings: Settings,
    environ: Mapping[str, str] | None = None,
) -> Boundary:
    """The boundary for one run: what the data is, and who is reading."""
    from themis.execute.profiles import ProfileError, read_profile

    assistant = assistant_reading(environ)
    try:
        profile = read_profile(project, target=target)
    except (ProfileError, OSError, ValueError):
        # No profile to read means no way to know: real.
        return Boundary(real_data=True, warehouse="an unreadable profile", assistant=assistant)
    real, warehouse = classify_warehouse(profile, settings)
    return Boundary(real_data=real, warehouse=warehouse, assistant=assistant)
