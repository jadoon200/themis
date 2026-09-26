"""Copies of a review with every value read from the warehouse taken out.

For a reader who must not see real data (themis/boundary.py). What stays is what a reviewer
needs to act: which models moved, which way, in which columns, and every finding. What goes
is every number and value the warehouse returned — row counts, totals, null rates, paired-row
counts, key values, periods — and any number in text written from them, including what the
model layer wrote. The review is concealed on its way out, never before it is stored: people
and THEMIS's own model keep the full one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from themis.models import Confidence, ExecutionDelta, Finding, Grain, sum_moved

WITHHELD = "[withheld]"

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\b")
# A number standing on its own. Digits glued to a letter or underscore are part of a name —
# `F1004`, `amount_2` — and survive; everything else goes, including numbers in SQL types,
# which is the price of never letting a total through.
_NUMBER = re.compile(r"(?<![A-Za-z_\d.])[-+]?\d[\d,]*(?:\.\d+)?(?:\s?(?:%|[KMB]\b|bn\b))?")
_QUOTED = re.compile(r"'[^'\n]{1,200}'")
# Findings whose text is written from measured values.
_MEASURED_RULES = frozenset({"X0001", "X0002", "X0004", "F1004"})


def scrub(text: str | None, *, known: Iterable[str] = ()) -> str | None:
    """Text with every date, number, quoted literal and known value withheld."""
    if not text:
        return text
    for value in sorted({v for v in known if v and len(v) >= 2}, key=len, reverse=True):
        text = text.replace(value, WITHHELD)
    text = _DATE.sub(WITHHELD, text)
    text = _QUOTED.sub(f"'{WITHHELD}'", text)
    return _NUMBER.sub(WITHHELD, text)


def _direction(before: float, after: float) -> str:
    return "rose" if after > before else "fell"


def conceal_delta(delta: ExecutionDelta) -> ExecutionDelta:
    """The same measurement, in words, with nothing the warehouse returned."""
    if delta.concealed:
        return delta
    notes: list[str] = []
    if delta.build_error is not None:
        notes.append(f"{delta.failed_revision or 'a'} build failed")
    if delta.row_delta:
        assert delta.rows_before is not None and delta.rows_after is not None
        notes.append(f"row count {_direction(delta.rows_before, delta.rows_after)}")
    elif delta.row_delta == 0:
        notes.append("row count unchanged")
    for column, (before, after) in sorted(delta.sum_deltas.items()):
        if sum_moved(before, after):
            notes.append(f"sum({column}) {_direction(before, after)}")
    for column, (was, now) in sorted(delta.null_rate_deltas.items()):
        if was != now:
            notes.append(f"null rate of {column} {_direction(was, now)}")
    keyed = delta.keyed
    if keyed is not None and keyed.moved:
        parts = [
            label
            for label, count in (
                ("rows added", keyed.rows_added),
                ("rows removed", keyed.rows_removed),
                ("values changed", keyed.rows_changed),
            )
            if count
        ]
        columns = ", ".join(sorted(keyed.columns_changed))
        notes.append(
            f"paired on ({', '.join(keyed.key)}): {', '.join(parts)}"
            + (f" in {columns}" if columns else "")
        )
        if keyed.restates_a_closed_period:
            notes.append(f"moves figures in a closed {keyed.period_column} period")
    return delta.model_copy(
        update={
            "rows_before": None,
            "rows_after": None,
            "sum_deltas": {},
            "null_rate_deltas": {},
            "keyed": None,
            "build_error": scrub(delta.build_error),
            "concealed": True,
            "withheld": tuple(notes),
            "concealed_material": delta.is_material,
        }
    )


def conceal_grain(grain: Grain) -> Grain:
    if grain.rows_per_key is None:
        return grain
    return grain.model_copy(
        update={
            "rows_per_key": None,
            "note": (
                "measured: the key identifies a row"
                if grain.rows_per_key <= 1.0
                else "measured: the key does NOT identify a row"
            ),
        }
    )


def conceal_finding(finding: Finding, *, known: Iterable[str] = ()) -> Finding:
    known = tuple(known)
    measured = (
        finding.execution_delta is not None
        or finding.confidence is Confidence.MEASURED
        or finding.rule_id in _MEASURED_RULES
    )
    evidence = finding.evidence
    update: dict[str, Any] = {
        # The model's prose may repeat what it was shown, and it was shown real values.
        "llm_rationale": scrub(finding.llm_rationale, known=known),
    }
    if measured:
        update["consequence"] = scrub(finding.consequence, known=known) or ""
        update["evidence"] = evidence.model_copy(update={"note": scrub(evidence.note, known=known)})
    if finding.execution_delta is not None:
        update["execution_delta"] = conceal_delta(finding.execution_delta)
    return finding.model_copy(update=update)


def sensitive_values(deltas: Iterable[ExecutionDelta]) -> tuple[str, ...]:
    """Values the warehouse returned as text — sample keys and periods — to withhold
    wherever they turn up, prose included."""
    values: set[str] = set()
    for delta in deltas:
        keyed = delta.keyed
        if keyed is None:
            continue
        values.update(keyed.sample_keys)
        values.update(v for v in (keyed.latest_period, keyed.earliest_changed_period) if v)
    return tuple(sorted(values))


def conceal_execution(execution: Any) -> Any:
    """An ExecutionResult with its deltas and measured grains concealed."""
    if execution is None:
        return None
    return replace(
        execution,
        deltas={name: conceal_delta(d) for name, d in execution.deltas.items()},
        measured_grains={n: conceal_grain(g) for n, g in execution.measured_grains.items()},
        baseline_grains={n: conceal_grain(g) for n, g in execution.baseline_grains.items()},
    )


def conceal_review(result: Any) -> Any:
    """A ReviewResult as a reader who must not see real values may see it."""
    deltas = result.execution.deltas.values() if result.execution is not None else ()
    known = sensitive_values(deltas)
    return replace(
        result,
        findings=[conceal_finding(f, known=known) for f in result.findings],
        grains={name: conceal_grain(grain) for name, grain in result.grains.items()},
        execution=conceal_execution(result.execution),
    )
