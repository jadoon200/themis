"""Stage 3 — build both revisions and measure what actually changed.

Everything before this stage reasons *about* the SQL. This stage runs it, and what it
produces is categorically stronger: not "this join may fan out" but "row count 1.2M to
1.68M, sum(amount_usd) 44.1M to 61.7M". A reviewer does not have to adjudicate a
measurement — which is exactly why a wrong one is worse than none.

It is also the only stage that executes anything, so the production guard in
``acquire.dbt_runner`` gates every invocation and fails closed.
"""

from __future__ import annotations

import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from themis.acquire import git
from themis.acquire.dbt_runner import (
    assert_target_allowed,
    extract_dbt_error,
    node_statuses,
    run_dbt,
    seed_partial_parse,
)
from themis.capabilities import Capability, CapabilityError, require
from themis.config import Settings
from themis.execute.differ import diff_tables, measure_grain
from themis.execute.profiles import ProfileError, read_profile, write_profile_for_schema
from themis.execute.warehouse import WarehouseClient, client_for_profile, drop_run_schemas
from themis.logging import get_logger
from themis.models import ExecutionDelta, Grain

log = get_logger(__name__)

# Statuses that mean a node errored itself, as opposed to being skipped because of
# something upstream.
_ERRORED = frozenset({"error", "fail", "runtime error"})


@dataclass(frozen=True)
class BuildOutcome:
    """What one revision's build produced, model by model.

    The warehouse cannot answer this. A relation that exists may have been written by
    this build, by the first of two passes before the second failed, or by an earlier
    run entirely — and all three look identical from a ``select count(*)``. dbt's own
    per-node status is the only record of which models this build actually produced.
    """

    error: str | None = None
    # Node name to dbt status, from run_results.json. Empty when dbt wrote none — a
    # crash before any node ran — in which case the exit code is all there is to go on.
    statuses: dict[str, str] = field(default_factory=dict)

    def failure(self, model: str) -> str | None:
        """Why a model was not built, or None when it was."""
        status = self.statuses.get(model)
        if status == "success":
            return None
        if status is None:
            return self.error
        if status == "skipped":
            return "not built: skipped because something it depends on failed" + (
                f" ({self.error})" if self.error else ""
            )
        return self.error or f"dbt reported the model as {status}"

    def skipped(self, model: str) -> bool:
        return self.statuses.get(model) == "skipped"

    @property
    def errored(self) -> tuple[str, ...]:
        """Nodes that failed themselves, rather than being skipped."""
        return tuple(sorted(name for name, status in self.statuses.items() if status in _ERRORED))


