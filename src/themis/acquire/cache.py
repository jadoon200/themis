"""A cache for compiled manifests, keyed by the revision that produced them.

dbt writes its manifest into ``target/``, which is gitignored in every project that
has one. So a manifest is never something a review can find lying in the repository —
it is something THEMIS compiles. It compiles the base revision on every single review,
and the corpus compiles the *same* base revision once per mutation, which is the same
few seconds of dbt startup paid thirty-eight times over for a byte-identical result.

A compiled manifest is a pure function of the code at a git SHA, so it is content
addressable: compile once, keep it under ``.themis/`` (already gitignored), and every
later review of that revision reads it instead.

**Except when it is not a pure function of the code.** A macro that calls ``run_query``
builds its SQL from whatever the warehouse held at compile time, so the same revision
compiles differently as data moves. Caching that would serve a manifest whose compiled
SQL describes last week's data, and the semantic diff would then report changes nobody
made — or, far worse, stay silent about ones they did. Those projects are detected from
the manifest itself and never cached.

Only revisions are cached, never a working tree: a dirty checkout is not described by
any SHA, so there is no honest key for it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from themis.logging import get_logger
from themis.snapshot import ProjectSnapshot

log = get_logger(__name__)

_CACHE_DIRNAME = "manifests"


@dataclass(frozen=True)
class CacheKey:
    """What a compiled manifest actually depends on.

    The target is part of it because it decides the catalog and schema a ``ref()``
    compiles to — two targets produce different SQL from identical code, and serving
    one for the other would put the wrong relation names in front of every rule.
    """

    revision: str
    target: str
    project: str

    def filename(self) -> str:
        digest = hashlib.sha256(self.project.encode()).hexdigest()[:8]
        return f"{self.revision[:12]}-{self.target}-{digest}.json"


class ManifestCache:
    """Compiled manifests on disk, addressed by the revision that produced them."""

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self._root = root / _CACHE_DIRNAME
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def path_for(self, key: CacheKey) -> Path:
        return self._root / key.filename()

    def get(self, key: CacheKey) -> Path | None:
        """The cached manifest for a revision, or None.

        A file that cannot be read as JSON is treated as a miss and removed rather
        than raised on: a half-written cache entry must cost a recompile, never a run.
        """
        if not self._enabled:
            return None
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            log.warning("manifest_cache.corrupt", path=str(path))
            path.unlink(missing_ok=True)
            return None
        log.info("manifest_cache.hit", revision=key.revision[:8], target=key.target)
        return path

    def put(self, key: CacheKey, manifest: Path, snapshot: ProjectSnapshot) -> Path | None:
        """Store a compiled manifest, unless this project's SQL depends on its data.

        Returns the cached path, or None when caching was refused — the refusal is a
        real answer and gets logged, because a project silently never caching looks
        exactly like a cache that is not working.
        """
        if not self._enabled:
            return None

        generated = snapshot.data_dependent_models()
        if generated:
            log.info(
                "manifest_cache.refused",
                reason="compiled SQL is built from query results, not from code alone",
                models=len(generated),
            )
            return None

        destination = self.path_for(key)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Written beside and moved into place, so a reader never sees a partial
            # file — a half-written manifest is indistinguishable from a real one
            # until something deep in the pipeline reads a missing node.
            staging = destination.with_suffix(".partial")
            staging.write_bytes(manifest.read_bytes())
            staging.replace(destination)
        except OSError as exc:
            log.warning("manifest_cache.write_failed", error=str(exc)[:200])
            return None

        log.info("manifest_cache.stored", revision=key.revision[:8], target=key.target)
        return destination

    def clear(self) -> int:
        """Drop every entry. Returns how many were removed."""
        if not self._root.exists():
            return 0
        removed = 0
        for path in self._root.glob("*.json"):
            path.unlink(missing_ok=True)
            removed += 1
        return removed
