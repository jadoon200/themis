"""structlog console logging, matching the sibling projects."""

from __future__ import annotations

import logging
import sys

import structlog


class _CurrentStderr:
    """Whatever ``sys.stderr`` is at the moment of writing, not when logging was set up.

    Handing structlog ``sys.stderr`` itself binds the stream object that existed at
    configure time. Anything that swaps stderr for a while — a CLI test runner, an
    embedding host — then closes that object, and every later log line raises "I/O
    operation on closed file", from code that has nothing to do with logging.
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return sys.stderr.isatty()


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
    # httpx logs every request at INFO; a model call per tool step made that most of the
    # output. Its warnings and errors still come through.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=_CurrentStderr()),  # type: ignore[arg-type]
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
