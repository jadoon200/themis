"""The context window the model is asked for, and what happens when a prompt overflows it.

Ollama's default window is about 2,048 tokens when none is requested, and a longer prompt
is not refused: its beginning is dropped — system prompt and finding first — and the model
answers from the rest. Measured against qwen3:8b: 2,050 of 30,324 prompt tokens evaluated
and a nonsense answer, where requesting the window got every token and the right answer.

A truncated prompt must fail loudly, so the deterministic finding stands, rather than
produce a confident answer to half a question.
"""

from __future__ import annotations

import json

import httpx
import pytest

from themis.config import Settings
from themis.llm import provider as provider_module


def _reply(payload: dict[str, object], *, evaluated: int = 100) -> httpx.Response:
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/generate")
    return httpx.Response(
        200,
        json={"response": json.dumps(payload), "prompt_eval_count": evaluated},
        request=request,
    )


def test_the_context_window_is_always_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def capture(url: str, json: dict[str, object], timeout: float) -> httpx.Response:
        sent.append(json)
        return _reply({"verdict": "confirm"})

    monkeypatch.setattr(provider_module.httpx, "post", capture)
    provider_module.OllamaProvider(Settings(llm_context_window=12000)).complete(
        system="s", prompt="p", schema={}, model="m"
    )
    options = sent[0]["options"]
    assert isinstance(options, dict) and options["num_ctx"] == 12000


def test_a_prompt_too_long_for_the_window_is_refused_before_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def never(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(1)
        return _reply({})

    monkeypatch.setattr(provider_module.httpx, "post", never)
    with pytest.raises(provider_module.LLMError, match="does not fit"):
        provider_module.OllamaProvider(Settings(llm_context_window=1000)).complete(
            system="s", prompt="x" * 10_000, schema={}, model="m"
        )
    assert calls == []


def test_a_prompt_that_filled_the_window_is_rejected_not_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The estimate is rough. If the model reports it filled the window anyway, the front
    of the prompt was dropped, and the answer is to part of the question."""
    monkeypatch.setattr(
        provider_module.httpx,
        "post",
        lambda *a, **k: _reply({"verdict": "refute"}, evaluated=4000),
    )
    monkeypatch.setattr(provider_module.time, "sleep", lambda s: None)
    with pytest.raises(provider_module.LLMError, match="truncated"):
        provider_module.OllamaProvider(
            Settings(llm_context_window=4096, llm_max_output_tokens=400)
        ).complete(system="s", prompt="p", schema={}, model="m")


def test_a_truncation_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same prompt will overflow the same window every time."""
    calls: list[int] = []

    def full(*args: object, **kwargs: object) -> httpx.Response:
        calls.append(1)
        return _reply({"verdict": "refute"}, evaluated=4000)

    monkeypatch.setattr(provider_module.httpx, "post", full)
    monkeypatch.setattr(provider_module.time, "sleep", lambda s: None)
    with pytest.raises(provider_module.LLMError):
        provider_module.OllamaProvider(Settings(llm_context_window=4096, llm_retries=2)).complete(
            system="s", prompt="p", schema={}, model="m"
        )
    assert len(calls) == 1
