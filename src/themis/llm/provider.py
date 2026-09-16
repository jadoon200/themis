"""Talking to a language model.

Two rules shape this module.

**The model never produces facts.** Every call is given facts the deterministic stages
already established and asked only to judge them. Recce's published failure was an
agent inventing DAG lineage from semantic inference; the defence is not a better prompt
but never asking the question in the first place.

**Every call is schema-constrained.** Ollama supports JSON-schema structured output,
and an 8B model asked one narrow question with a fixed output shape is a very different
proposition from one asked to reason freely. Free-form output would also have to be
parsed, and a parse failure mid-review is indistinguishable from a clean result.

Recorded responses live in ``llm/cassette.py``: a provider that replays what the real
model returned, keyed by prompt, so the model path runs in CI without a model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from themis.config import Settings
from themis.logging import get_logger

log = get_logger(__name__)


class LLMError(RuntimeError):
    """The model could not be reached, or did not return usable output."""


class _Permanent(LLMError):
    """A failure that will repeat on every attempt, so is not retried."""


@dataclass
class Usage:
    """What a call cost. Reported so the cost story is measured, not asserted."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.seconds += other.seconds


@dataclass
class Response:
    payload: dict[str, Any]
    usage: Usage = field(default_factory=Usage)


class Provider(Protocol):
    """The one operation THEMIS needs: ask a narrow question, get structured JSON."""

    def complete(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str
    ) -> Response: ...


class OllamaProvider:
    """Local Ollama. The default, because nothing leaves the machine.

    That is not only a cost decision. Reviewing a financial institution's SQL means the
    prompt contains that SQL, so a hosted model would be exfiltrating the thing under
    review.
    """

    def __init__(self, settings: Settings) -> None:
        self._base = settings.llm_base_url.rstrip("/")
        self._timeout = settings.llm_timeout_s
        self._temperature = settings.llm_temperature
        self._max_output_tokens = settings.llm_max_output_tokens
        self._retries = settings.llm_retries
        self._backoff = settings.llm_retry_backoff_s

    def complete(self, *, system: str, prompt: str, schema: dict[str, Any], model: str) -> Response:
        """One completion, retried on the failures that are worth retrying.

        A local model under load times out, drops a connection, or occasionally returns a
        truncated body that is not JSON. Each of those is transient, and without a retry
        a single blip discarded a specialist's answer or an intent pass — the deterministic
        finding stood, but the review silently had less in it than it should. A 4xx is not
        retried: a missing model or a bad request fails the same way every time.
        """
        attempts = self._retries + 1
        last: LLMError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._complete_once(system=system, prompt=prompt, schema=schema, model=model)
            except _Permanent as exc:
                raise LLMError(str(exc)) from exc
            except LLMError as exc:
                last = exc
                if attempt < attempts:
                    log.warning("llm.retrying", attempt=attempt, of=attempts, error=str(exc)[:200])
                    time.sleep(self._backoff * attempt)
        assert last is not None
        raise last

    def _complete_once(
        self, *, system: str, prompt: str, schema: dict[str, Any], model: str
    ) -> Response:
        started = time.monotonic()
        body = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            # Reasoning traces leak into the response body and break JSON parsing on
            # the larger Qwen models, and the specialists are not asked to reason
            # aloud in any case.
            "think": False,
            "format": schema,
            "options": {
                "temperature": self._temperature,
                "num_predict": self._max_output_tokens,
            },
        }
        try:
            response = httpx.post(f"{self._base}/api/generate", json=body, timeout=self._timeout)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                raise _Permanent(f"the model at {self._base} refused the request: {exc}") from exc
            raise LLMError(f"the model at {self._base} failed: {exc}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"could not reach the model at {self._base}: {exc}") from exc

        raw = str(data.get("response", "")).strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned non-JSON output: {raw[:200]}") from exc
        if not isinstance(payload, dict):
            raise LLMError(f"model returned {type(payload).__name__}, expected an object")

        return Response(
            payload=payload,
            usage=Usage(
                calls=1,
                prompt_tokens=int(data.get("prompt_eval_count", 0)),
                completion_tokens=int(data.get("eval_count", 0)),
                seconds=time.monotonic() - started,
            ),
        )


def build_provider(settings: Settings) -> Provider:
    if settings.llm_provider == "ollama":
        return OllamaProvider(settings)
    raise LLMError(f"unknown provider {settings.llm_provider!r}; supported: ollama")
