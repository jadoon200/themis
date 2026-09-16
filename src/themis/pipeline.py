"""Wire the stages together.

Kept deliberately linear and explicit. This is a merge gate, so being able to read the
whole flow in one screen matters more than any abstraction it might be factored into.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from themis import vocabulary
from themis.acquire.snapshot_builder import AcquireResult, acquire
from themis.analyze.grain import infer_grains
from themis.analyze.lineage import LineageIndex
from themis.analyze.positioning import position_findings
from themis.analyze.suggest import suggest_tests
from themis.capabilities import Capability, require
from themis.config import Settings
from themis.execute.runner import ExecutionResult, execute
from themis.logging import get_logger
from themis.models import (
    Confidence,
    ExecutionDelta,
    Finding,
    FindingHistory,
    Grain,
    GrainSource,
    KeyedDiff,
    sum_moved,
)
from themis.review.supervisor import ReviewSummary
from themis.rules.base import RuleContext, SkippedRule
from themis.rules.registry import run_rules
from themis.snapshot import ProjectSnapshot
from themis.triage.rubric import calibrate
from themis.vocabulary import DEFAULT as DEFAULT_VOCABULARY
from themis.vocabulary import Vocabulary

log = get_logger(__name__)


@dataclass
class ReviewResult:
    """Everything one review produced, including what it could not check."""

    findings: list[Finding] = field(default_factory=list)
    skipped: list[SkippedRule] = field(default_factory=list)
    grains: dict[str, Grain] = field(default_factory=dict)
    models_reviewed: tuple[str, ...] = ()
    macro_affected: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Seeds whose data changed, and every model built on them. A data change reviews no
    # SQL, so without this a PR editing only an FX-rate file reported zero changed models.
    seed_affected: dict[str, tuple[str, ...]] = field(default_factory=dict)
    degraded_reason: str | None = None
    executed: bool = False
    execution: ExecutionResult | None = None
    llm: ReviewSummary | None = None
    # Models tagged for reconciliation or reporting. Triage weights a finding that
    # lands in one, and the tag is the project's own statement about what it feeds.
    governed_models: frozenset[str] = frozenset()
    # Reviewed models whose grain THEMIS derived and nothing in the project asserts.
    # Reported as one line rather than a finding each: on a project with no test
    # coverage a per-model finding would fire on everything and bury the real ones.
    untested_grains: tuple[str, ...] = ()
    # Whether the caller asked for Stage 3, so a review that wanted measurement and did
    # not get it can say so rather than passing as an inference-only review by choice.
    execution_requested: bool = False

    @property
    def incomplete(self) -> tuple[tuple[str, str], ...]:
        """Every way this review checked less than it was asked to, as (kind, reason).

        The merge gate used to read findings alone, so a review with most of its rules
        skipped, or one that asked for execution and never built anything, exited 0 when
        it found nothing — the same failure as the corpus job that measured 9/29 rules
        and passed. A report can carry a banner; a gate only reads an exit code.

        The kind is stable and carries no project detail, so it survives redaction.
        """
        items: list[tuple[str, str]] = []
        if self.degraded_reason:
            items.append(("grounding_degraded", f"grounding degraded: {self.degraded_reason}"))
        if self.execution_requested and not self.executed:
            why = self.execution.skipped_reason if self.execution else "it did not run"
            items.append(("execution_not_run", f"execution was requested and did not run: {why}"))
        if self.skipped:
            rules = sorted({s.rule_id for s in self.skipped})
            items.append(
                (
                    "checks_skipped",
                    f"{len(self.skipped)} check(s) could not run ({', '.join(rules[:8])}"
                    + (" and more" if len(rules) > 8 else "")
                    + ")",
                )
            )
        return tuple(items)

    @property
    def incomplete_reasons(self) -> tuple[str, ...]:
        return tuple(reason for _, reason in self.incomplete)


def build_contexts(
    result: AcquireResult,
    grains: dict[str, Grain],
    *,
    dialect: str,
    vocab: Vocabulary = DEFAULT_VOCABULARY,
) -> list[RuleContext]:
    """One context per model the change actually affects.

    Two sources feed this: models whose own file changed, and models reached through a
    changed macro. The second is why a one-line macro edit is reviewed as the N-model
    change it is rather than the one-file change it looks like.
    """
    directly_changed = set(result.changed_models)

    # Models reached only through a changed schema YAML. Where configuration lives in
    # YAML — materialization, partitioning, hooks — a YAML-only change alters real
    # behaviour while touching no .sql file at all.
    via_yaml: dict[str, str] = {}
    for yaml_file in result.changed_schema_files:
        for model in result.after.models_in_yaml(yaml_file):
            if model not in directly_changed:
                via_yaml.setdefault(model, Path(yaml_file).name)

    via_macro: dict[str, str] = {}
    for macro_file in result.changed_macro_files:
        names = result.after.macros_in_file(macro_file) or (Path(macro_file).stem,)
        label = ", ".join(names)
        for model in result.after.models_using_macro_file(macro_file):
            if model not in directly_changed:
                via_macro.setdefault(model, label)

    # dbt_project.yml carries folder-level configs and vars, so one line there can
    # re-materialize or re-filter a whole directory while no model file changes. The
    # models it reached are the ones whose compiled SQL or configuration now differ.
    if result.changed_project_config:
        for model in _reconfigured_models(result.before, result.after):
            if model not in directly_changed and model not in via_macro:
                via_yaml.setdefault(model, "dbt_project.yml")

    affected = directly_changed | set(via_macro) | set(via_yaml)

    # Column lineage is traced over the changed models and everything below them --
    # the only region where a column edge can change a verdict. The index builds on
    # first use, so a review whose rules never ask for lineage never pays for it.
    trace = set(affected)
    for name in affected:
        trace.update(result.after.downstream_of(name))
        trace.update(result.before.downstream_of(name))
    lineage = LineageIndex(
        before_snapshot=result.before,
        after_snapshot=result.after,
        trace=frozenset(trace),
        dialect=dialect,
    )

    contexts: list[RuleContext] = []
    for name in sorted(affected):
        before = result.before.models.get(name)
        after = result.after.models.get(name)
        if before is None and after is None:
            continue  # a changed file that is not a model in either revision
        contexts.append(
            RuleContext(
                model_name=name,
                before=before,
                after=after,
                before_snapshot=result.before,
                after_snapshot=result.after,
                grains=grains,
                dialect=dialect,
                lineage=lineage,
                via_macro=via_macro.get(name),
                via_yaml=via_yaml.get(name),
                vocabulary=vocab,
            )
        )
    return contexts


def _macro_texts(snapshot: ProjectSnapshot, names: set[str]) -> dict[str, str]:
    """The source of these macros and every macro they call, transitively."""
    seen: dict[str, str] = {}
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        macro = snapshot.macros.get(name)
        seen[name] = macro.raw_sql if macro else ""
        if macro:
            pending.extend(ref.split(".")[-1] for ref in macro.depends_on_macros)
    return seen


def code_changed(name: str, before: ProjectSnapshot, after: ProjectSnapshot) -> bool:
    """Whether a model's code differs between revisions.

    Compiled SQL answers that for most models. It cannot for a model whose SQL is built
    from query results: two compiles of identical code differ whenever the rows come back
    differently — including merely in a different order, which an unordered query does
    between one compile and the next. The demo project's own generated model did exactly
    that, so it read as changed in every review. For such a model the code is its raw SQL
    and the macros it calls.
    """
    was, now = before.models.get(name), after.models.get(name)
    if was is None or now is None:
        return (was is None) != (now is None)
    generated = set(before.data_dependent_models()) | set(after.data_dependent_models())
    if name not in generated:
        return was.analysable_sql != now.analysable_sql
    if was.raw_sql != now.raw_sql:
        return True
    called = {ref.split(".")[-1] for ref in (*was.depends_on_macros, *now.depends_on_macros)}
    return _macro_texts(before, called) != _macro_texts(after, called)


def _reconfigured_models(before: ProjectSnapshot, after: ProjectSnapshot) -> tuple[str, ...]:
    """SQL models whose code or write configuration differ between revisions."""

    def shape(snapshot: ProjectSnapshot, name: str) -> tuple[object, ...] | None:
        model = snapshot.models.get(name)
        if model is None or model.is_seed:
            return None
        return (
            model.relation_name,
            model.materialization,
            model.incremental_strategy,
            model.unique_key,
            model.on_schema_change,
            model.tags,
            model.contract_enforced,
            tuple(sorted(model.properties.items())),
            model.pre_hooks,
            model.post_hooks,
        )

    names = set(before.models) | set(after.models)
    return tuple(
        sorted(
            name
            for name in names
            if shape(after, name) is not None
            and (shape(before, name) != shape(after, name) or code_changed(name, before, after))
        )
    )


def attach_execution(findings: list[Finding], result: ExecutionResult) -> list[Finding]:
    """Attach measured evidence to the findings it settles.

    A finding whose model actually moved is no longer a hypothesis, so its confidence
    is raised to MEASURED and it bypasses the model review entirely — there is nothing
    left to adjudicate once the row count and the total have both changed.
    """
    attached: list[Finding] = []
    for finding in findings:
        delta = result.deltas.get(finding.evidence.model_name)
        # A build failure is not a measurement of what the rule describes. It is its own
        # finding (X0002); promoting a grain-change prediction to MEASURED because the
        # model did not compile would claim evidence nobody has.
        if delta is None or not delta.is_material or delta.build_error is not None:
            attached.append(finding)
            continue
        attached.append(
            finding.model_copy(
                update={
                    "execution_delta": delta,
                    "confidence": Confidence.MEASURED,
                }
            )
        )
    return attached


def unexplained_change_findings(
    result: ExecutionResult,
    findings: list[Finding],
    before: ProjectSnapshot,
    after: ProjectSnapshot,
    changed_seeds: tuple[str, ...] = (),
    vocab: Vocabulary = DEFAULT_VOCABULARY,
) -> list[Finding]:
    """Report models whose results moved with no rule explaining why.

    This is the safety net under the entire rule catalogue. Rules only catch defect
    classes somebody anticipated, so a change outside all of them produces a clean
    report while the money moves. That happened: inverting an FX conversion shifted
    revenue by 1.8 million across six models and the review said "no findings", because
    multiplying instead of dividing is arithmetically ordinary and structurally
    invisible.

    Changes are attributed to their **root** — the model whose own SQL changed — rather
    than reported once per affected model. A single edit propagates through the DAG, so
    per-model reporting turns one defect into six findings, five of which can only say
    that nothing changed in their own SQL. That is noise for the reviewer and, when the
    model layer runs, five wasted calls producing five "unclear" answers.
    """

    explained = {f.evidence.model_name for f in findings}

    moved = {
        name for name, delta in result.deltas.items() if delta.is_material and not delta.build_error
    }
    if not moved:
        return []

    # An origin is any model whose own SQL changed — whether or not its own output
    # moved. That distinction is the whole of this: a model can change in a way its
    # own totals do not show while every model beneath it moves. Truncating a date to
    # the year alters no row count and no monetary sum in the staging model itself,
    # and shifts every figure below it. Requiring an origin to have moved left those
    # six descendants ownerless, so one explained change produced six criticals.
    origins: set[str] = {
        name
        for name in set(before.models) | set(after.models)
        if not (after.models.get(name) or before.models[name]).is_seed
        and code_changed(name, before, after)
    }
    # A seed whose data changed is an origin too, with no SQL to show for it. Without
    # this every model beneath an edited FX-rate file read as having moved for no
    # reason anyone could name.
    origins.update(changed_seeds)

    attributed: dict[str, list[str]] = {origin: [] for origin in origins}
    roots = moved & origins
    unattributed: set[str] = set()
    for name in sorted(moved - roots):
        owner = next(
            (origin for origin in sorted(origins) if name in after.downstream_of(origin)), None
        )
        if owner is not None:
            attributed.setdefault(owner, []).append(name)
        else:
            # Moved, own SQL unchanged, and downstream of nothing that changed. That is
            # genuinely unexplained and must not be folded into someone else's finding.
            unattributed.add(name)

    # Only report what no rule has already accounted for. A root a rule explains needs
    # no second finding, and neither does anything downstream of it. A model attributed
    # to an origin is never reported: the origin is where a reviewer should look, and
    # the descendant can only say that its own SQL is unchanged.
    #
    # An origin is reportable even when its own results did not move, provided
    # something beneath it did. Requiring it to have moved silences the case entirely:
    # a staging edit that shifts every figure downstream while its own totals sit
    # still would produce no finding anywhere, which is the exact silence this net
    # exists to break.
    reportable = {
        name
        for name in (origins | unattributed)
        if name not in explained
        and not _explained_upstream(name, origins, explained, after)
        and (name in moved or attributed.get(name))
    }

    out: list[Finding] = []
    for name in sorted(reportable):
        # An origin that did not move itself has no delta of its own; the movement
        # being reported is its descendants'.
        delta = result.deltas.get(name) or ExecutionDelta(model_name=name)
        consequences = tuple(sorted(attributed.get(name, [])))
        out.append(
            _unexplained_finding(
                name,
                delta,
                after=after,
                consequences=consequences,
                is_root=name in roots,
                own_code_changed=name in origins,
                consequence_deltas={
                    c: result.deltas[c]
                    for c in consequences
                    if c in result.deltas and result.deltas[c].is_material
                },
                vocab=vocab,
            )
        )
    return out


def _explained_upstream(
    name: str, origins: set[str], explained: set[str], after: ProjectSnapshot
) -> bool:
    """Whether some origin above this model already carries a finding.

    A rule that fired on a staging model accounts for every figure that moved beneath
    it. Repeating the movement as an unexplained critical for each descendant tells a
    reviewer six times about one thing, and buries the finding that actually names it.
    """
    return any(
        origin in explained and name in after.downstream_of(origin)
        for origin in origins
        if origin != name
    )


def _unexplained_finding(
    name: str,
    delta: ExecutionDelta,
    *,
    after: ProjectSnapshot,
    consequences: tuple[str, ...],
    is_root: bool,
    own_code_changed: bool = False,
    consequence_deltas: dict[str, ExecutionDelta] | None = None,
    vocab: Vocabulary = DEFAULT_VOCABULARY,
) -> Finding:
    from themis.models import Evidence, Severity

    consequence_deltas = consequence_deltas or {}

    moved = [
        (column, was, now) for column, (was, now) in sorted(delta.sum_deltas.items()) if was != now
    ]
    rows_moved = delta.row_delta not in (0, None)

    # Severity follows where the change *lands*, not where it originates. Attributing
    # to the root model was right for reporting, but an untagged staging model whose
    # change reaches a regulatory mart is not a lesser problem than one that starts
    # there — the reported figure moved either way.
    #
    # "Lands" means measured to move there, not merely reachable. Reachability used to
    # stand in for it, and a reclassification that moved one untagged table was reported
    # as critical, naming three regulatory marts as "a reported figure moved" when none of
    # them reads the column that changed. Critical is reserved for a reported figure that
    # was demonstrated to move; a governed mart that was not measured has not been.
    measured_here = {name: delta, **consequence_deltas}
    governed_models = tuple(
        reached_name
        for reached_name in (name, *after.downstream_of(name))
        if (reached_model := after.models.get(reached_name))
        and vocab.is_governed(reached_model.tags)
        and reached_name in measured_here
        and _values_moved(measured_here[reached_name])
    )
    severity = Severity.CRITICAL if governed_models else Severity.HIGH

    detail: list[str] = []
    if rows_moved and delta.rows_before is not None and delta.rows_after is not None:
        detail.append(f"rows {delta.rows_before:,} -> {delta.rows_after:,}")
    for column, was, now in moved:
        shift = ((now - was) / was * 100) if was else 0.0
        detail.append(f"sum({column}) {was:,.2f} -> {now:,.2f} ({shift:+.1f}%)")
    keyed = delta.keyed
    if keyed is not None and keyed.moved:
        detail.append(_keyed_detail(keyed))

    # An origin whose own results measure the same still owns the movement beneath it —
    # a view with no countable key, say, over a table where the rows were paired. Without
    # this the finding showed an unchanged row count and nothing else, and a reviewer could
    # not see what had moved at all.
    for child, child_delta in sorted(consequence_deltas.items())[:3]:
        measured = _delta_summary(child_delta)
        if measured:
            detail.append(f"{child}: {measured}")

    if consequences:
        detail.append(f"same change reaches {len(consequences)} downstream model(s)")

    seed = after.models.get(name)
    if seed is not None and seed.is_seed:
        origin = (
            "This seed's data changed. No rule can judge a data change — there is no SQL "
            "in it — so what it does to the figures built on it has been measured, not "
            "assessed."
        )
    elif is_root:
        origin = (
            "Building both revisions produced different results for this model, and none "
            "of the checks accounts for the difference."
        )
    elif own_code_changed:
        # Its SQL changed and its own results measure the same, but what is built on it
        # moved. Reported here because this is the code a reviewer has to read; calling it
        # "SQL unchanged" sent them looking for a cause somewhere else.
        origin = (
            "This model's SQL changed. Its own results measure the same, but the models "
            "built on it changed when both revisions were built, and none of the checks "
            "accounts for the difference."
        )
    else:
        origin = (
            "This model's results changed although its own SQL did not, and nothing "
            "upstream that changed accounts for it."
        )
    reach = f" The same movement carries into {', '.join(consequences)}." if consequences else ""

    return Finding(
        rule_id="X0001",
        family="X",
        title=f"`{name}` changed and no rule explains why",
        severity=severity,
        confidence=Confidence.MEASURED,
        # The note is what moved and by how much; the issue is that this model moved.
        evidence=Evidence(model_name=name, note="; ".join(detail), identity=""),
        consequence=(
            origin + " That means the change is outside every defect class this tool knows "
            "about — so it has not been assessed, only observed."
            + (
                f" A reported figure moved: {', '.join(governed_models)} "
                f"{'is' if len(governed_models) == 1 else 'are'} tagged for "
                "reconciliation or regulatory reporting."
                if governed_models
                else ""
            )
            + reach
            + " It needs a human explanation before merging."
        ),
        suggestion=(
            "Confirm the movement is intended and expected at this magnitude. If it is "
            "a defect class worth catching automatically, it is a candidate for a new "
            "rule."
        ),
        blast_radius=after.downstream_of(name),
        execution_delta=delta,
    )


def _values_moved(delta: ExecutionDelta) -> bool:
    """Whether a figure in this model changed: a total, or values paired on its key.

    Values moving between keys with every total intact still counts — revenue
    reclassified is not less wrong for summing to the same number.
    """
    if any(sum_moved(before, after) for before, after in delta.sum_deltas.values()):
        return True
    return delta.keyed is not None and delta.keyed.moved


def _delta_summary(delta: ExecutionDelta) -> str:
    """What moved in one model, in one clause: rows, then totals, then paired values."""
    parts: list[str] = []
    if delta.row_delta not in (0, None):
        parts.append(f"rows {delta.rows_before:,} -> {delta.rows_after:,}")
    for column, (was, now) in sorted(delta.sum_deltas.items()):
        if sum_moved(was, now):
            parts.append(f"sum({column}) {was:,.2f} -> {now:,.2f}")
    if delta.keyed is not None and delta.keyed.moved:
        parts.append(_keyed_detail(delta.keyed))
    return "; ".join(parts)


def _keyed_detail(keyed: KeyedDiff) -> str:
    """One line on what pairing rows found, for a finding's evidence note."""
    parts: list[str] = []
    if keyed.rows_changed:
        top = sorted(keyed.columns_changed.items(), key=lambda item: (-item[1], item[0]))[:4]
        columns = ", ".join(f"{name} ({count:,})" for name, count in top)
        parts.append(f"{keyed.rows_changed:,} row(s) changed value in {columns}")
    if keyed.rows_added:
        parts.append(f"{keyed.rows_added:,} key(s) only in head")
    if keyed.rows_removed:
        parts.append(f"{keyed.rows_removed:,} key(s) only in base")
    return f"paired on ({', '.join(keyed.key)}): " + "; ".join(parts)


