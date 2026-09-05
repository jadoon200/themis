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

from themis.llm.context_pack import ContextPack, Section
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

You are judging whether a flagged **risk** is real, not whether harm has already
occurred. "confirm" means the risk described is genuine and a reviewer should look at
it. It does not mean you have proved the damage. If you can see the mechanism by which
the change could produce a wrong result, that is "confirm".

Reserve "uncertain" for when you genuinely cannot tell from the context — not for when
you can see the problem but cannot quantify it.

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
    """One reviewer: a name, the families it handles, its question, and its evidence."""

    name: str
    families: frozenset[str]
    question: str
    needs: frozenset[Section] = frozenset()

    @property
    def system_prompt(self) -> str:
        return f"{_SHARED_RULES}\n\n{self.question}"


GRAIN = Specialist(
    name="grain",
    families=frozenset({"F1"}),
    needs=frozenset({Section.RELATED_SQL, Section.GRAIN, Section.UPSTREAM_GRAINS}),
    question="""You judge whether a flagged change actually duplicates or drops rows.

A join multiplies rows when the joined table has more than one row per join key.

Work it out in this order:

1. If a measurement is present, it settles the matter. Prefer it over any reasoning.
2. Otherwise, read the SQL of the model being joined to, which you are given. Work out
   what one row of it represents — from its GROUP BY, its DISTINCT, its comments, or
   what it selects. If one row is identified by more columns than the join key uses,
   the join multiplies rows and the finding is confirmed.
3. Only if neither settles it, answer "uncertain".

The stated grains are a starting point, not the answer. "structural" and "measured"
are reliable; "heuristic" is a guess from column naming and proves nothing — do not
treat a heuristic grain as evidence the join is safe, and do not stop at "the grain is
unproven" if the SQL in front of you shows what the real key is.

There is a third answer and it is often the correct one. Some tables are unique on a
key because of what the data happens to contain, not because anything in the SQL makes
them so: a dimension selecting straight from a staging model has no GROUP BY, no
DISTINCT and no dedup, so nothing in front of you decides it either way. That is
"uncertain". Do not confirm on the grounds that uniqueness is unproven — unproven is
why you were asked, not an answer — and do not refute on the grounds that it looks
like a dimension. Say that the SQL cannot settle it and that measuring would.""",
)

MONEY = Specialist(
    name="money",
    families=frozenset({"F3", "F4"}),
    needs=frozenset({Section.COLUMN_TYPES, Section.TAGS}),
    question="""You judge whether a flagged change corrupts monetary values.

Money must be an exact decimal type. Binary floating point cannot represent 0.01, so
summing such a column accumulates error. Reducing a decimal's scale truncates. Changing
which branch of a sign convention is negated inverts every amount. Changing a period
truncation moves rows between reporting periods and can select the wrong rate.

Judge whether the specific flagged change does one of these things to a column that
actually holds money.""",
)

FILTERS = Specialist(
    name="filters",
    families=frozenset({"F2"}),
    needs=frozenset({Section.RELATED_SQL, Section.GRAIN, Section.TAGS}),
    question="""You judge whether a change to a predicate alters which rows are counted.

A filter decides the population. Adding a condition removes rows from every total
downstream; removing one adds them. Neither errors, and the output is still
well-formed — only the figure changes.

Work it out in this order:

1. Read the predicate that changed and say plainly which rows it now includes or
   excludes that it did not before.
2. Judge whether that population change matters for what the model represents. A
   filter on a monetary or entity column almost always does; one on a technical
   column such as a batch id or a load timestamp often does not.
3. Watch for three-valued logic. `NOT IN` against a subquery returning any NULL is
   never true, so it removes every row. `<>` and `NOT LIKE` silently drop NULLs. Both
   look like ordinary SQL and produce an empty or truncated result rather than an
   error.

A boundary moved by a small amount is still a population change — say so rather than
treating it as cosmetic.

One case does settle cleanly: a filter excluding NULLs of a column that the stated
grain proves cannot be NULL removes no rows at all. Refute that one — but only when the
grain is "structural", "measured", "config" or "declared_test". A "heuristic" grain is a
guess from the column's name and settles nothing, so that case is "uncertain".""",
)

