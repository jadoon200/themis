"""Putting a finding on the line it is about.

Rules reason over compiled SQL, and a pull request is reviewed against the file someone
wrote — Jinja, macros, comments and all. Every finding used to carry no line at all, so
SARIF placed every annotation on line 1 and a reviewer had to go looking for what the
finding was talking about.

The model does not choose the line. Open Code Review names the failure this avoids —
position drift, where an LLM's reported line does not match the code it describes — and
fixes it with a deterministic module that finds the quoted code itself. Here the quote is
the evidence fragment a rule recorded, and finding it is plain token matching: the
compiled fragment and the raw line share their identifiers even when one says
``CAST(ROUND(entries.amount_txn_ccy * rates.rate, 2) AS DECIMAL(38, 2))`` and the other
says ``{{ money('entries.amount_txn_ccy * rates.rate') }}``.

It declines rather than guesses. An annotation on the wrong line is worse than one at the
top of the file, because it points a reviewer confidently at code that is fine.
"""

from __future__ import annotations

import re

from themis.models import Evidence, Finding
from themis.snapshot import ProjectSnapshot

# Identifiers and quoted strings. A quoted string's contents count as an identifier,
# because `ref('stg_fx_rates')` in the file is `"stg_fx_rates"` once compiled.
_TOKEN = re.compile(r"'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*)")
_LINE_COMMENT = re.compile(r"--[^\n]*")
_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

# Words every SQL statement shares. They say nothing about *where* a fragment is.
_STOP = frozenset(
    [
        "select",
        "from",
        "where",
        "and",
        "or",
        "not",
        "on",
        "join",
        "inner",
        "left",
        "right",
        "full",
        "outer",
        "cross",
        "as",
        "case",
        "when",
        "then",
        "else",
        "end",
        "is",
        "null",
        "in",
        "group",
        "by",
        "order",
        "having",
        "limit",
        "with",
        "union",
        "all",
        "distinct",
        "cast",
        "coalesce",
        "sum",
        "count",
        "avg",
        "min",
        "max",
        "round",
        "decimal",
        "varchar",
        "double",
        "integer",
        "bigint",
        "date",
        "timestamp",
        "interval",
        "true",
        "false",
        "between",
        "like",
        "exists",
        "over",
        "partition",
        "row_number",
        "asc",
        "desc",
        "ref",
        "source",
        "config",
        "this",
        "is_incremental",
        "if",
        "endif",
        "endfor",
        "for",
        "set",
        "macro",
        "endmacro",
    ]
)

# How much of the fragment's vocabulary a window must contain to count as the fragment.
_MIN_COVERAGE = 0.75
# A fragment with fewer distinctive words than this cannot be told apart from noise.
_MIN_TOKENS = 2
# The widest run of lines a single fragment is looked for across.
_MAX_WINDOW = 4


def _tokens(text: str) -> set[str]:
    found: set[str] = set()
    for match in _TOKEN.finditer(text):
        raw = next(group for group in match.groups() if group is not None)
        for part in re.split(r"[^A-Za-z0-9_]+", raw.lower()):
            if len(part) >= 2 and part not in _STOP and not part.isdigit():
                found.add(part)
    return found


# catalog.schema.table, quoted or not. Compiled SQL names a relation in full; the file
# names it with ref('table'), so only the last part is shared.
_PART = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
_QUALIFIED = re.compile(rf"{_PART}\.{_PART}\.({_PART})")


def locate(raw_sql: str, fragment: str) -> int | None:
    """The 1-based line where ``fragment`` is written, or None if it cannot be placed.

    Windows of one to four lines are scored, and the best is the one holding the most of
    the fragment; a tie goes to the narrower window, then the earlier line. Coverage comes
    first because a join is written over two lines and its second line alone already holds
    most of its words — ranking width first put the annotation on the ``on`` clause
    instead of the join it belongs to.
    """
    wanted = _tokens(_QUALIFIED.sub(lambda m: m.group(1), fragment))
    if len(wanted) < _MIN_TOKENS:
        return None

    cleaned = _JINJA_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), raw_sql)
    lines = [_LINE_COMMENT.sub("", line) for line in cleaned.splitlines()]
    per_line = [_tokens(line) for line in lines]

    candidates: list[tuple[float, int, int]] = []  # (-coverage, width, start)
    for width in range(1, _MAX_WINDOW + 1):
        for start in range(len(lines) - width + 1):
            # The window's own first line has to carry part of the fragment, or a wider
            # window would report the blank line above the real one.
            if not (wanted & per_line[start]):
                continue
            window = set().union(*per_line[start : start + width])
            coverage = len(wanted & window) / len(wanted)
            if coverage < _MIN_COVERAGE:
                continue
            candidates.append((-coverage, width, start))
    if not candidates:
        return None
    best = min(candidates)

    # The same fragment matched equally well somewhere else entirely — the same join in two
    # CTEs, a predicate repeated in a union. Choosing the first would be a guess presented
    # as a position, so decline.
    rivals = [
        other
        for other in candidates
        if other[:2] == best[:2] and abs(other[2] - best[2]) > _MAX_WINDOW
    ]
    if rivals:
        return None

    # A clause's opening keyword often sits alone on the line above its identifiers —
    # `inner join rates` over `on entries.currency_code = ...`. Its only distinctive
    # word is repeated below, so scoring cannot prefer it; the keyword it starts with can.
    # Step up one line when that line opens the way the fragment does and holds nothing
    # the fragment does not.
    start = best[2]
    lead = fragment.lstrip().split(maxsplit=1)[0].lower() if fragment.strip() else ""
    if start > 0 and lead:
        above = lines[start - 1].strip().lower()
        if above.split(maxsplit=1)[:1] == [lead] and per_line[start - 1] <= wanted:
            start -= 1
    return start + 1


def _config_line(raw_sql: str) -> int | None:
    for number, line in enumerate(raw_sql.splitlines(), start=1):
        if "config(" in line.replace(" ", ""):
            return number
    return None


# Families whose findings are about how a model is configured rather than a statement in
# its SQL. Their natural home is the config block.
_CONFIG_FAMILIES = frozenset({"F5", "F7"})


def position_findings(findings: list[Finding], snapshot: ProjectSnapshot) -> list[Finding]:
    """Give each finding the line in its model's file that it is about, where one exists.

    Findings that already carry a line keep it. Measured findings about a whole model
    have no fragment and stay unpositioned — the model is the right granularity for
    "this model's totals moved".
    """
    out: list[Finding] = []
    for finding in findings:
        evidence = finding.evidence
        model = snapshot.models.get(evidence.model_name)
        raw = model.raw_sql if model is not None else None
        if evidence.line is not None or not raw:
            out.append(finding)
            continue

        line: int | None = None
        if evidence.sql_after:
            line = locate(raw, evidence.sql_after)
        if line is None and finding.family in _CONFIG_FAMILIES:
            line = _config_line(raw)

        if line is None:
            out.append(finding)
            continue
        placed: Evidence = evidence.model_copy(update={"line": line})
        out.append(finding.model_copy(update={"evidence": placed}))
    return out