def build_failure_findings(result: ExecutionResult, after: ProjectSnapshot) -> list[Finding]:
    """Report a head revision that no longer builds.

    Before this existed a build failure was only ever evidence attached to some other
    rule's finding, and the net for unexplained movement deliberately ignores it. So a
    change whose head failed to build, and which no rule happened to describe, came back
    as "No findings" — the review had watched the build fail and said nothing.

    One finding per model that failed itself, not per model skipped behind it: the
    model that broke is where to look, and the ones that never ran are its consequence.
    A model that failed on the base too is not this change's doing and is left out.
    """
    from themis.models import Evidence, Severity

    head, base = result.head_build, result.base_build
    unbuilt_downstream = sorted(
        name
        for name, delta in result.deltas.items()
        if delta.failed_revision == "head" and delta.build_skipped
    )

    if head.statuses:
        roots = [name for name in head.errored if name not in base.errored]
    elif head.error is not None:
        # dbt recorded no per-node statuses, so the model that broke cannot be told apart
        # from the ones skipped behind it. Name every measured model the head lost.
        roots = sorted(
            name for name, delta in result.deltas.items() if delta.failed_revision == "head"
        )
        unbuilt_downstream = []
    else:
        roots = []

    findings: list[Finding] = []
    for name in roots:
        model = after.models.get(name)
        downstream = [m for m in unbuilt_downstream if m != name]
        findings.append(
            Finding(
                rule_id="X0002",
                family="X",
                title=f"`{name}` does not build at the head revision",
                severity=Severity.HIGH,
                confidence=Confidence.MEASURED,
                evidence=Evidence(
                    model_name=name,
                    file_path=model.file_path if model else None,
                    # The dbt error text can carry run-specific detail; the issue is the model.
                    identity="",
                    note=(head.error or "dbt reported the model as failed")[:600],
                ),
                consequence=(
                    "Building the proposed change failed on this model, while the base "
                    "revision built it. Nothing downstream of it can be measured, and "
                    "merged as-is the next production run fails here."
                    + (f" Not built as a result: {', '.join(downstream)}." if downstream else "")
                ),
                suggestion=(
                    "Fix the error above and re-run. If the change was meant to remove "
                    "something a downstream model still reads, update that model in the "
                    "same change."
                ),
                blast_radius=after.downstream_of(name),
            )
        )
    return findings


