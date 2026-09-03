"""Stage 3 — build both revisions and measure what actually changed.

Everything before this stage reasons *about* the SQL. This stage runs it, and what it
produces is categorically stronger: not "this join may fan out" but "row count 1.2M to
1.68M, sum(amount_usd) 44.1M to 61.7M". A reviewer does not have to adjudicate a
measurement.

It is also the only stage that executes anything, so the production guard in
``acquire.dbt_runner`` gates every invocation and fails closed.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from themis.acquire import git
from themis.acquire.dbt_runner import assert_target_allowed, run_dbt
from themis.config import Settings
from themis.execute.differ import diff_tables, measure_grain
from themis.execute.profiles import ProfileError, read_profile, write_profile_for_schema
from themis.execute.warehouse import WarehouseClient, client_for_profile
from themis.logging import get_logger
from themis.models import ExecutionDelta, Grain

log = get_logger(__name__)


@dataclass
class ExecutionResult:
    """What Stage 3 measured, and what it could not."""

    deltas: dict[str, ExecutionDelta] = field(default_factory=dict)
    measured_grains: dict[str, Grain] = field(default_factory=dict)
    # The same measurement against the base revision, so a finding can say whether a
    # change made the grain worse rather than merely that it is bad.
    baseline_grains: dict[str, Grain] = field(default_factory=dict)
    built: tuple[str, ...] = ()
    skipped_reason: str | None = None

    @property
    def ran(self) -> bool:
        return self.skipped_reason is None

    @property
    def material_models(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, d in self.deltas.items() if d.is_material))


def _build(
    project_dir: Path,
    *,
    models: tuple[str, ...],
    schema: str,
    target: str,
    settings: Settings,
    profiles_root: Path,
    anchor_dir: Path,
    label: str,
    incremental_models: tuple[str, ...] = (),
) -> str | None:
    """Build a selection into a schema. Returns an error string, or None on success.

    Incremental models are built twice: once with ``--full-refresh`` and once without.

    The first pass is what makes the comparison deterministic. Incremental models
    append into whatever is already in the schema, so without it a run inherits state
    from whatever ran before — which silently poisoned the corpus, making
    behaviour-preserving refactors measure as defects.

    The second pass is what makes incremental *logic* measurable. Under full refresh
    ``is_incremental()`` is false and the guarded branch never runs, so a removed guard
    or a narrowed lookback would show no difference at all. Running both passes
    reproduces what production does: an established table, then an incremental load
    onto it.
    """
    profiles_dir = write_profile_for_schema(
        project_dir,
        profiles_root / label,
        target=target,
        schema=schema,
        anchor_dir=anchor_dir,
    )
    # `+model` for every model being measured — each one's full ancestor closure.
    #
    # Redirecting output to a fresh schema means every ref() resolves there too, so
    # everything a built model reads must exist in that schema or the build fails on the
    # first reference. `+model+` is not enough: it pulls in descendants but not those
    # descendants' *own* ancestors, so a downstream model joining an unrelated dimension
    # still breaks. Passing the measured set explicitly, each with its ancestors, is
    # exact — and it is also the correct comparison semantics, since the whole subgraph
    # feeding a model must come from the same revision as the model itself.
    #
    # This rebuilds more than strictly changed. At scale the dbt-native answer is
    # --defer against a production manifest, resolving unchanged upstreams to prod
    # rather than rebuilding them; that needs the dual-manifest backend.
    #
    # `dbt build` rather than `run`: seeds must be loaded into this schema too, or the
    # models have nothing to read and the comparison runs against an empty database.
    selection = [arg for model in models for arg in ("--select", f"+{model}")]
    result = run_dbt(
        project_dir,
        ["build", "--full-refresh", *selection],
        target=target,
        allowed_targets=settings.execute_allowed_targets,
        profiles_dir=profiles_dir,
        timeout_s=settings.execute_timeout_s,
    )
    if result.ok and incremental_models:
        # The second pass only needs to re-run the incremental models themselves.
        # Pass one already built their upstreams, and rebuilding the whole closure
        # again doubles the cost of every run for no additional signal.
        second = [arg for model in incremental_models for arg in ("--select", model)]
        result = run_dbt(
            project_dir,
            ["build", *second],
            target=target,
            allowed_targets=settings.execute_allowed_targets,
            profiles_dir=profiles_dir,
            timeout_s=settings.execute_timeout_s,
        )
    if not result.ok:
        # A partial build is still worth measuring — the models that did build give
        # real evidence, and the failure itself is a finding.
        message = _extract_dbt_error(result.stdout) or "dbt build failed"
        log.warning("execute.build_failed", label=label, error=message[:300])
        return message
    return None


def _extract_dbt_error(output: str) -> str:
    """Pull the actual error out of a dbt log.

    dbt writes a few hundred lines of progress around the one that matters, wrapped in
    ANSI colour. Surfacing the raw tail buries the cause in noise, and this text goes
    into a report a human is meant to read.
    """
    import re

    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    lines = [line.strip() for line in clean.splitlines()]
    collected: list[str] = []
    capturing = False
    for line in lines:
        if "Error in model" in line or "Runtime Error" in line or "Compilation Error" in line:
            capturing = True
        if capturing and line:
            # Drop dbt's leading timestamps so the message reads as a message.
            collected.append(re.sub(r"^\d{2}:\d{2}:\d{2}\s+", "", line))
        if capturing and len(collected) >= 6:
            break
    return " ".join(collected).strip()


def execute(
    project_dir: Path,
    *,
    base: str,
    head: str,
    models: tuple[str, ...],
    settings: Settings,
    target: str = "dev",
    grain_candidates: dict[str, Grain] | None = None,
    incremental_models: tuple[str, ...] = (),
) -> ExecutionResult:
    """Build base and head side by side, then diff the results.

    The base revision is built inside a temporary git worktree so the working tree is
    never touched, and both builds are pointed at the same database via a generated
    profile so the only difference between them is the code.
    """
    if not models:
        return ExecutionResult(skipped_reason="no changed models to build")

    try:
        assert_target_allowed(target, settings.execute_allowed_targets)
    except Exception as exc:
        return ExecutionResult(skipped_reason=str(exc))

    try:
        profile = read_profile(project_dir, target=target)
    except ProfileError as exc:
        return ExecutionResult(skipped_reason=f"could not read dbt profile: {exc}")

    repo = git.repo_root(project_dir)
    base_sha = git.resolve_revision(repo, base)
    relative = project_dir.resolve().relative_to(repo.resolve())

    with tempfile.TemporaryDirectory(prefix="themis-exec-") as tmp:
        profiles_root = Path(tmp)

        head_error = _build(
            project_dir,
            models=models,
            schema=settings.execute_head_schema,
            target=target,
            settings=settings,
            profiles_root=profiles_root,
            anchor_dir=project_dir,
            label="head",
            incremental_models=incremental_models,
        )

        base_error: str | None
        with git.worktree_at(repo, base_sha) as tree:
            base_error = _build(
                tree / relative,
                models=models,
                schema=settings.execute_base_schema,
                target=target,
                settings=settings,
                profiles_root=profiles_root,
                # Anchor to the real project, never the worktree.
                anchor_dir=project_dir,
                label="base",
                incremental_models=incremental_models,
            )

    client = client_for_profile(profile, project_dir)
    if client is None:
        return ExecutionResult(
            skipped_reason=(
                "built both revisions but cannot measure them: no supported warehouse "
                "client for this adapter"
            )
        )

    try:
        return _measure(
            client,
            models=models,
            settings=settings,
            head_error=head_error,
            base_error=base_error,
            grain_candidates=grain_candidates or {},
        )
    finally:
        client.close()


def _measure(
    client: WarehouseClient,
    *,
    models: tuple[str, ...],
    settings: Settings,
    head_error: str | None,
    base_error: str | None,
    grain_candidates: dict[str, Grain],
) -> ExecutionResult:
    deltas: dict[str, ExecutionDelta] = {}
    grains: dict[str, Grain] = {}
    baselines: dict[str, Grain] = {}

    for model in models:
        delta = diff_tables(
            client,
            model,
            base_schema=settings.execute_base_schema,
            head_schema=settings.execute_head_schema,
            max_rows=settings.execute_max_rows,
        )
        # A build failure on the head revision is the most severe possible outcome and
        # must not be masked by an otherwise-empty delta.
        if head_error and delta.rows_after is None:
            delta = delta.model_copy(update={"build_error": head_error})
        elif base_error and delta.rows_before is None:
            delta = delta.model_copy(
                update={"build_error": f"base revision failed to build: {base_error}"}
            )
        deltas[model] = delta

        candidate = grain_candidates.get(model)
        measured = measure_grain(
            client, model, schema=settings.execute_head_schema, candidate=candidate
        )
        if measured is not None:
            grains[model] = measured
        baseline = measure_grain(
            client, model, schema=settings.execute_base_schema, candidate=candidate
        )
        if baseline is not None:
            baselines[model] = baseline

    log.info(
        "execute.measured",
        models=len(deltas),
        material=sum(1 for d in deltas.values() if d.is_material),
        grains=len(grains),
    )
    return ExecutionResult(
        deltas=deltas, measured_grains=grains, baseline_grains=baselines, built=models
    )
