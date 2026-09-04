"""What a worker may do, and what happens when it is asked to do more.

The capability that carries weight is EXECUTE: it is the only stage that runs code
against a warehouse. A fleet where most workers cannot do it at all is a different
security story from one where they merely choose not to, and that difference has to
survive a bug in the scheduler — so it is enforced twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from themis.capabilities import (
    DEFAULT_CAPABILITIES,
    Capability,
    CapabilityError,
    parse_capabilities,
    require,
)


def test_the_default_fleet_cannot_reach_a_warehouse() -> None:
    """Warehouse access should be something someone turned on, not forgot to prevent."""
    assert Capability.EXECUTE not in DEFAULT_CAPABILITIES
    assert Capability.ANALYSE in DEFAULT_CAPABILITIES


def test_capabilities_are_read_from_a_list() -> None:
    assert parse_capabilities("analyse,compile") == frozenset(
        {Capability.ANALYSE, Capability.COMPILE}
    )


def test_all_is_spelled_out_rather_than_implied() -> None:
    assert parse_capabilities("all") == frozenset(Capability)
    assert parse_capabilities("") == DEFAULT_CAPABILITIES
    assert parse_capabilities(None) == DEFAULT_CAPABILITIES


def test_an_unknown_capability_is_refused_not_ignored() -> None:
    """Silently dropping a typo would grant a fleet less than its operator believes."""
    with pytest.raises(CapabilityError, match="unknown capability"):
        parse_capabilities("analyse,excute")


def test_requiring_a_held_capability_passes() -> None:
    require(frozenset({Capability.EXECUTE}), Capability.EXECUTE, what="Stage 3")


def test_requiring_a_missing_capability_says_what_is_held() -> None:
    with pytest.raises(CapabilityError, match="analyse"):
        require(frozenset({Capability.ANALYSE}), Capability.EXECUTE, what="Stage 3")


def test_execution_refuses_without_the_capability(tmp_path: Path) -> None:
    """The second gate. A run reaching the wrong worker must not build anyway.

    It is reported as a skip with a reason rather than raised: the deterministic
    findings are still worth returning, and the report has to say what was left out.
    """
    from themis.config import Settings
    from themis.execute.runner import execute

    result = execute(
        tmp_path,
        base="main",
        head="HEAD",
        models=("m",),
        settings=Settings(),
        capabilities=frozenset({Capability.ANALYSE}),
    )
    assert not result.ran
    assert "execute" in (result.skipped_reason or "")


def test_a_worker_without_execute_does_not_claim_a_run_that_needs_it(tmp_path: Path) -> None:
    """The first gate. Leaving the run queued lets a capable worker take it.

    Claiming it and skipping Stage 3 would return a review quietly missing its
    strongest evidence, which reads exactly like a review that found nothing.
    """
    from themis.db.base import get_engine, session_scope
    from themis.db.models import Base
    from themis.db.store import claim_next_run, enqueue_run

    url = f"sqlite:///{tmp_path / 'q.db'}"
    Base.metadata.create_all(get_engine(url))

    with session_scope(url) as session:
        enqueue_run(
            session,
            project="demo_project",
            base_ref="main",
            head_ref="HEAD",
            execute=True,
        )

    with session_scope(url) as session:
        assert claim_next_run(session, worker_id="w1", timeout_s=60, can_execute=False) is None

    with session_scope(url) as session:
        claimed = claim_next_run(session, worker_id="w2", timeout_s=60, can_execute=True)
        assert claimed is not None
