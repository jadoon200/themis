"""THEMIS command line interface.

Every command is a thin wrapper over the pipeline stages so that CI, a developer's shell,
and the eval harness all exercise exactly the same code path. Commands live in modules by
purpose; importing them here registers them on the one Typer app, in help order.
"""

from themis.cli import agent, evaluate, explore, review, serve, setup, store  # noqa: F401
from themis.cli._app import app
from themis.cli._shared import EXIT_INCOMPLETE, _gate_exit_code, _review_exit_code

__all__ = ["EXIT_INCOMPLETE", "_gate_exit_code", "_review_exit_code", "app"]
