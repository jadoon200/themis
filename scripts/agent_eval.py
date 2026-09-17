"""How often the agent answers correctly, refuses correctly, and is caught when it is wrong.

A handful of good live answers says little. This asks a fixed set of questions about the
demo project with known answers — facts that must appear, and facts that must not — plus
questions no tool can answer, which must be refused. Review-mode questions run against the
fan-out fixture, where the findings and the SQL diff are known.

Four outcomes per question, and the distinction between the last two is the point:
  correct            grounded, every required fact present, no forbidden one
  refused correctly  an unanswerable question, refused
  wrong              grounded but missing a fact or stating a forbidden one — the failure
                     grounding alone cannot catch, and the number to watch
  refused wrongly    an answerable question, refused

    python scripts/agent_eval.py [--only manifest|review] [--json results.json]

Needs Ollama with the configured model, and the demo project compiled (make demo-build).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from themis.agent.loop import investigate  # noqa: E402
from themis.agent.workspace import Workspace  # noqa: E402
from themis.config import load_settings  # noqa: E402
from themis.llm.provider import build_provider  # noqa: E402
from themis.logging import configure_logging  # noqa: E402

PROJECT = REPO / "demo_project"


@dataclass(frozen=True)
class Question:
    text: str
    mode: str  # "manifest" or "review"
    must: tuple[str, ...] = ()
    must_not: tuple[str, ...] = ()
    answerable: bool = True
    note: str = ""


QUESTIONS: tuple[Question, ...] = (
    Question(
        "What is the grain of fct_account_period_summary?",
        "manifest",
        must=("account_id", "period_month"),
    ),
    Question(
        "What is the grain of fct_regulatory_summary, and how was it established?",
        "manifest",
        must=("period_month", "entity_code", "currency_code", "structural"),
    ),
    Question(
        "Which models downstream of int_revenue_recognized are tagged regulatory?",
        "manifest",
        must=("fct_regulatory_summary", "fct_revenue_reported"),
        must_not=("fct_revenue_incremental",),
        note="fct_revenue_incremental is tagged recon only — the trap",
    ),
    Question(
        "Which upstream column is fct_revenue.amount_usd computed from?",
        "manifest",
        must=("int_revenue_recognized",),
    ),
    Question(
        "How is fct_revenue_incremental materialized, and with which incremental strategy?",
        "manifest",
        must=("incremental", "delete+insert"),
    ),
    Question(
        "Is dim_accounts built on stg_accounts?",
        "manifest",
        must=("yes",),
    ),
    Question(
        "Does int_gl_entries_converted join stg_fx_rates on the rate period "
        "as well as the currency?",
        "manifest",
        must=("rate_period",),
    ),
    Question("What does rule F1001 check for?", "manifest", must=("join",)),
    # Multi-hop: several tools chained, where a small model is most likely to go wrong —
    # and where the tools themselves first answered wrongly (see EVAL, "The agent").
    Question(
        "Which columns of fct_regulatory_summary are computed, directly or indirectly, "
        "from stg_fx_rates.rate?",
        "manifest",
        must=("revenue_usd",),
        note="needed downstream models traced before answering",
    ),
    Question(
        "Which staging model columns does fct_regulatory_summary.revenue_usd ultimately come from?",
        "manifest",
        must=("stg_fx_rates", "stg_gl_entries"),
        note="needed every ancestor traced, not one hop",
    ),
    Question(
        "Which models downstream of stg_gl_entries are materialized as incremental?",
        "manifest",
        must=("fct_revenue_incremental",),
        must_not=("fct_revenue_reported", "fct_account_period_summary"),
    ),
    Question(
        "Is any model built on int_account_activity tagged regulatory?",
        "manifest",
        must=("no",),
        must_not=("fct_regulatory_summary",),
    ),
    Question("Who is the business owner of fct_revenue?", "manifest", answerable=False),
    Question(
        "How long does fct_revenue take to build in production?", "manifest", answerable=False
    ),
    Question("What was total reported revenue in the last quarter?", "manifest", answerable=False),
    Question(
        "Which rule fired on int_gl_entries_converted in this review?",
        "review",
        must=("F1001",),
    ),
    Question(
        "What changed in the join of int_gl_entries_converted between the two revisions?",
        "review",
        must=("rate_period",),
    ),
)


@dataclass
class Outcome:
    question: Question
    verdict: str
    answer: str
    refusal: str | None
    tools: list[str] = field(default_factory=list)
    seconds: float = 0.0
    calls: int = 0


def _verdict(question: Question, grounded: bool, answer: str) -> str:
    text = answer.lower()
    if not question.answerable:
        return "refused correctly" if not grounded else "wrong"
    if not grounded:
        return "refused wrongly"
    if all(fact.lower() in text for fact in question.must) and not any(
        fact.lower() in text for fact in question.must_not
    ):
        return "correct"
    return "wrong"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", choices=["manifest", "review"])
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    configure_logging()
    settings = load_settings()
    provider = build_provider(settings)

    workspaces: dict[str, Workspace] = {}
    if args.only in (None, "manifest"):
        workspaces["manifest"] = Workspace.from_manifest(PROJECT / "target" / "manifest.json")
    if args.only in (None, "review"):
        from themis.pipeline import review

        result = review(
            PROJECT,
            base="fixture/fx-fanout~1",
            head="fixture/fx-fanout",
            settings=settings,
            run_llm=False,
            use_manifest_cache=False,
        )
        workspaces["review"] = Workspace.from_review(result)

    outcomes: list[Outcome] = []
    for question in QUESTIONS:
        if question.mode not in workspaces:
            continue
        started = time.monotonic()
        answer = investigate(
            question.text, workspaces[question.mode], provider=provider, settings=settings
        )
        outcome = Outcome(
            question=question,
            verdict=_verdict(question, answer.grounded, answer.answer),
            answer=answer.answer,
            refusal=answer.refusal_reason,
            tools=[step.tool for step in answer.steps],
            seconds=time.monotonic() - started,
            calls=answer.usage.calls,
        )
        outcomes.append(outcome)
        print(f"{outcome.verdict:18s} {outcome.seconds:5.1f}s  {question.text}", flush=True)
        if outcome.verdict in ("wrong", "refused wrongly"):
            print(f"    tools: {outcome.tools}")
            print(f"    answer: {outcome.answer[:300] or outcome.refusal}")

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
    answerable = sum(1 for o in outcomes if o.question.answerable)
    unanswerable = len(outcomes) - answerable
    print("")
    print(
        f"answerable   {counts.get('correct', 0)}/{answerable} correct, "
        f"{counts.get('refused wrongly', 0)} refused, "
        f"{sum(1 for o in outcomes if o.question.answerable and o.verdict == 'wrong')} wrong"
    )
    print(
        f"unanswerable {counts.get('refused correctly', 0)}/{unanswerable} refused, "
        f"{sum(1 for o in outcomes if not o.question.answerable and o.verdict == 'wrong')} answered"
    )
    print(
        f"median time  {sorted(o.seconds for o in outcomes)[len(outcomes) // 2]:.1f}s, "
        f"model calls {sum(o.calls for o in outcomes)}"
    )

    if args.json:
        args.json.write_text(
            json.dumps(
                [
                    {
                        "question": o.question.text,
                        "mode": o.question.mode,
                        "verdict": o.verdict,
                        "answer": o.answer,
                        "refusal": o.refusal,
                        "tools": o.tools,
                        "seconds": round(o.seconds, 1),
                    }
                    for o in outcomes
                ],
                indent=2,
            )
        )
    return 1 if any(o.verdict == "wrong" for o in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
