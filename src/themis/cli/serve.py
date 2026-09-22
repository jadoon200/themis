"""Serve the pages and the API: the one command a server at the office runs."""

from __future__ import annotations

import ipaddress
from typing import Annotated

import typer

from themis.cli._app import VerboseOpt, app
from themis.config import load_settings
from themis.logging import configure_logging


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8040,
    trust_network: Annotated[
        bool,
        typer.Option(
            "--trust-network",
            help="Bind beyond loopback although identity comes from a trusted header: "
            "only when nothing but the sign-in proxy can reach this port.",
        ),
    ] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Serve the web pages (/ui) and the JSON API on one port.

    Behind the organisation's sign-in proxy, set THEMIS_UI_TRUSTED_USER_HEADER and bind to
    loopback: the header names the person, so anything that can reach the port directly
    could name anyone. Without the header this is the demo — names are typed on the page.
    """
    import uvicorn

    configure_logging(verbose=verbose)
    settings = load_settings()
    local = _is_loopback(host)

    if settings.ui_trusted_user_header and not local and not trust_network:
        typer.echo(
            f"Refusing to bind {host}: identity comes from the {settings.ui_trusted_user_header} "
            "header, and a client that reaches this port without passing the sign-in proxy "
            "can set it to anyone. Bind 127.0.0.1 behind the proxy, or pass --trust-network "
            "if the network already guarantees that.",
            err=True,
        )
        raise typer.Exit(code=2)
    if not local and settings.ui_trusted_user_header is None:
        typer.echo(
            f"Demo mode on {host}: anyone who can reach this address can record decisions "
            "under a typed name. Set THEMIS_UI_TRUSTED_USER_HEADER behind sign-in for real use.",
            err=True,
        )
    if not local and settings.api_token is None:
        typer.echo(
            "No THEMIS_API_TOKEN: the JSON API (queueing reviews) is open to this network.",
            err=True,
        )

    typer.echo(f"THEMIS on http://{host}:{port}/ui — model host {settings.llm_base_url}", err=True)
    uvicorn.run(
        "themis.api.app:app",
        host=host,
        port=port,
        proxy_headers=True,
        log_level="info" if verbose else "warning",
    )