INCREMENTAL = Specialist(
    name="incremental",
    families=frozenset({"F5"}),
    needs=frozenset({Section.CONFIG, Section.GRAIN, Section.TAGS}),
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
    families=frozenset({"F6"}),
    needs=frozenset({Section.COLUMN_CONSUMERS, Section.BLAST_RADIUS, Section.TAGS}),
    question="""You judge whether a flagged change breaks something downstream.

A removed column breaks the models that read it. A literal table name in place of
ref() still runs but leaves the DAG, so build order is no longer guaranteed and a
development run can read production. Narrowing a type under an enforced contract
breaks the promise the contract makes.

You are told which downstream columns actually read the one that changed, traced
through the SQL rather than found by searching for its name. Use that list, and note
that a model can depend on a column without carrying it forward — one that joins on it
or filters by it breaks just as surely while producing no column of its own.

An empty consumer list from lineage means nothing reads it. A model listed as
unresolved means nobody knows; treat that as a reason for caution, not for comfort.""",
)


ENGINE = Specialist(
    name="engine",
    families=frozenset({"F8"}),
    needs=frozenset({Section.ENGINE_SHAPE, Section.RELATED_SQL, Section.BLAST_RADIUS}),
    question="""You judge whether a flagged change makes a query behave badly on Trino.

This is about how the engine runs the query, not about whether the numbers are wrong.

- A join across two catalogs cannot be pushed down: both sides are read in full and
  joined on the coordinator. On a large table that is the difference between a query
  and an outage.
- A filter that wraps the partition column in a function stops the engine pruning
  partitions, so it scans everything.
- A join condition that is always true pairs every row with every row.
- `LIMIT` without `ORDER BY` returns an arbitrary subset, which differs between runs.
- `approx_distinct` and `approx_percentile` are estimates. In a regulatory figure that
  is wrong even when it is close.

Say which of these the change does. If it does none of them, refute. Cost alone, with
no correctness or reproducibility consequence, is a real finding but a low-severity
one — say so rather than inflating it.""",
)


GOVERNANCE = Specialist(
    name="governance",
    families=frozenset({"F7"}),
    needs=frozenset({Section.TAGS, Section.BLAST_RADIUS, Section.COLUMN_CONSUMERS}),
    question="""You judge whether a flagged change widens who can see something, or
weakens what can be proven about a reported figure.

- A personal or counterparty column — an email, a name, an account identifier — reaching
  a model with a wider audience than the one it came from is an exposure, whether or not
  anyone has queried it yet.
- A model tagged `regulatory`, `recon` or `control` feeds something someone signs. A
  change to one carries more weight than the same change elsewhere, and the tags you
  are given are the evidence for that, not a guess.
- A model whose grain cannot be established means every duplication check involving it
  is unproven — including the ones that came back clean.

Judge exposure and provability, not arithmetic. If the change is a correctness problem
rather than a governance one, refute and say which — another reviewer has that.""",
)

INTENT = Specialist(
    name="intent",
    families=frozenset(),
    needs=frozenset(),
    question="""You compare what a change does against what its author said it does.

You are given the author's description, the models affected, and what the automated
checks found. List anything the change does that the description does not mention —
particularly changes to incremental behaviour, to models tagged regulatory or recon,
or to how money is typed.

Do not repeat findings that the automated checks already reported; they are shown to
you as context, not as output.

If the description covers the change, return an **empty list**. Do not write a sentence
saying there is nothing undisclosed — an empty list is how you say that, and a sentence
saying "nothing was omitted" is read by everything downstream as an omission having
been found.""",
)

ALL_SPECIALISTS: tuple[Specialist, ...] = (
    GRAIN,
    FILTERS,
    MONEY,
    INCREMENTAL,
    CONTRACTS,
    ENGINE,
    GOVERNANCE,
)


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
