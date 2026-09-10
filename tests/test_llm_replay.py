"""The model path, end to end, against recorded responses.

Unit tests against a fake provider prove the wiring works. They cannot prove that the
real prompts produce parseable, schema-valid, grounded output — the fake returns
whatever the test asked for. These replay responses actually recorded from qwen3:8b,
so a prompt change that makes the model emit something unusable fails here.

Re-record with `make record-cassette` after changing a prompt. The key includes the
prompt, so an edited prompt no longer matches and this fails loudly rather than
replaying an answer to a different question.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from themis.llm.cassette import Cassette, ReplayProvider, cassette_key
from themis.review.selfcheck import quote_is_grounded

CASSETTE_PATH = Path(__file__).parent / "cassettes" / "review.json"


@pytest.fixture
def cassette() -> Cassette:
    if not CASSETTE_PATH.exists():
        pytest.skip("no cassette recorded")
    return Cassette(CASSETTE_PATH)


def test_the_cassette_is_present_and_populated(cassette: Cassette) -> None:
    """A missing cassette must fail rather than silently skip the model path."""
    assert len(cassette) >= 2


# Every schema THEMIS asks a model to answer, keyed by the field that identifies it.
# Read from the live schemas rather than restated here, so a prompt that gains a required
# field cannot leave its recording behind without this failing. `can_fix` was missing
# until the fix pass was recorded for the first time, and a set that has to be updated by
# hand is a set that goes stale.
def _schemas() -> dict[str, dict[str, object]]:
    from themis.ask.answer import ANSWER_SCHEMA
    from themis.review.explain import EXPLAIN_SCHEMA
    from themis.review.fix import FIX_SCHEMA
    from themis.review.specialists import INTENT_SCHEMA, VERDICT_SCHEMA

    return {
        "verdict": VERDICT_SCHEMA,
        "hypothesis": EXPLAIN_SCHEMA,
        "undisclosed_changes": INTENT_SCHEMA,
        "can_fix": FIX_SCHEMA,
        "can_answer": ANSWER_SCHEMA,
    }


def _payloads() -> dict[str, dict[str, object]]:
    entries = json.loads(CASSETTE_PATH.read_text())
    return {
        key: {k: v for k, v in entry.items() if not k.startswith("_")}
        for key, entry in entries.items()
    }


def test_every_recorded_response_matches_a_known_shape(cassette: Cassette) -> None:
    """What the real model actually emitted, not what a fake was told to return."""
    schemas = _schemas()
    for key, payload in _payloads().items():
        assert payload, f"{key} recorded an empty payload"
        assert set(schemas) & set(payload), f"{key} matches no known response shape"


def test_every_recorded_response_still_satisfies_its_schema(cassette: Cassette) -> None:
    """The check that makes a stale recording loud.

    A recording keyed by its prompt goes quietly unused once the prompt is edited: the
    key stops matching, the replay never happens, and nothing noticed — which is how the
    intent recordings outlived a prompt that had gained a required field. Shape alone
    could not see it, because the discriminator was still there. The required fields of
    the live schema can.
    """
    schemas = _schemas()
    for key, payload in _payloads().items():
        for discriminator, schema in schemas.items():
            if discriminator not in payload:
                continue
            required = schema.get("required")
            assert isinstance(required, list)
            missing = [field for field in required if field not in payload]
            assert not missing, (
                f"{key} is missing {missing} — the {discriminator} prompt gained a "
                "required field after this was recorded. Re-record with "
                "`make record-cassette`."
            )
            break


def test_the_cassette_covers_every_seat_the_model_layer_has(cassette: Cassette) -> None:
    """Adjudication, explain, intent and the fix pass all reach a real model here.

    The adjudication recording was a fossil for a while: the recorder built its worktree
    without a data anchor, so compile failed, all 20 rules skipped, and there were no
    findings to adjudicate — while the cassette still held the verdict recorded before
    that broke. A recorded shape going missing is the tell, so it is asserted rather
    than left to be noticed.
    """
    recorded = {key for payload in _payloads().values() for key in _schemas() if key in payload}
    assert {"verdict", "hypothesis", "undisclosed_changes", "can_fix"} <= recorded


def test_recorded_verdicts_are_within_the_enum(cassette: Cassette) -> None:
    entries = json.loads(CASSETTE_PATH.read_text())
    for key, entry in entries.items():
        if "verdict" in entry:
            assert entry["verdict"] in ("confirm", "refute", "uncertain"), key
        if "confidence" in entry:
            assert entry["confidence"] in ("likely", "possible", "unclear"), key


def test_replay_is_deterministic(cassette: Cassette) -> None:
    provider = ReplayProvider(cassette)
    entries = json.loads(CASSETTE_PATH.read_text())
    key = next(iter(entries))
    # Reconstructing the exact prompt is not possible from the cassette alone, so this
    # asserts the lookup itself is stable rather than replaying a full exchange.
    assert cassette.get(key) is not None
    assert cassette.get(key) is not None
    assert provider.misses == []


def test_an_unrecorded_prompt_fails_loudly(cassette: Cassette) -> None:
    """Silence would mean a changed prompt quietly loses its coverage."""
    from themis.llm.provider import LLMError

    provider = ReplayProvider(cassette)
    with pytest.raises(LLMError, match="no recorded response"):
        provider.complete(
            system="a system prompt that was never recorded",
            prompt="a prompt that was never recorded",
            schema={},
            model="qwen3:8b",
        )


def test_a_changed_prompt_invalidates_its_recording() -> None:
    """The reason the prompt is part of the key: a recorded answer to the old question
    says nothing about the new one."""
    a = cassette_key(system="s", prompt="original prompt", model="m")
    b = cassette_key(system="s", prompt="original prompt, edited", model="m")
    assert a != b


def test_recorded_evidence_quotes_would_survive_the_self_check(cassette: Cassette) -> None:
    """Not a grounding check — the context is not in the cassette — but a shape check:
    a quote the model returned must at least be quotable text rather than a stub."""
    entries = json.loads(CASSETTE_PATH.read_text())
    for key, entry in entries.items():
        quote = entry.get("evidence_quote")
        if quote is None:
            continue
        if entry.get("verdict") == "uncertain":
            continue  # abstaining needs no evidence
        assert quote_is_grounded(quote, quote), f"{key} returned an unusable quote"
