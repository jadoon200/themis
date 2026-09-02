"""Read a dbt ``manifest.json`` into a ``ProjectSnapshot``.

The distinction that matters here is ``dbt parse`` versus ``dbt compile``. Parse gives
the DAG and configs but leaves ``raw_code`` full of unexpanded Jinja; only compile
populates ``compiled_code``. Against a macro-heavy project the difference is the
difference between analysing the SQL and guessing at it, so the loader reports which
one it got and the pipeline refuses to pretend.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from themis.logging import get_logger
from themis.models import Backend
from themis.snapshot import (
    ColumnSchema,
    DeclaredTest,
    Exposure,
    MacroNode,
    ModelNode,
    ProjectSnapshot,
)

log = get_logger(__name__)


class ManifestError(RuntimeError):
    """The manifest is absent or not shaped like a dbt manifest."""


def _as_tuple(value: Any) -> tuple[str, ...]:
    """dbt writes single-valued configs as a scalar and multi-valued as a list."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    return ()


def _hook_texts(value: Any) -> tuple[str, ...]:
    """Hook SQL, which dbt records as a string, a dict, or a list of either."""
    if value is None:
        return ()
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict):
            out.append(str(item.get("sql", "")))
        else:
            out.append(str(item))
    return tuple(text for text in out if text)


def _model_from_node(unique_id: str, node: dict[str, Any]) -> ModelNode:
    config = node.get("config") or {}
    contract = node.get("contract") or {}
    columns = tuple(
        ColumnSchema(
            name=str(name),
            data_type=col.get("data_type"),
            description=col.get("description"),
        )
        for name, col in (node.get("columns") or {}).items()
    )
    depends_on = node.get("depends_on") or {}
    return ModelNode(
        name=str(node.get("name", unique_id.split(".")[-1])),
        unique_id=unique_id,
        file_path=str(node.get("original_file_path", "")),
        resource_type=str(node.get("resource_type", "model")),
        patch_path=node.get("patch_path"),
        raw_sql=str(node.get("raw_code") or ""),
        # Absent on a parse-only manifest. Left as None rather than falling back to
        # raw_code, so downstream code cannot accidentally parse Jinja as SQL.
        compiled_sql=node.get("compiled_code"),
        materialization=str(config.get("materialized", "view")),
        incremental_strategy=config.get("incremental_strategy"),
        unique_key=_as_tuple(config.get("unique_key")),
        on_schema_change=config.get("on_schema_change"),
        tags=_as_tuple(config.get("tags")) or _as_tuple(node.get("tags")),
        meta={str(k): str(v) for k, v in (config.get("meta") or {}).items()},
        columns=columns,
        contract_enforced=bool(contract.get("enforced", False)),
        properties={str(k): str(v) for k, v in (config.get("properties") or {}).items()},
        pre_hooks=_hook_texts(config.get("pre-hook") or config.get("pre_hook")),
        post_hooks=_hook_texts(config.get("post-hook") or config.get("post_hook")),
        depends_on_models=tuple(
            n for n in depends_on.get("nodes", []) if str(n).startswith(("model.", "seed."))
        ),
        depends_on_macros=tuple(depends_on.get("macros", []) or ()),
        depends_on_sources=tuple(
            n for n in depends_on.get("nodes", []) if str(n).startswith("source.")
        ),
    )


def _tests_from_nodes(nodes: dict[str, Any]) -> tuple[DeclaredTest, ...]:
    """Extract declared tests.

    Frequently comes back near-empty. That is the whole reason grain is derived
    rather than read, and the emptiness is itself reported.
    """
    tests: list[DeclaredTest] = []
    for unique_id, node in nodes.items():
        if node.get("resource_type") != "test":
            continue
        attached = node.get("attached_node") or ""
        depends = (node.get("depends_on") or {}).get("nodes") or []
        target = attached or (depends[0] if depends else "")
        if not target:
            continue
        test_meta = node.get("test_metadata") or {}
        column = node.get("column_name")
        tests.append(
            DeclaredTest(
                test_name=str(test_meta.get("name") or unique_id.split(".")[-1]),
                model_name=str(target).split(".")[-1],
                columns=(str(column),) if column else (),
                severity=str((node.get("config") or {}).get("severity", "error")),
            )
        )
    return tuple(tests)


def load_manifest(path: Path, *, revision: str, backend: Backend) -> ProjectSnapshot:
    """Build a snapshot from a manifest on disk."""
    if not path.exists():
        raise ManifestError(f"no manifest at {path}")
    try:
        payload: dict[str, Any] = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest at {path} is not valid JSON: {exc}") from exc

    nodes: dict[str, Any] = payload.get("nodes") or {}
    models = {
        node["name"]: _model_from_node(uid, node)
        for uid, node in nodes.items()
        if node.get("resource_type") in ("model", "seed")
    }
    macros = {
        node["name"]: MacroNode(
            name=str(node["name"]),
            unique_id=uid,
            file_path=str(node.get("original_file_path", "")),
            raw_sql=str(node.get("macro_sql") or ""),
            depends_on_macros=tuple((node.get("depends_on") or {}).get("macros", []) or ()),
        )
        for uid, node in (payload.get("macros") or {}).items()
        # Package macros are noise; only the project's own can be edited in a PR.
        if not str(node.get("package_name", "")).startswith("dbt")
    }
    exposures = {
        node["name"]: Exposure(
            name=str(node["name"]),
            exposure_type=str(node.get("type", "unknown")),
            depends_on=tuple((node.get("depends_on") or {}).get("nodes", []) or ()),
            owner=((node.get("owner") or {}).get("name")),
        )
        for node in (payload.get("exposures") or {}).values()
    }

    # child_map is keyed by unique_id; the rest of THEMIS works in model names.
    by_uid = {m.unique_id: name for name, m in models.items()}
    child_map: dict[str, tuple[str, ...]] = {}
    for uid, children in (payload.get("child_map") or {}).items():
        parent = by_uid.get(uid)
        if parent is None:
            continue
        named = tuple(by_uid[c] for c in children if c in by_uid)
        if named:
            child_map[parent] = named

    snapshot = ProjectSnapshot(
        revision=revision,
        backend=backend,
        models=models,
        macros=macros,
        tests=_tests_from_nodes(nodes),
        exposures=exposures,
        child_map=child_map,
    )
    log.info(
        "manifest.loaded",
        revision=revision[:8],
        models=len(models),
        macros=len(macros),
        tests=len(snapshot.tests),
        compiled=snapshot.has_compiled_sql,
    )
    if not snapshot.has_compiled_sql:
        # Loud, not silent. With macro-heavy models a parse-only manifest cannot
        # support the analysis that follows, and a reviewer must know that.
        log.warning(
            "manifest.not_compiled",
            hint="manifest has no compiled_code; run `dbt compile`, not `dbt parse`",
        )
    return snapshot
