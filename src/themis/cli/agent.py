"""The agent, and the same tools served over MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from themis.boundary import detect as detect_boundary
from themis.cli._app import (
    _REVIEW_ERRORS,
    HeadOpt,
    ProjectOpt,
    VerboseOpt,
    app,
)
from themis.cli._shared import (
    _history,
)
from themis.config import load_settings
from themis.logging import conceal_values, configure_logging
from themis.report.conceal import scrub, sensitive_values


@app.command()
def agent(
    question: Annotated[
        str | None,
        typer.Argument(
            help="What to find out. Omit to ask several questions against one loaded review."
        ),
    ] = None,
    project: ProjectOpt = Path("demo_project"),
    base: Annotated[
        str | None,
        typer.Option("--base", help="Review a change from this revision. Omit to explore only."),
    ] = None,
    head: HeadOpt = "HEAD",
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="Explore a compiled manifest, with no change to review."),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute/--no-execute", help="Build both revisions so the agent can ask what moved."
        ),
    ] = False,
    max_steps: Annotated[int, typer.Option("--max-steps", help="Tool calls allowed.")] = 6,
    as_json: Annotated[bool, typer.Option("--json", help="Print the outcome as JSON.")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Ask a question the agent answers by calling THEMIS's tools, and must prove.

    Every claim in the answer quotes a tool result verbatim, or the answer is refused. With
    --base it reviews the change first and can use the findings; with --manifest it explores
    a compiled project; with --execute it can also ask what building both revisions measured.

    With no question it keeps the review loaded and reads questions one per line until an
    empty line or end of input — compiling both revisions once instead of once a question.

    Exit codes: 0 a grounded answer, 1 refused, 2 could not start. Interactively, 0.
    """
    from themis import conventions
    from themis.agent.loop import agent_provider, investigate
    from themis.agent.workspace import Workspace

    configure_logging(verbose=verbose)
    settings = load_settings()
    # THEMIS's own model reasons over real values; an AI assistant reading the answer may
    # not see them (themis/boundary.py).
    boundary = detect_boundary(project, target="dev", settings=settings)
    conceal_values(boundary.conceal)
    known: tuple[str, ...] = ()

    try:
        if base is not None:
            from themis.pipeline import review as run_review

            result = run_review(
                project,
                base=base,
                head=head,
                settings=settings,
                run_execution=execute,
                run_llm=False,
                history=_history(str(project), settings),
            )
            if result.execution is not None:
                known = sensitive_values(result.execution.deltas.values())
            workspace = Workspace.from_review(
                result,
                dialect=settings.dialect,
                conventions=conventions.load_at(project, head).conventions,
            )
        else:
            path = manifest or project / "target" / "manifest.json"
            if not path.exists():
                typer.echo(
                    f"No manifest at {path}. Run `dbt compile` in {project}, or pass --base "
                    "to review a change.",
                    err=True,
                )
                raise typer.Exit(code=2)
            workspace = Workspace.from_manifest(
                path, dialect=settings.dialect, conventions=conventions.load(project).conventions
            )
    except _REVIEW_ERRORS as exc:
        typer.echo(f"Agent could not start: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    provider = agent_provider(settings)

    def answer(text: str) -> bool:
        outcome = investigate(
            text, workspace, provider=provider, settings=settings, max_steps=max_steps
        )
        if boundary.conceal:
            typer.echo(f"> Values withheld from this answer: {boundary.reason}.", err=True)
        _print_outcome(text, outcome, as_json=as_json, conceal=boundary.conceal, known=known)
        return outcome.grounded

    if question is not None:
        raise typer.Exit(code=0 if answer(question) else 1)

    typer.echo("Loaded. Ask a question per line; an empty line ends the session.", err=True)
    while True:
        try:
            line = input("? ").strip()
        except EOFError:
            break
        if not line:
            break
        answer(line)
        typer.echo("")
    raise typer.Exit(code=0)


def _print_outcome(
    question: str,
    outcome: Any,
    *,
    as_json: bool,
    conceal: bool = False,
    known: tuple[str, ...] = (),
) -> None:
    steps = {step.number: step for step in outcome.steps}

    def shown(text: str | None) -> str | None:
        return scrub(text, known=known) if conceal else text

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "question": question,
                    "grounded": outcome.grounded,
                    "answer": shown(outcome.answer) or None,
                    "refusal_reason": shown(outcome.refusal_reason),
                    "citations": [
                        {"result": c.result, "tool": steps[c.result].tool, "quote": shown(c.quote)}
                        for c in outcome.citations
                        if c.result in steps
                    ],
                    "steps": [
                        {
                            "n": s.number,
                            "tool": s.tool,
                            "arguments": s.arguments,
                            "ok": s.result.ok,
                            "repeated_from": s.repeated_from,
                        }
                        for s in outcome.steps
                    ],
                    "model_calls": outcome.usage.calls,
                },
                indent=2,
            )
        )
        return
    for step in outcome.steps:
        arguments = ", ".join(f"{k}={v}" for k, v in step.arguments.items())
        mark = "" if step.result.ok else "  (no result)"
        typer.echo(f"[{step.number}] {step.tool}({arguments}){mark}", err=True)
    typer.echo("")
    if outcome.grounded:
        typer.echo(shown(outcome.answer))
        typer.echo("")
        for citation in outcome.citations:
            tool = steps[citation.result].tool if citation.result in steps else "?"
            typer.echo(f'  [{citation.result}] {tool}: "{shown(citation.quote)}"')
    else:
        typer.echo(f"Could not answer: {shown(outcome.refusal_reason)}")


@app.command()
def mcp(
    project: ProjectOpt = Path("demo_project"),
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="The compiled manifest to serve. Default: target/."),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Serve the agent's tools to an MCP client over stdio.

    Tool results contain the SQL under review. A client backed by a hosted model sends
    them to that provider — connect only a local-model client to proprietary projects.
    Needs the optional SDK: pip install 'themis[mcp]'.
    """
    from themis import conventions
    from themis.agent.mcp_server import PRIVACY_NOTICE, serve
    from themis.agent.workspace import Workspace

    configure_logging(verbose=verbose)
    settings = load_settings()
    path = manifest or project / "target" / "manifest.json"
    if not path.exists():
        typer.echo(f"No manifest at {path}. Run `dbt compile` in {project} first.", err=True)
        raise typer.Exit(code=2)
    # stdout is the protocol channel; everything for a person goes to stderr.
    typer.echo(f"THEMIS MCP server for {path}. {PRIVACY_NOTICE}", err=True)
    workspace = Workspace.from_manifest(
        path, dialect=settings.dialect, conventions=conventions.load(project).conventions
    )
    try:
        serve(workspace)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
