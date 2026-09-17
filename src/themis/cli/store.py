"""What stored reviews answer: follow-up questions and the captured dataset."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from themis.cli._app import (
    VerboseOpt,
    app,
)
from themis.config import load_settings
from themis.logging import configure_logging


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What to ask about a completed review.")],
    run: Annotated[str, typer.Option("--run", help="Run id, or 'latest'.")] = "latest",
    verbose: VerboseOpt = False,
) -> None:
    """Ask a grounded follow-up question about a completed review.

    Answers come only from the persisted run artifact. A question the artifact cannot
    answer gets a refusal, never an inference.
    """
    from themis.ask.answer import answer_question
    from themis.ask.retrieval import gather, latest_run, run_by_key
    from themis.db.base import session_scope
    from themis.llm.provider import build_provider

    configure_logging(verbose=verbose)
    settings = load_settings()

    with session_scope() as session:
        stored = latest_run(session) if run == "latest" else run_by_key(session, run)
        if stored is None:
            typer.echo(
                "No completed review to ask about."
                if run == "latest"
                else f"No review with key {run}.",
                err=True,
            )
            raise typer.Exit(code=2)

        facts = gather(session, stored, question)
        # The refusal for an unexamined model already says why the review is silent.
        context_is_empty = facts.is_empty and not facts.unknown_entities
        run_key = stored.run_key

        # Answering happens inside the session because the facts are ORM rows; nothing
        # is written, and the model is never given a database handle.
        result = answer_question(
            question, facts, provider=build_provider(settings), settings=settings
        )

    typer.echo(f"[{run_key}]")
    typer.echo("")

    if result.grounded:
        typer.echo(result.text)
        if result.evidence_quote:
            typer.echo("")
            typer.echo(f"  based on: {result.evidence_quote}")
        raise typer.Exit(code=0)

    typer.echo(f"Cannot answer from this review: {result.refusal_reason}")
    if context_is_empty:
        typer.echo("")
        typer.echo(
            "This review recorded nothing about what you asked. That may itself be the "
            "answer — or the question is about something the review did not cover."
        )
    raise typer.Exit(code=1)


@app.command()
def dataset(
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the calls here as JSONL. Omitted: counts only."),
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", help="Only this project's runs.")
    ] = None,
    judged_only: Annotated[
        bool,
        typer.Option("--judged-only", help="Only calls a human later ruled on."),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """What every model call was shown, what it answered, and how it was later judged.

    The tuning set, accumulated from real reviews rather than written alongside the
    rules. Run it with no `--out` to see whether there is yet enough to tune on: a few
    hundred judged calls across more than one project is the bar, and until then this
    prints the honest number.

    The output contains the SQL under review verbatim, because that is what the model
    was shown. Treat the file the way you would treat the repository.
    """
    from themis.db.base import session_scope
    from themis.db.store import export_calls

    configure_logging(verbose=verbose)

    try:
        with session_scope() as session:
            rows = export_calls(session, project=project, judged_only=judged_only)
    except Exception as exc:
        typer.echo(f"Could not read the store: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    judged = sum(1 for row in rows if row["human_disposition"])
    by_seat: dict[str, int] = {}
    for row in rows:
        seat = str(row["seat"])
        by_seat[seat] = by_seat.get(seat, 0) + 1

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row, default=str) + "\n")
        typer.echo(f"{len(rows)} call(s) written to {out}")
    else:
        typer.echo(f"{len(rows)} captured call(s)")

    for seat, count in sorted(by_seat.items()):
        typer.echo(f"  {seat}: {count}")
    typer.echo(f"{judged} of them carry a human judgement.")
    if judged < 100:
        # Said every time, because the number is the whole point: a model tuned on a
        # handful of judgements learns the handful.
        typer.echo(
            "Too few to tune on. The bar is hundreds of real judgements across more "
            "than one project, plus a held-out set of real pull requests to test the "
            "result on — see docs/ROADMAP.md."
        )
