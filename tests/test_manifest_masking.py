"""Masking compile-time literals when a manifest is loaded.

The loader is the one place that knows which invocation produced the compiled SQL, so it
is where two compiles of identical code are made to compare equal.
"""

from __future__ import annotations

import json
from pathlib import Path

from themis.acquire.manifest import load_manifest
from themis.analyze.volatility import INVOCATION_ID, RUN_STARTED_AT
from themis.models import Backend

_RAW = "select '{{ run_started_at }}' as a, '{{ invocation_id }}' as b"


def _manifest(tmp_path: Path, invocation: str, started: str, rendered: str) -> Path:
    path = tmp_path / f"{invocation}.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "invocation_id": invocation,
                    "invocation_started_at": started,
                    "generated_at": started,
                },
                "nodes": {
                    "model.d.m": {
                        "resource_type": "model",
                        "name": "m",
                        "original_file_path": "models/m.sql",
                        "raw_code": _RAW,
                        "compiled_code": f"select '{rendered}' as a, '{invocation}' as b",
                        "config": {"materialized": "table"},
                        "depends_on": {"nodes": [], "macros": []},
                    }
                },
                "child_map": {},
            }
        )
    )
    return path


def test_two_compiles_of_identical_code_load_identical(tmp_path: Path) -> None:
    first = load_manifest(
        _manifest(
            tmp_path,
            "11111111-aaaa",
            "2026-09-17T15:25:55.055285Z",
            "2026-09-17 15:25:55.055542+00:00",
        ),
        revision="a",
        backend=Backend.MANIFEST,
    )
    second = load_manifest(
        _manifest(
            tmp_path,
            "22222222-bbbb",
            "2026-09-17T15:25:58.191245Z",
            "2026-09-17 15:25:58.191384+00:00",
        ),
        revision="b",
        backend=Backend.MANIFEST,
    )
    assert first.models["m"].compiled_sql == second.models["m"].compiled_sql
    assert RUN_STARTED_AT in (first.models["m"].compiled_sql or "")
    assert INVOCATION_ID in (first.models["m"].compiled_sql or "")


def test_raw_code_is_never_touched(tmp_path: Path) -> None:
    loaded = load_manifest(
        _manifest(tmp_path, "11111111-aaaa", "2026-09-17T15:25:55Z", "2026-09-17 15:25:55.1+00:00"),
        revision="a",
        backend=Backend.MANIFEST,
    )
    assert (
        loaded.models["m"].raw_sql
        == "select '{{ run_started_at }}' as a, '{{ invocation_id }}' as b"
    )
