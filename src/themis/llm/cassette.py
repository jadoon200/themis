"""Recorded model responses, so the model path can be tested without a model.

A merge gate has to be deterministic and replayable. Downloading a model into a CI
runner to get a non-deterministic answer would be neither, and running no model at all
leaves the specialists, the self-check and the explain pass covered only by unit tests
against a fake — which proves the wiring works but never that the real prompts produce
parseable, grounded output.

A cassette is recorded once against the real model and replayed thereafter. The key is
a hash of the prompt, so changing a prompt invalidates its recording rather than
silently replaying an answer to a different question.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from themis.llm.provider import LLMError, Provider, Response, Usage
from themis.logging import get_logger

log = get_logger(__name__)


def cassette_key(*, system: str, prompt: str, model: str) -> str:
    """A stable key for one exchange.

    The prompt is part of the key on purpose. If a prompt is edited, its recording no
    longer matches and the test fails loudly — which is correct, because a recorded
    answer to the old question says nothing about the new one.
    """
    payload = "\x1f".join((model, system, prompt))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


class Cassette:
    """A file of recorded exchanges."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            self._entries = json.loads(path.read_text())

    def get(self, key: str) -> dict[str, Any] | None:
        return self._entries.get(key)

    def put(self, key: str, payload: dict[str, Any], *, note: str) -> None:
        # The note is for a human reading the file; nothing reads it back.
        self._entries[key] = {"_note": note, **payload}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2, sort_keys=True) + "\n")

    def __len__(self) -> int:
        return len(self._entries)


class ReplayProvider:
    """Replays a cassette. Fails loudly on an unrecorded prompt."""

    def __init__(self, cassette: Cassette) -> None:
        self._cassette = cassette
        self.misses: list[str] = []

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any], model: str) -> Response:
        key = cassette_key(system=system, prompt=prompt, model=model)
        entry = self._cassette.get(key)
        if entry is None:
            self.misses.append(key)
            raise LLMError(
                f"no recorded response for {key[:12]}. Re-record with "
                "`make record-cassette` after changing a prompt."
            )
        payload = {k: v for k, v in entry.items() if not k.startswith("_")}
        return Response(payload=payload, usage=Usage(calls=1))


class RecordingProvider:
    """Calls a real provider and writes what comes back into a cassette."""

    def __init__(self, inner: Provider, cassette: Cassette) -> None:
        self._inner = inner
        self._cassette = cassette

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any], model: str) -> Response:
        response = self._inner.complete(system=system, prompt=prompt, schema=schema, model=model)
        key = cassette_key(system=system, prompt=prompt, model=model)
        # The first line of the prompt identifies the exchange well enough for someone
        # reviewing a cassette diff to see what changed.
        note = prompt.strip().splitlines()[0][:80] if prompt.strip() else "(empty prompt)"
        self._cassette.put(key, response.payload, note=note)
        log.info("cassette.recorded", key=key[:12], model=model)
        return response
