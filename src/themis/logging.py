"""structlog console logging, matching the sibling projects."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(*, verbose: bool = False) -> None:
    """Human-readable console logging. Called once, from the CLI."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
