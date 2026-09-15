"""structlog console logging, matching the sibling projects."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(*, verbose: bool = False) -> None:
    """Human-readable console logging, on stderr. Called once, from the CLI.

    stderr, explicitly. structlog's default logger prints to stdout, so every log line
    landed in the middle of what commands exist to emit — `suggest-tests --yaml` wrote log
    lines into the YAML, `profile --json` was not JSON, and a review redirected into a PR
    comment carried the pipeline's logs with it. Colour only when stderr is a terminal,
    so a CI log is not full of escape codes.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
