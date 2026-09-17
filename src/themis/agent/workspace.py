"""What the tools read: one project, and optionally the review of a change to it.

Built three ways — from a compiled manifest alone (exploring a project), from a review run
in this process (investigating a change), or with execution results attached (asking what
moved). Expensive analysis is computed on first use and kept, so an agent that only asks
about lineage never pays for grain, and one that asks twice pays once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from themis.acquire.manifest import load_manifest
from themis.analyze.grain import infer_grains
from themis.analyze.lineage import ColumnGraph, build_column_graph
from themis.analyze.volatility import volatile_columns
from themis.conventions import Convention
from themis.execute.runner import ExecutionResult
from themis.models import Backend, Finding, Grain
from themis.snapshot import ProjectSnapshot


@dataclass
class Workspace:
    after: ProjectSnapshot
    before: ProjectSnapshot | None = None
    changed_models: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    execution: ExecutionResult | None = None
    conventions: tuple[Convention, ...] = ()
    dialect: str = "trino"
    _grains: dict[str, Grain] | None = field(default=None, repr=False)
    _lineage: ColumnGraph | None = field(default=None, repr=False)
    _traced: frozenset[str] = field(default=frozenset(), repr=False)
    _volatile: dict[str, frozenset[str]] | None = field(default=None, repr=False)

    @classmethod
    def from_manifest(
        cls,
        manifest: Path,
        *,
        dialect: str = "trino",
        conventions: tuple[Convention, ...] = (),
    ) -> Workspace:
        snapshot = load_manifest(manifest, revision="manifest", backend=Backend.MANIFEST)
        return cls(after=snapshot, dialect=dialect, conventions=conventions)

    @classmethod
    def from_review(
        cls,
        result: object,
        *,
        dialect: str = "trino",
        conventions: tuple[Convention, ...] = (),
    ) -> Workspace:
        from themis.pipeline import ReviewResult

        if not isinstance(result, ReviewResult) or result.acquired is None:
            raise ValueError("a workspace needs a review run in this process")
        acquired = result.acquired
        workspace = cls(
            after=acquired.after,
            before=acquired.before,
            changed_models=result.models_reviewed,
            findings=tuple(result.findings),
            execution=result.execution,
            conventions=conventions,
            dialect=dialect,
        )
        # The review already derived grain, and measured some of it. Reuse rather than
        # recompute, so the agent sees the same keys the findings were judged against.
        workspace._grains = dict(result.grains)
        return workspace

    @property
    def is_review(self) -> bool:
        return self.before is not None

    @property
    def grains(self) -> dict[str, Grain]:
        if self._grains is None:
            self._grains = infer_grains(self.after, dialect=self.dialect)
        return self._grains

    def lineage(self, *models: str) -> ColumnGraph:
        """Column lineage traced over at least ``models``.

        Tracing is the expensive half, so it covers what has been asked about — plus the
        changed region in a review — and grows as questions reach further.
        """
        wanted = frozenset(models) | frozenset(self.changed_models)
        if self.is_review:
            for name in self.changed_models:
                wanted |= frozenset(self.after.downstream_of(name))
        if self._lineage is None or not wanted <= self._traced:
            self._traced = self._traced | wanted
            self._lineage = build_column_graph(
                self.after, trace=set(self._traced), dialect=self.dialect
            )
        return self._lineage

    @property
    def volatile(self) -> dict[str, frozenset[str]]:
        if self._volatile is None:
            self._volatile = volatile_columns(self.after, dialect=self.dialect)
        return self._volatile
