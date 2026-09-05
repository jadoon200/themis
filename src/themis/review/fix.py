"""Proposing the corrected SQL, not just naming what is wrong.

A rule can say a join fans out and can say, in general terms, what to do about it. It
cannot write the corrected `ON` clause, because that depends on which columns the key
actually needs — which the rule knows only as a list, not as SQL. This is the one job
where a model writing text is exactly the right tool, and where being wrong is cheap:
the reviewer reads a diff, the same as any other suggestion from a colleague.

Two things keep it honest.

**A fix that does not parse is discarded.** Not reviewed, not caveated — dropped. It is
an objective check, it costs a millisecond, and a suggestion that is not valid SQL
wastes the reader's time in the one way this feature cannot afford.

**A fix identical to the original is discarded too.** A model that echoes the input back
has not proposed anything, and presenting that as a suggestion would teach a reviewer to
stop reading them.

Nothing here is ever applied. The output is a suggestion beside a finding, and it says
so.
"""

from __future__ import annotations

from typing import Any

from themis.analyze.parse import ParseError, parse_sql
from themis.config import Settings
from themis.llm.context_pack import ContextPack
from themis.llm.provider import LLMError, Provider, Usage
from themis.logging import get_logger
from themis.models import Finding

log = get_logger(__name__)

FIX_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The model is asked to decline rather than guess. A fix nobody can stand
        # behind is worse than the generic advice the rule already gave.
        "can_fix": {"type": "boolean"},
        "fixed_sql": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["can_fix", "fixed_sql", "explanation"],
}

SYSTEM_PROMPT = """You rewrite a fragment of SQL to remove a specific defect a checker found.

You are given the finding, the SQL it is about, and what the checker knows about the
models involved. Return the corrected SQL for that fragment only — not the whole model,
and not an explanation dressed up as SQL.

Rules you must follow:

- Change only what the finding is about. A reviewer has to read your version against
  theirs, and unrelated edits make that unreadable.
- The dialect is Trino. Write SQL that Trino would accept.
- If the correct fix depends on something you were not given — which columns a key
  really has, whether a table is unique, what the business rule is — set can_fix to
  false and say what you would need. Guessing here produces a fix that looks right and
  is not, which is worse than the generic advice already attached to the finding.
- Never invent a column or a table that does not appear in what you were shown.
"""


def propose(
    finding: Finding,
    pack: ContextPack,
    *,
    provider: Provider,
    settings: Settings,
    usage: Usage,
    dialect: str = "trino",
) -> str | None:
    """Corrected SQL for one finding, or None when nothing usable came back."""
    original = (finding.evidence.sql_after or "").strip()

    prompt = (
        f"{pack.text}\n\n"
        f"## The finding to fix\n\n"
        f"{finding.rule_id}: {finding.title}\n{finding.consequence}\n\n"
        f"## The SQL to rewrite\n\n```sql\n{original or '(see the model SQL above)'}\n```\n"
    )
    try:
        response = provider.complete(
            system=SYSTEM_PROMPT,
            prompt=prompt,
            schema=FIX_SCHEMA,
            model=settings.llm_specialist_model,
        )
    except LLMError as exc:
        log.warning("fix.failed", rule_id=finding.rule_id, error=str(exc)[:200])
        return None

    usage.add(response.usage)
    if not response.payload.get("can_fix"):
        log.debug("fix.declined", rule_id=finding.rule_id)
        return None

    fixed = str(response.payload.get("fixed_sql", "")).strip()
    if not fixed:
        return None

    if not _parses_like(fixed, original, dialect=dialect):
        # Objective, cheap, and the one failure this feature cannot afford: a
        # suggestion that is not valid SQL costs the reader more than silence.
        log.warning("fix.unparseable", rule_id=finding.rule_id, sql=fixed[:120])
        return None

    if _normalised(fixed) == _normalised(original):
        log.debug("fix.unchanged", rule_id=finding.rule_id)
        return None

    return fixed


# A finding's evidence is usually a fragment — a join clause, a predicate, an
# expression — and none of those parse on their own. Checking the fix in isolation
# would therefore reject every correct answer, so it is checked in the same shape the
# original is: whatever context makes the original parse must also make the fix parse.
_CONTEXTS: tuple[str, ...] = (
    "{sql}",
    "select 1 from t {sql}",
    "select 1 from t where {sql}",
    "select {sql} from t",
)


def _parses_like(fixed: str, original: str, *, dialect: str) -> bool:
    """Whether the proposal parses in the same context the original does.

    Falls back to requiring a standalone parse when the original parses in no context
    at all — which means it was never SQL to begin with, and a proposal that is
    complete and valid is still an improvement on it.
    """
    for context in _CONTEXTS:
        if original and _parses(context.format(sql=original), dialect):
            return _parses(context.format(sql=fixed), dialect)
    return _parses(fixed, dialect)


def _parses(sql: str, dialect: str) -> bool:
    try:
        parse_sql(sql, dialect=dialect)
    except ParseError:
        return False
    return True


def _normalised(sql: str) -> str:
    """Whitespace-insensitive comparison, so a reformat does not read as a change."""
    return " ".join(sql.lower().split())
