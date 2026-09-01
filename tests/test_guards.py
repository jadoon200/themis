"""The production guard.

Stage 3 runs dbt, which means it writes to a warehouse. In a financial environment
running a build against production is not a mistake this tool gets to make once, so
the guard is an allowlist: anything unrecognised fails closed.
"""

from __future__ import annotations

import pytest

from themis.acquire.dbt_runner import UnsafeTargetError, assert_target_allowed
from themis.config import Settings

ALLOWED = ("dev", "ci", "duckdb", "test", "local")


@pytest.mark.parametrize("target", ALLOWED)
def test_recognised_dev_targets_are_allowed(target: str) -> None:
    assert_target_allowed(target, ALLOWED)


@pytest.mark.parametrize(
    "target",
    ["prod", "production", "PROD", "prd", "live", "warehouse", "default", ""],
)
def test_unrecognised_targets_are_refused(target: str) -> None:
    with pytest.raises(UnsafeTargetError):
        assert_target_allowed(target, ALLOWED)


def test_guard_fails_closed_on_a_typo() -> None:
    """A typo must not fall through to whatever profile happens to be default."""
    with pytest.raises(UnsafeTargetError):
        assert_target_allowed("dve", ALLOWED)


def test_refusal_names_the_target_and_the_allowlist() -> None:
    """The error has to be actionable; a bare refusal invites disabling the guard."""
    with pytest.raises(UnsafeTargetError) as excinfo:
        assert_target_allowed("prod", ALLOWED)
    message = str(excinfo.value)
    assert "prod" in message
    assert "dev" in message


def test_execution_is_off_by_default() -> None:
    """The one stage that runs code is opt-in."""
    assert Settings().execute_enabled is False


def test_gate_is_advisory_by_default() -> None:
    """A review that blocks every merge stops being read."""
    assert Settings().fail_on_severity is None


def test_default_allowlist_excludes_production_names() -> None:
    allowed = set(Settings().execute_allowed_targets)
    assert not allowed & {"prod", "production", "live", "default"}