def measured_grain_findings(result: ExecutionResult, inferred: dict[str, Grain]) -> list[Finding]:
    """Report where measurement and inference disagree.

    That disagreement is itself worth surfacing: it is the only direct evidence of
    whether the derivation lattice can be trusted on a given project.
    """
    from themis.models import Evidence, Severity

    findings: list[Finding] = []
    for name, measured in result.measured_grains.items():
        if measured.rows_per_key is None or measured.rows_per_key <= 1.0:
            continue
        # Unchanged duplication is a pre-existing condition, not this change's doing.
        baseline = result.baseline_grains.get(name)
        if (
            baseline is not None
            and baseline.rows_per_key is not None
            and measured.rows_per_key <= baseline.rows_per_key + 1e-9
        ):
            continue
        derived = inferred.get(name)
        source = derived.source.value if derived else GrainSource.UNKNOWN.value
        findings.append(
            Finding(
                rule_id="F1004",
                family="F1",
                title=f"`{name}` is not unique on its derived key",
                severity=Severity.HIGH,
                confidence=Confidence.MEASURED,
                evidence=Evidence(
                    model_name=name,
                    note=measured.note,
                    # The multiplier is data; the issue is duplication on this key.
                    identity=",".join(measured.columns),
                ),
                consequence=(
                    f"The key ({', '.join(measured.columns)}) was derived as this "
                    f"model's grain [{source}], but the built table has "
                    f"{measured.rows_per_key:.2f} rows per key. Any join onto that key "
                    "multiplies rows, and any amount summed after it is overstated by "
                    "roughly that factor."
                ),
                suggestion=(
                    "Either the join keys are incomplete or the model genuinely has a "
                    "finer grain than assumed. Adding a uniqueness test on the real key "
                    "would make this checkable without a build."
                ),
            )
        )
    return findings