@dataclass
class ExecutionResult:
    """What Stage 3 measured, and what it could not."""

    deltas: dict[str, ExecutionDelta] = field(default_factory=dict)
    measured_grains: dict[str, Grain] = field(default_factory=dict)
    # The same measurement against the base revision, so a finding can say whether a
    # change made the grain worse rather than merely that it is bad.
    baseline_grains: dict[str, Grain] = field(default_factory=dict)
    built: tuple[str, ...] = ()
    head_build: BuildOutcome = field(default_factory=BuildOutcome)
    base_build: BuildOutcome = field(default_factory=BuildOutcome)
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
    defer_state: Path | None = None,
) -> BuildOutcome:
    """Build a selection into a schema, and record which models it actually produced.

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
    # dbt's artefacts go somewhere this build owns. Reading run_results.json out of the
    # project's own target/ could read the previous run's, and a build that died before
    # writing one would then report the last build's successes as its own.
    target_dir = profiles_root / label / "target"
    seed_partial_parse(anchor_dir, target_dir)

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
    # `dbt build` rather than `run`: seeds must be loaded into this schema too, or the
    # models have nothing to read and the comparison runs against an empty database.
    #
    # Deferral replaces all of that. With a state manifest, everything not selected
    # resolves to the relation that manifest recorded, so only the models being
    # measured get built and the ancestor closure — seeds included — is read where it
    # already is. On a project where the closure is hundreds of models that is the
    # difference between a review that fits in CI and one that does not.
    #
    # `--favor-state` is deliberate. Without it dbt prefers a relation that happens to
    # exist in the target schema, so a leftover table from an earlier run would be
    # silently preferred over the state one, and two runs of the same code could
    # measure differently.
    defer_args: list[str] = []
    if defer_state is not None:
        defer_args = ["--defer", "--favor-state", "--state", str(defer_state)]
        selection = [arg for model in models for arg in ("--select", model)]
    else:
        selection = [arg for model in models for arg in ("--select", f"+{model}")]

    def run(args: list[str]) -> tuple[bool, str, dict[str, str]]:
        (target_dir / "run_results.json").unlink(missing_ok=True)
        result = run_dbt(
            project_dir,
            args,
            target=target,
            allowed_targets=settings.execute_allowed_targets,
            profiles_dir=profiles_dir,
            timeout_s=settings.execute_timeout_s,
            target_path=target_dir,
        )
        return result.ok, result.stdout, node_statuses(target_dir)

    ok, stdout, statuses = run(["build", "--full-refresh", *selection, *defer_args])
    if ok and incremental_models:
        # The second pass only needs to re-run the incremental models themselves.
        # Pass one already built their upstreams, and rebuilding the whole closure
        # again doubles the cost of every run for no additional signal.
        second = [arg for model in incremental_models for arg in ("--select", model)]
        ok, stdout, second_statuses = run(["build", *second, *defer_args])
        if not ok and not second_statuses:
            # The second pass failed before recording anything, so the first pass's
            # "success" for these models describes a table the second pass never
            # finished writing. They were not built.
            second_statuses = {model: "error" for model in incremental_models}
        statuses = {**statuses, **second_statuses}

    if ok:
        return BuildOutcome(statuses=statuses)
    # A partial build is still worth measuring — the models that did build give real
    # evidence, and the failure itself is a finding.
    message = extract_dbt_error(stdout) or "dbt build failed"
    log.warning("execute.build_failed", label=label, error=message[:300])
    return BuildOutcome(error=message, statuses=statuses)


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
    defer_state: Path | None = None,
    capabilities: frozenset[Capability] | None = None,
    data_anchor: Path | None = None,
) -> ExecutionResult:
    """Build base and head side by side, then diff the results.

    Each revision is built from the code it names. The base always comes from a
    temporary worktree; the head comes from the working tree only when that *is* the
    head (see ``git.is_working_tree``), so a review of another commit measures that
    commit rather than whatever happens to be checked out.

    Both builds land in schemas unique to this run and are dropped afterwards. Shared
    schema names let a failed build measure the previous run's table as its own result,
    and let two workers overwrite each other mid-measurement.

    ``defer_state`` points at a directory holding a manifest from an existing build —
    production, or a nightly. Unselected models resolve to the relations that manifest
    names instead of being rebuilt, which is how this stage stays affordable on a
    project whose ancestor closure is hundreds of models. Both revisions defer to the
    *same* state, so the upstream data is identical on either side and the only
    difference left between the two builds is the code being reviewed.

    ``data_anchor`` is where the data lives when the code is a copy — the eval harness
    reviews a worktree whose database exists only in the original project.
    """
    if not models:
        return ExecutionResult(skipped_reason="no changed models to build")

    # Enforced here as well as at claim time. A guard that lives only in the scheduler
    # is one that a scheduling bug removes, and this is the stage that runs code
    # against a warehouse.
    if capabilities is not None:
        try:
            require(capabilities, Capability.EXECUTE, what="Stage 3 execution")
        except CapabilityError as exc:
            return ExecutionResult(skipped_reason=str(exc))

    if defer_state is not None:
        if not (defer_state / "manifest.json").exists():
            return ExecutionResult(
                skipped_reason=(
                    f"--defer-state {defer_state} holds no manifest.json. Deferral needs "
                    "a manifest from an existing build; without one every ref() to an "
                    "unbuilt model would resolve to nothing."
                )
            )
        defer_state = defer_state.resolve()

    try:
        assert_target_allowed(target, settings.execute_allowed_targets)
    except Exception as exc:
        return ExecutionResult(skipped_reason=str(exc))

    try:
        profile = read_profile(project_dir, target=target)
    except ProfileError as exc:
        return ExecutionResult(skipped_reason=f"could not read dbt profile: {exc}")

    anchor = (data_anchor or project_dir).resolve()
    repo = git.repo_root(project_dir)
    base_sha = git.resolve_revision(repo, base)
    head_in_place = git.is_working_tree(repo, head, project_dir)
    head_sha = None if head_in_place else git.resolve_revision(repo, head)
    relative = project_dir.resolve().relative_to(repo.resolve())

    token = secrets.token_hex(4)
    base_schema = f"{settings.execute_base_schema}_{token}"
    head_schema = f"{settings.execute_head_schema}_{token}"

    def build(tree: Path, schema: str, label: str, root: Path) -> BuildOutcome:
        return _build(
            tree,
            models=models,
            schema=schema,
            target=target,
            settings=settings,
            profiles_root=root,
            # Anchor to the real project, never the worktree.
            anchor_dir=anchor,
            label=label,
            incremental_models=incremental_models,
            defer_state=defer_state,
        )

    try:
        with tempfile.TemporaryDirectory(prefix="themis-exec-") as tmp:
            root = Path(tmp)
            if head_sha is None:
                head_build = build(project_dir, head_schema, "head", root)
            else:
                with git.worktree_at(repo, head_sha) as tree:
                    head_build = build(tree / relative, head_schema, "head", root)
            with git.worktree_at(repo, base_sha) as tree:
                base_build = build(tree / relative, base_schema, "base", root)

        client = client_for_profile(profile, anchor)
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
                base_schema=base_schema,
                head_schema=head_schema,
                max_rows=settings.execute_max_rows,
                head_build=head_build,
                base_build=base_build,
                grain_candidates=grain_candidates or {},
            )
        finally:
            client.close()
    finally:
        if settings.execute_keep_schemas:
            log.info("execute.schemas_kept", base=base_schema, head=head_schema)
        else:
            drop_run_schemas(profile, anchor, (base_schema, head_schema))


def _unbuilt_delta(
    client: WarehouseClient,
    model: str,
    *,
    base_schema: str,
    head_schema: str,
    head_failure: str | None,
    base_failure: str | None,
    head_build: BuildOutcome,
    base_build: BuildOutcome,
) -> ExecutionDelta:
    """A delta for a model at least one revision did not build.

    Nothing is measured on a side that did not build, whatever the warehouse holds
    there. A relation can survive a failed build — the first of two passes wrote it —
    and measuring it would present that table as this revision's result.
    """
    if head_failure and base_failure:
        failed, message = "both", f"neither revision built: {head_failure}"
        skipped = head_build.skipped(model)
    elif head_failure:
        failed, message, skipped = "head", head_failure, head_build.skipped(model)
    else:
        failed = "base"
        message = f"base revision did not build: {base_failure}"
        skipped = base_build.skipped(model)

    before = client.shape(base_schema, model) if base_failure is None else None
    after = client.shape(head_schema, model) if head_failure is None else None
    return ExecutionDelta(
        model_name=model,
        rows_before=before.row_count if before is not None and before.exists else None,
        rows_after=after.row_count if after is not None and after.exists else None,
        build_error=message,
        failed_revision=failed,
        build_skipped=skipped,
    )


def _measure(
    client: WarehouseClient,
    *,
    models: tuple[str, ...],
    base_schema: str,
    head_schema: str,
    max_rows: int,
    head_build: BuildOutcome,
    base_build: BuildOutcome,
    grain_candidates: dict[str, Grain],
) -> ExecutionResult:
    deltas: dict[str, ExecutionDelta] = {}
    grains: dict[str, Grain] = {}
    baselines: dict[str, Grain] = {}

    for model in models:
        head_failure = head_build.failure(model)
        base_failure = base_build.failure(model)
        if head_failure or base_failure:
            deltas[model] = _unbuilt_delta(
                client,
                model,
                base_schema=base_schema,
                head_schema=head_schema,
                head_failure=head_failure,
                base_failure=base_failure,
                head_build=head_build,
                base_build=base_build,
            )
            continue

        deltas[model] = diff_tables(
            client, model, base_schema=base_schema, head_schema=head_schema, max_rows=max_rows
        )
        candidate = grain_candidates.get(model)
        measured = measure_grain(client, model, schema=head_schema, candidate=candidate)
        if measured is not None:
            grains[model] = measured
        baseline = measure_grain(client, model, schema=base_schema, candidate=candidate)
        if baseline is not None:
            baselines[model] = baseline

    log.info(
        "execute.measured",
        models=len(deltas),
        material=sum(1 for d in deltas.values() if d.is_material),
        unbuilt=sum(1 for d in deltas.values() if d.failed_revision),
        grains=len(grains),
    )
    return ExecutionResult(
        deltas=deltas,
        measured_grains=grains,
        baseline_grains=baselines,
        built=models,
        head_build=head_build,
        base_build=base_build,
    )
