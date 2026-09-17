"""Logs go to stderr, so what a command emits on stdout is only what it means to emit.

structlog's default printed to stdout: `suggest-tests --yaml` wrote log lines into the YAML,
`profile --json` was not valid JSON, and a review redirected into a PR comment carried the
pipeline's logs with it.
"""

from __future__ import annotations

import pytest
import structlog

from themis.logging import configure_logging, get_logger


def test_log_lines_go_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        configure_logging()
        get_logger("themis.test").info("manifest.loaded", models=3)
        captured = capsys.readouterr()
        assert "manifest.loaded" in captured.err
        assert captured.out == ""
    finally:
        structlog.reset_defaults()


def test_logging_survives_stderr_being_swapped_and_closed() -> None:
    """A CLI test runner replaces stderr and closes its stand-in afterwards. Logging bound
    to that object then raised on every later line — 72 unrelated tests failed at once."""
    import io
    import sys

    from themis.logging import configure_logging, get_logger

    original = sys.stderr
    stand_in = io.StringIO()
    sys.stderr = stand_in
    try:
        configure_logging()
        get_logger("swap").info("while.swapped")
    finally:
        sys.stderr = original
        stand_in.close()

    get_logger("swap").info("after.restored")  # must not raise
