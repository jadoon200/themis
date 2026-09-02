"""The specialists.

Five narrow reviewers, each given one question and a fixed output shape. Specialised
agents beat general-purpose ones at this size — an 8B model asked "is this specific
flagged join a real fan-out, given these grains" is doing something quite different
from one asked to review a pull request.

Every prompt says the same three things, and they matter more than the domain text:

- judge only the finding you were given;
- everything you need is in the pack, and nothing outside it may be assumed;
- if the pack does not settle it, say uncertain.

The last is the one that has to survive prompt-tuning. A model that guesses when it
lacks evidence produces exactly the confident-and-wrong output this whole design is
built to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from themis.llm.context_pack import ContextPack
from themis.llm.provider import LLMError, Provider, Usage
from themis.logging import get_logger

log = get_logger(__name__)

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["confirm", "refute", "uncertain"]},
        "severity": {
            "type": "string",
            "enum": ["critical", "high", "medium", "low", "info"],
        },
        "rationale": {"type": "string"},
        "evidence_quote": {"type": "string"},
    },
    "required": ["verdict", "severity", "rationale", "evidence_quote"],
}

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "undisclosed_changes": {"type": "array", "items": {"type": "string"}},
        "rationale": {"type": "string"},
    },
    "required": ["undisclosed_changes", "rationale"],
}

_SHARED_RULES = """You review changes to dbt SQL models that transform financial data.

Rules you must follow:
- Judge ONLY the finding you are given. Do not comment on anything else.
- Everything you may rely on is in the context. Do not assume table contents, row
  counts, lineage, or column meanings that are not stated there.
- If the context does not settle the question, answer "uncertain". Guessing is worse
  than abstaining, because a confident wrong answer will be trusted.
- `evidence_quote` must be copied verbatim from the context. Do not paraphrase it and
  do not invent one.
- Be brief. Two sentences of rationale is usually enough."""


@dataclass(frozen=True)
class Specialist:
    """One reviewer: a name, the families it handles, and its question."""

    name: str
    families: frozenset[str]
    question: str

    @property
    def system_prompt(self) -> str:
        return f"{_SHARED_RULES}\n\n{self.question}"


GRAIN = Specialist(
    name="grain",
    families=frozenset({"F1", "F8"}),
    question="""You judge whether a flagged change actually duplicates or drops rows.

A join multiplies rows when the joined table has more than one row per join key. The
context gives you the grains that were established, and how: "structural" and
"measured" are reliable, "heuristic" is a guess from column naming and proves nothing.
An unestablished grain means the question is open, not that the join is safe.

If a measurement is present, it settles the matter — prefer it over any reasoning.""",
)

MONEY = Specialist(
    name="money",
    families=frozenset({"F3", "F4"}),
    question="""You judge whether a flagged change corrupts monetary values.

Money must be an exact decimal type. Binary floating point cannot represent 0.01, so
summing such a column accumulates error. Reducing a decimal's scale truncates. Changing
which branch of a sign convention is negated inverts every amount. Changing a period
truncation moves rows between reporting periods and can select the wrong rate.

Judge whether the specific flagged change does one of these things to a column that
actually holds money.""",
)

INCREMENTAL = Specialist(
    name="incremental",
    families=frozenset({"F5"}),
    question="""You judge whether a flagged change breaks incremental loading.

Incremental models fail quietly: the run succeeds and rows are missing or duplicated.
`append` never deduplicates; `delete+insert` removes the matched window first; `merge`
needs a genuinely unique key. Removing the `is_incremental()` guard reprocesses all
history. Narrowing a lookback window silently drops late-arriving rows.

Note that several of these produce no visible change today and fail only later. That
still counts as confirmed — say so in the rationale.""",
)

CONTRACTS = Specialist(
    name="contracts",
    families=frozenset({"F6", "F7"}),
    question="""You judge whether a flagged change breaks something downstream.

A removed column breaks the models that select it. A literal table name in place of
ref() still runs but leaves the DAG, so build order is no longer guaranteed and a
development run can read production. A sensitive column reaching a published model
widens who can see it.

Weigh the number of downstream models and any regulatory or reconciliation tags.""",
)

INTENT = Specialist(
    name="intent",
    families=frozenset(),
    question="""You compare what a change does against what its author said it does.

You are given the author's description, the models affected, and what the automated
checks found. List anything the change does that the description does not mention —
particularly changes to incremental behaviour, to models tagged regulatory or recon,
or to how money is typed.

List nothing if the description covers the change. Do not repeat findings that the
automated checks already reported; they are shown to you as context, not as output.""",
)

ALL_SPECIALISTS: tuple[Specialist, ...] = (GRAIN, MONEY, INCREMENTAL, CONTRACTS)


def specialist_for(family: str) -> Specialist | None:
    for specialist in ALL_SPECIALISTS:
        if family in specialist.families:
            return specialist
    return None


@dataclass
class Adjudication:
    """A specialist's answer about one finding."""

    verdict: str
    severity: str
    rationale: str
    evidence_quote: str
    specialist: str
    usage: Usage

    @property
    def confirmed(self) -> bool:
        return self.verdict == "confirm"

    @property
    def refuted(self) -> bool:
        return self.verdict == "refute"


def adjudicate(
    provider: Provider,
    specialist: Specialist,
    pack: ContextPack,
    *,
    model: str,
) -> Adjudication | None:
    """Ask one specialist about one finding. None if the model could not answer."""
    try:
        response = provider.complete(
            system=specialist.system_prompt,
            prompt=pack.text,
            schema=VERDICT_SCHEMA,
            model=model,
        )
    except LLMError as exc:
        # A model failure must never silently drop a finding. Returning None leaves
        # the deterministic finding standing exactly as it was.
        log.warning("specialist.failed", specialist=specialist.name, error=str(exc)[:200])
        return None

    payload = response.payload
    verdict = str(payload.get("verdict", "uncertain"))
    if verdict not in ("confirm", "refute", "uncertain"):
        verdict = "uncertain"

    return Adjudication(
        verdict=verdict,
        severity=str(payload.get("severity", "medium")),
        rationale=str(payload.get("rationale", "")).strip(),
        evidence_quote=str(payload.get("evidence_quote", "")).strip(),
        specialist=specialist.name,
        usage=response.usage,
    )
