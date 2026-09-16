"""What a project's reviewers have written down about how it works.

Dispositions teach one finding at a time and need several before they say anything.
Some knowledge does not have to wait for that: the team already knows that FX rates are
one row per currency per month by contract with the treasury feed, or that every
`_ntnl` column is in the trade's own currency. Semgrep's memories showed the shape that
works — a condition, the guidance, and what it implies — and that general statements
outperform notes about single findings.

Three decisions, each deliberate:

- **They live in the dbt project, in version control.** A convention changes what a
  reviewer is shown, so a change to one should be reviewed like any other change — not
  typed into a database where nobody sees it.
- **They inform and never decide.** A convention reaches a specialist's pack as context,
  and like past judgements it sits outside the text a quote may be grounded in. A stale
  convention must not be able to refute a live finding on its own say-so.
- **A convention that is a checkable fact should become a check.** "stg_fx_rates is unique
  on (currency_code, rate_period)" is better declared as a test: THEMIS then reads it as
  a declared grain and, with `--execute`, measures it. `themis conventions` says so.

File: ``themis_conventions.yml`` at the project root::

    conventions:
      - id: fx-rates-one-per-period
        rules: [F1001]
        models: ["int_*", "fct_revenue*"]
        condition: A join onto stg_fx_rates on currency and rate period.
        guidance: stg_fx_rates holds one row per currency per month, by contract with treasury.
        implication: Such a join does not multiply rows when both keys are in the condition.
        owner: data-platform
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

from themis.logging import get_logger
from themis.models import Finding

log = get_logger(__name__)

FILENAME = "themis_conventions.yml"

# Past this, a convention stops being guidance and starts crowding out the evidence.
_MAX_TEXT = 400


@dataclass(frozen=True)
class Convention:
    id: str
    condition: str
    guidance: str
    implication: str
    rules: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    owner: str | None = None

    def applies_to(self, finding: Finding) -> bool:
        """Scoped by rule and model. An unscoped side matches everything."""
        if self.rules and finding.rule_id not in self.rules:
            return False
        model = finding.evidence.model_name
        return not self.models or any(fnmatch(model, pattern) for pattern in self.models)


@dataclass(frozen=True)
class Loaded:
    conventions: tuple[Convention, ...] = ()
    # (id or position, why it was not loaded). Refusals are reported, never swallowed: a
    # convention someone wrote that silently does nothing is worse than an error.
    rejected: tuple[tuple[str, str], ...] = ()


def _text(entry: dict[str, Any], field: str) -> str | None:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split())


def _strings(entry: dict[str, Any], field: str) -> tuple[str, ...] | None:
    value = entry.get(field, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return tuple(v.strip() for v in value if v.strip())


def load(project_dir: Path) -> Loaded:
    """Read the project's conventions file. No file means no conventions, not an error."""
    path = project_dir / FILENAME
    if not path.exists():
        return Loaded()
    try:
        document = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return Loaded(rejected=((FILENAME, f"not valid YAML: {exc}"),))

    entries = document.get("conventions") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return Loaded(rejected=((FILENAME, "expected a top-level `conventions:` list"),))

    accepted: list[Convention] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        label = f"entry {position}"
        if not isinstance(entry, dict):
            rejected.append((label, "not a mapping"))
            continue
        identifier = _text(entry, "id")
        label = identifier or label
        if identifier is None:
            rejected.append((label, "has no `id`"))
            continue
        if identifier in seen:
            rejected.append((label, "duplicate `id`"))
            continue

        # All three parts, as Semgrep's guidance insists: a memory with no stated
        # implication leaves the reader to guess what follows from it.
        fields = {name: _text(entry, name) for name in ("condition", "guidance", "implication")}
        missing = [name for name, value in fields.items() if value is None]
        if missing:
            rejected.append((label, f"missing {', '.join(missing)}"))
            continue
        too_long = [name for name, value in fields.items() if value and len(value) > _MAX_TEXT]
        if too_long:
            rejected.append(
                (label, f"{', '.join(too_long)} over {_MAX_TEXT} characters — say less")
            )
            continue

        rules = _strings(entry, "rules")
        models = _strings(entry, "models")
        if rules is None or models is None:
            rejected.append((label, "`rules` and `models` must be lists of strings"))
            continue

        seen.add(identifier)
        accepted.append(
            Convention(
                id=identifier,
                condition=fields["condition"] or "",
                guidance=fields["guidance"] or "",
                implication=fields["implication"] or "",
                rules=tuple(r.upper() for r in rules),
                models=models,
                owner=_text(entry, "owner"),
            )
        )

    for label, reason in rejected:
        log.warning("conventions.rejected", convention=label, reason=reason)
    return Loaded(conventions=tuple(accepted), rejected=tuple(rejected))


def for_finding(conventions: tuple[Convention, ...], finding: Finding) -> tuple[Convention, ...]:
    return tuple(c for c in conventions if c.applies_to(finding))


# Words that mark a convention as an assertion about a key — the kind that belongs in a
# test, where it can be measured, rather than in prose, where it can only be believed.
_KEY_WORDS = ("unique", "one row per", "primary key", "grain", "distinct")


def checkable(convention: Convention) -> bool:
    text = f"{convention.condition} {convention.guidance}".lower()
    return any(word in text for word in _KEY_WORDS)