# What earlier runs did with these same findings. A callable rather than a session so
# the pipeline keeps no database dependency: the CLI and the worker each bind one to
# their own store, and a run with no store simply has no history.
HistoryLookup = Callable[[list[Finding]], Sequence[FindingHistory | None]]


def attach_history(findings: list[Finding], lookup: HistoryLookup | None) -> list[Finding]:
    """Hang each finding's own history on it, if anything can supply one.

    Deliberately before the model layer and the ranking, because both read it: the
    specialist is shown what people decided about findings like this one, and the
    rubric ranks down what they keep dismissing.
    """
    if lookup is None or not findings:
        return findings
    try:
        histories = lookup(findings)
    except Exception as exc:  # pragma: no cover - a store problem must not fail a review
        log.warning("review.history_unavailable", error=str(exc)[:200])
        return findings

    out: list[Finding] = []
    seen = 0
    for finding, history in zip(findings, histories, strict=False):
        if history is None:
            out.append(finding)
            continue
        seen += 1
        out.append(finding.model_copy(update={"history": history}))
    if seen:
        log.info("review.history_attached", findings=seen)
    return out


def review(
    project_dir: Path,
    *,
    base: str,
    head: str,
    settings: Settings,
    target: str = "dev",
    prod_manifest: Path | None = None,
    # A directory holding a manifest from an existing build. Passing it makes Stage 3
    # defer unselected upstreams to those relations instead of rebuilding them.
    defer_state: Path | None = None,
    use_manifest_cache: bool | None = None,
    # What the caller is permitted to do. None means an unrestricted local run — the
    # CLI on a developer's machine, where the person already has every access the tool
    # would use. Workers pass their own, and a worker without EXECUTE cannot build.
    capabilities: frozenset[Capability] | None = None,
    run_execution: bool = False,
    run_llm: bool = False,
    pr_description: str | None = None,
    provider: object | None = None,
    data_anchor: Path | None = None,
    history: HistoryLookup | None = None,
) -> ReviewResult:
    """Run the deterministic stages, optionally including execution.

    The model review (stages 4-5) layers on top of this; the deterministic core stands
    alone and is useful without it.
    """
    if capabilities is not None:
        # Every stage a run needs is checked before any of it starts. A worker declared
        # without COMPILE used to compile anyway, with the warehouse credentials that
        # implies, because only EXECUTE was ever looked at.
        require(capabilities, Capability.COMPILE, what="compiling the revisions under review")
        require(capabilities, Capability.ANALYSE, what="running the rules")
        if run_llm:
            require(capabilities, Capability.REVIEW, what="the model review")

    acquired = acquire(
        project_dir,
        base=base,
        head=head,
        target=target,
        allowed_targets=settings.execute_allowed_targets,
        timeout_s=settings.execute_timeout_s,
        prod_manifest=prod_manifest,
        data_anchor=data_anchor,
        use_cache=(
            settings.manifest_cache_enabled if use_manifest_cache is None else use_manifest_cache
        ),
    )

    grains = infer_grains(acquired.after, dialect=settings.dialect)
    vocab = vocabulary.from_settings(settings)
    contexts = build_contexts(acquired, grains, dialect=settings.dialect, vocab=vocab)
    findings, skipped = run_rules(contexts)

    macro_affected = {
        macro: acquired.after.models_using_macro(macro) for macro in acquired.changed_macros
    }
    seed_affected = {seed: acquired.after.downstream_of(seed) for seed in acquired.changed_seeds}

    execution: ExecutionResult | None = None
    if run_execution:
        # Measure descendants as well as the changed models themselves. A fan-out in an
        # intermediate model is invisible in its own row count when the join is the last
        # step, but shows up unmistakably as an inflated SUM in the mart below it.
        # Changed seeds join the selection with everything built on them: a data change
        # has no SQL for a rule to read, so measuring is the only way it is reviewed.
        changed = {c.model_name for c in contexts} | set(seed_affected)
        targets = set(changed)
        for name in changed:
            targets.update(acquired.after.downstream_of(name))
        # Two-pass building is only needed when something in the selection is
        # incremental; for everything else the second pass is wasted time.
        # Only the incremental models need a second pass, and only when they are in
        # the selection at all.
        incremental_models = tuple(
            sorted(
                name
                for name in targets
                if (model := acquired.after.models.get(name) or acquired.before.models.get(name))
                and model.materialization == "incremental"
            )
        )
        execution = execute(
            project_dir,
            base=base,
            head=head,
            models=tuple(sorted(targets)),
            settings=settings,
            target=target,
            grain_candidates=grains,
            incremental_models=incremental_models,
            defer_state=defer_state,
            capabilities=capabilities,
            data_anchor=data_anchor,
        )
        if execution.ran:
            findings = attach_execution(findings, execution)
            findings.extend(build_failure_findings(execution, acquired.after))
            findings.extend(measured_grain_findings(execution, grains))
            # Runs after the others so "explained" reflects everything already found.
            findings.extend(
                unexplained_change_findings(
                    execution,
                    findings,
                    acquired.before,
                    acquired.after,
                    changed_seeds=acquired.changed_seeds,
                    vocab=vocab,
                )
            )
            grains = {**grains, **execution.measured_grains}
        else:
            log.warning("execute.skipped", reason=execution.skipped_reason)

    # Every context shares one lazily-built index, so reading it off the first is the
    # same object the rules used — and asking for `.before` is what builds it, only if
    # a specialist that needs column lineage is actually reached.
    column_lineage = contexts[0].lineage.before if contexts and contexts[0].lineage else None

    # Before the model layer and before triage, because both read it.
    findings = attach_history(findings, history)

    llm_summary: ReviewSummary | None = None
    if run_llm and findings:
        # Runs last on purpose. Execution settles what it can first, so the model is
        # only asked about findings that are still genuinely open.
        from themis.llm.provider import LLMError, Provider, build_provider
        from themis.review import supervisor

        try:
            # Injectable so a recording or replaying provider can stand in. Without it
            # the model path can only be tested against a fake, which proves the wiring
            # and never that the real prompts produce parseable, grounded output.
            active: Provider = provider if provider is not None else build_provider(settings)  # type: ignore[assignment]
            llm_summary = supervisor.review(
                findings,
                provider=active,
                settings=settings,
                snapshot=acquired.after,
                grains=grains,
                changed_models=tuple(c.model_name for c in contexts),
                pr_description=pr_description,
                before_snapshot=acquired.before,
                # The before graph: a column that was removed still exists there, which
                # is the only revision in which "what reads it" has an answer.
                lineage=column_lineage,
            )
            findings = llm_summary.findings
        except LLMError as exc:
            # A model that cannot be reached must not fail the review; the
            # deterministic findings stand on their own.
            log.warning("review.llm_unavailable", error=str(exc)[:200])

    log.info(
        "review.complete",
        models=len(contexts),
        findings=len(findings),
        skipped=len(skipped),
        executed=bool(execution and execution.ran),
        llm=bool(llm_summary),
    )
    # Last, after execution has had its chance to turn an inference into a measurement.
    # A critical that execution demonstrated keeps its level; one that is still a
    # prediction does not.
    findings = calibrate(findings)
    # Last of all, so every finding — measured, adjudicated, or neither — is placed on the
    # line of the file a reviewer will be reading, where its evidence allows.
    findings = position_findings(findings, acquired.after)

    reviewed = {c.model_name for c in contexts}
    untested = tuple(
        suggestion.model_name
        for suggestion in suggest_tests(acquired.after, grains)
        if suggestion.model_name in reviewed
    )

    return ReviewResult(
        findings=findings,
        skipped=skipped,
        grains=grains,
        governed_models=frozenset(
            name for name, model in acquired.after.models.items() if vocab.is_governed(model.tags)
        ),
        untested_grains=untested,
        models_reviewed=tuple(c.model_name for c in contexts),
        macro_affected=macro_affected,
        seed_affected=seed_affected,
        degraded_reason=acquired.degraded_reason,
        executed=bool(execution and execution.ran),
        execution=execution,
        execution_requested=run_execution,
        llm=llm_summary,
    )
