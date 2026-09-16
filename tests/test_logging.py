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
