"""F9 — snapshots, and the history they keep.

A snapshot is the one thing in a dbt project whose table cannot be rebuilt. A model can
be dropped and run again from its sources; a snapshot's earlier versions exist nowhere
but in its own table, recorded one run at a time. So an edit to how it recognises a row,
notices a change or treats a deletion is permanent for everything written before it —
and it merges green, because a snapshot built from nothing has no history to get wrong.

That is also why most of these are read from configuration rather than measured. A
build in an empty schema writes one version of everything; the damage appears on the
first run over history that already exists, which is the production table.

At work, snapshots are the slowly-changing data Dagster writes to Iceberg.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp

from themis.analyze import history
from themis.analyze.grain import query_grain
from themis.analyze.parse import ParseError, find_joins, join_kind, parse_sql
from themis.analyze.volatility import volatile_columns
from themis.models import Confidence, Evidence, Finding, Grain, Severity
from themis.rules.base import Rule, RuleContext
from themis.snapshot import HistoryConfig, ModelNode, ProjectSnapshot

FAMILY = "F9"


def _histories(ctx: RuleContext) -> tuple[HistoryConfig | None, HistoryConfig | None]:
    before = ctx.before.history if ctx.before is not None else None
    after = ctx.after.history if ctx.after is not None else None
    return before, after


def _key(node: ModelNode) -> tuple[str, ...]:
    return tuple(sorted(column.lower() for column in node.unique_key))


def _relation(node: ModelNode) -> str:
    return (node.relation_name or node.name).replace('"', "").replace("`", "").lower()


def _covered(grain: Grain, node: ModelNode) -> bool:
    """The query is provably unique on the columns the snapshot's key is made of."""
    columns = {c.lower() for c in history.key_columns(node)}
    return grain.is_proven and bool(columns) and {c.lower() for c in grain.columns} <= columns


def _finding(
    rule: Rule,
    ctx: RuleContext,
    *,
    title: str,
    confidence: Confidence,
    note: str,
    consequence: str,
    suggestion: str,
    severity: Severity | None = None,
    sql_after: str | None = None,
    related_model: str | None = None,
) -> Finding:
    node = ctx.after or ctx.before
    assert node is not None
    return Finding(
        rule_id=rule.rule_id,
        family=rule.family,
        title=title,
        severity=severity or rule.severity,
        confidence=confidence,
        evidence=Evidence(
            model_name=ctx.model_name,
            file_path=node.file_path,
            sql_after=sql_after,
            note=note,
            related_model=related_model,
        ),
        consequence=consequence,
        suggestion=suggestion,
        blast_radius=ctx.blast_radius,
    )


@dataclass
class SnapshotKeyNotUniqueRule(Rule):
    """The snapshot's query is not unique on its ``unique_key``.

    dbt does not check. On Trino the snapshot's MERGE refuses the moment one stored
    version matches two query rows — measured on the demo: the build passes, and the next
    run fails with MERGE_TARGET_ROW_MULTIPLE_MATCHES. An engine that does not refuse
    writes several current versions of one key instead: on DuckDB the demo's 20 accounts
    became 420 rows over three runs, 320 of them current.

    Fires when the change is what put the key in doubt: a new snapshot, a changed key, or
    a query that was provably unique on the key and no longer is.
    """

    rule_id: str = field(init=False, default="F9001")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        after = ctx.after
        if after is None or after.history is None or not after.unique_key:
            return []
        grain = query_grain(after, ctx.after_snapshot, ctx.grains, dialect=ctx.dialect)
        if _covered(grain, after):
            return []

        before = ctx.before
        if before is not None and before.history is not None and _key(before) == _key(after):
            was = query_grain(before, ctx.before_snapshot, ctx.grains, dialect=ctx.dialect)
            if not _covered(was, before):
                return []  # no more in doubt than it already was: not this change

        key = ", ".join(after.unique_key)
        if grain.is_proven:
            note = (
                f"unique_key ({key}); the query is unique on ({', '.join(grain.columns)}) "
                f"[{grain.source.value}], which the key does not cover"
            )
            confidence = Confidence.LIKELY
        else:
            note = f"unique_key ({key}); the query's grain could not be derived: {grain.note}"
            confidence = Confidence.POSSIBLE
        return [
            _finding(
                self,
                ctx,
                title=f"Snapshot key ({key}) may not identify one row of its query",
                confidence=confidence,
                note=note,
                consequence=(
                    "A snapshot matches each stored version to the query by its key. Where "
                    "two query rows share a key, Trino's MERGE refuses on the next run "
                    "(MERGE_TARGET_ROW_MULTIPLE_MATCHES) and the snapshot stops recording "
                    "anything; an engine that does not refuse writes several current "
                    "versions of one key, and everything reading the current version counts "
                    "it more than once."
                ),
                suggestion=(
                    "Make the unique_key the query's real key — every column that "
                    "identifies a row — or deduplicate the query to one row per key "
                    "before it reaches the snapshot."
                ),
            )
        ]


@dataclass
class SnapshotRekeyedRule(Rule):
    """``unique_key`` changed on a snapshot that already has history."""

    rule_id: str = field(init=False, default="F9002")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        before, after = ctx.before, ctx.after
        if before is None or after is None or before.history is None or after.history is None:
            return []
        if _key(before) == _key(after):
            return []
        was, now = ", ".join(before.unique_key), ", ".join(after.unique_key)
        return [
            _finding(
                self,
                ctx,
                title="Snapshot re-keyed: existing history is matched by a new key",
                confidence=Confidence.LIKELY,
                note=f"unique_key ({was}) -> ({now})",
                consequence=(
                    "From the next run, every version already written is matched to the "
                    "query by the new key, recomputed from the stored rows. A stored row "
                    "without the new key's columns, or with other values in them, matches "
                    "nothing: its current version is never closed, a second current version "
                    "is written beside it, and the history before this change no longer "
                    "joins to the history after it. Nothing fails."
                ),
                suggestion=(
                    "Treat re-keying as a migration. Either start a new snapshot under the "
                    "new key and keep the old one readable, or confirm on the production "
                    "table that every current version already has the new key's columns "
                    "and that the new key picks out exactly one current version per old key."
                ),
            )
        ]


@dataclass
class ChangeDetectionChangedRule(Rule):
    """How the snapshot decides a row changed was edited.

    ``strategy``, ``updated_at`` and ``check_cols`` decide when a new version is written.
    Changing any of them changes what the history means from that run on, while
    everything before keeps the old meaning — in one table, with nothing marking where
    one ends and the other begins. Widening ``check_cols`` is left alone: it records
    changes that were missed before and loses none.
    """

    rule_id: str = field(init=False, default="F9003")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        before, after = _histories(ctx)
        if before is None or after is None:
            return []

        if before.strategy != after.strategy:
            return [
                _finding(
                    self,
                    ctx,
                    title=(f"Snapshot strategy changed from {before.strategy} to {after.strategy}"),
                    confidence=Confidence.LIKELY,
                    note=f"strategy: {before.strategy} -> {after.strategy}",
                    consequence=(
                        "Versions written before and after this change mean different "
                        "things. Under `timestamp` a version begins when the source says the "
                        "row changed; under `check` it begins when a run happened to notice. "
                        "An as-of join across the boundary answers with the wrong version, "
                        "and the dates on the old versions are not comparable with the new."
                    ),
                    suggestion=(
                        "Keep the strategy, or start a new snapshot with the new one and "
                        "leave the old history readable where it is."
                    ),
                )
            ]

        if (
            after.strategy == "timestamp"
            and (before.updated_at or "").lower() != (after.updated_at or "").lower()
        ):
            return [
                _finding(
                    self,
                    ctx,
                    title=(
                        f"Snapshot now detects changes by {after.updated_at} instead of "
                        f"{before.updated_at}"
                    ),
                    confidence=Confidence.LIKELY,
                    note=f"updated_at: {before.updated_at} -> {after.updated_at}",
                    consequence=(
                        "A new version is written when updated_at passes the last version's. "
                        "A column that runs behind the old one records nothing until it "
                        "catches up, silently missing every change in between; one that runs "
                        "ahead writes a new version of every row on the next run."
                    ),
                    suggestion=(
                        "Compare the two columns on the production table before switching, "
                        "and confirm the new one is never earlier than the old for any "
                        "current version."
                    ),
                )
            ]

        if after.strategy == "check":
            dropped = _dropped_check_cols(before, after)
            if dropped:
                return [
                    _finding(
                        self,
                        ctx,
                        title=f"Snapshot stops recording changes to {', '.join(dropped)}",
                        severity=Severity.MEDIUM,
                        confidence=Confidence.LIKELY,
                        note=(
                            f"check_cols: {', '.join(before.check_cols)} -> "
                            f"{', '.join(after.check_cols)}"
                        ),
                        consequence=(
                            "A change to a column no longer checked writes no new version, "
                            "so its history stops here. The column is still stored — the "
                            "current version keeps whatever value it had when some other "
                            "column last changed, which is not the value at the source."
                        ),
                        suggestion=(
                            "Keep the column in check_cols, or stop selecting it in the "
                            "snapshot's query so no stale value is stored."
                        ),
                    )
                ]
        return []


def _dropped_check_cols(before: HistoryConfig, after: HistoryConfig) -> tuple[str, ...]:
    """Columns checked before and not after. From `all` to a list is a narrowing too."""
    if after.checks_all:
        return ()
    now = {c.lower() for c in after.check_cols}
    if before.checks_all:
        return ("columns not in the new list",)
    return tuple(c for c in before.check_cols if c.lower() not in now)


@dataclass
class VolatileChangeDetectionRule(Rule):
    """Change detection reads a value stamped at build time.

    ``updated_at`` from ``current_timestamp`` is newer on every run, so every run writes
    a new version of every row — measured on the demo: 40 rows, 80 after the second run,
    120 after the third. The same holds for a checked column computed from one.
    """

    rule_id: str = field(init=False, default="F9004")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        after = ctx.after
        if after is None or after.history is None:
            return []
        stamped = _stamped_detection(after, ctx.after_snapshot, ctx.dialect)
        if not stamped:
            return []
        if (
            ctx.before is not None
            and ctx.before.history is not None
            and _stamped_detection(ctx.before, ctx.before_snapshot, ctx.dialect)
        ):
            return []  # stamped before as well: not this change
        columns = ", ".join(stamped)
        return [
            _finding(
                self,
                ctx,
                title="Snapshot writes a new version of every row on every run",
                confidence=Confidence.PROVEN,
                note=(
                    f"{after.history.strategy} strategy reads {columns}, computed from a "
                    "value stamped when the build runs"
                ),
                consequence=(
                    "Every run sees every row as changed, closes its current version and "
                    "writes another. The table grows by its whole size each run, the history "
                    "records changes that never happened, and a question about when a row "
                    "really changed can no longer be answered."
                ),
                suggestion=(
                    "Detect changes by a value the source owns — its own last-modified "
                    "time, or the business columns under the check strategy — never by a "
                    "time the build computes."
                ),
            )
        ]


def _stamped_detection(node: ModelNode, project: ProjectSnapshot, dialect: str) -> tuple[str, ...]:
    """The change-detection columns this snapshot computes from build time."""
    config = node.history
    if config is None:
        return ()
    # Only the query's own columns: under `check` the bookkeeping columns are stamped by
    # design, and that is not this defect.
    stamped = set(volatile_columns(project, dialect=dialect).get(node.name, frozenset()))
    stamped -= set(config.meta_columns)
    if not stamped:
        return ()
    if config.strategy == "timestamp":
        return (config.updated_at,) if config.updated_at in stamped else ()
    if config.strategy == "check":
        if config.checks_all:
            return tuple(sorted(stamped))
        return tuple(c for c in config.check_cols if c in stamped)
    return ()


@dataclass
class HardDeletesRule(Rule):
    """What a row disappearing from the query means to history changed.

    Two shapes. Tracking deletions switched off: a row deleted at the source keeps a
    current version forever. And a filter added to a snapshot that does track them:
    every row the filter excludes is recorded as deleted on the next run — a deletion
    that never happened, written into history that cannot be rewritten.
    """

    rule_id: str = field(init=False, default="F9005")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        before, after = _histories(ctx)
        if before is None or after is None:
            return []
        if before.hard_deletes != after.hard_deletes:
            return [self._setting_changed(ctx, before, after)]
        if after.hard_deletes == "ignore":
            return []
        added = _narrowing(ctx)
        if not added:
            return []
        return [
            _finding(
                self,
                ctx,
                title="Rows filtered out of the snapshot will be recorded as deleted",
                confidence=Confidence.LIKELY,
                note=f"hard_deletes='{after.hard_deletes}'; the query now {added}",
                sql_after=added,
                consequence=(
                    "The snapshot tracks deletions, so every stored key the query stops "
                    "returning is closed on the next run as though it had been deleted at "
                    "the source. Those keys vanish from the current version, and history "
                    "records a deletion that never happened — permanently, since a "
                    "snapshot's past is not rebuilt."
                ),
                suggestion=(
                    "Filter where the snapshot is read, not in its query — or, if the "
                    "excluded rows should never have been tracked, start a new snapshot "
                    "with the filter rather than editing this one."
                ),
            )
        ]

    def _setting_changed(
        self, ctx: RuleContext, before: HistoryConfig, after: HistoryConfig
    ) -> Finding:
        note = f"hard_deletes: {before.hard_deletes} -> {after.hard_deletes}"
        if after.hard_deletes == "ignore":
            return _finding(
                self,
                ctx,
                title="Snapshot stops recording deletions",
                confidence=Confidence.LIKELY,
                note=note,
                consequence=(
                    "A row deleted at the source keeps a current version for ever. Anything "
                    "reading the current version goes on counting it — a closed account, "
                    "a cancelled contract — with nothing to show it is gone."
                ),
                suggestion="Keep hard_deletes='invalidate' (or 'new_record').",
            )
        if after.hard_deletes == "new_record":
            return _finding(
                self,
                ctx,
                title="Deleted rows now appear as current versions",
                severity=Severity.MEDIUM,
                confidence=Confidence.LIKELY,
                note=note,
                consequence=(
                    "Under 'new_record' a deletion is written as a new version with "
                    f"{after.column('dbt_is_deleted')} true, and that version is current. "
                    "Every model taking the current version now includes deleted rows unless "
                    "it also filters on that column."
                ),
                suggestion=(
                    f"Add `{after.column('dbt_is_deleted')} = false` wherever the current "
                    "version is read, in the same change."
                ),
            )
        return _finding(
            self,
            ctx,
            title=f"Snapshot now treats deletions as '{after.hard_deletes}'",
            severity=Severity.MEDIUM,
            confidence=Confidence.POSSIBLE,
            note=note,
            consequence=(
                "Deletions recorded before this change and after it are represented "
                "differently in one table."
            ),
            suggestion="Confirm every reader of the snapshot handles both representations.",
        )


def _conjunct_texts(tree: exp.Expression, dialect: str) -> set[str]:
    texts: set[str] = set()
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        if isinstance(where, exp.Where):
            for term in _split_and(where.this):
                texts.add(term.sql(dialect=dialect, normalize=True).lower())
    return texts


def _split_and(expression: exp.Expression) -> list[exp.Expression]:
    while isinstance(expression, exp.Paren):
        expression = expression.this
    if isinstance(expression, exp.And):
        return [*_split_and(expression.this), *_split_and(expression.expression)]
    return [expression]


def _inner_joins(tree: exp.Expression) -> int:
    return sum(1 for join in find_joins(tree) if join_kind(join) == "INNER")


def _narrowing(ctx: RuleContext) -> str | None:
    """What the change added that can remove rows from the snapshot's query, if anything."""
    before_sql = ctx.before.analysable_sql if ctx.before else None
    after_sql = ctx.after.analysable_sql if ctx.after else None
    if before_sql is None or after_sql is None:
        return None
    try:
        before_tree = parse_sql(before_sql, dialect=ctx.dialect)
        after_tree = parse_sql(after_sql, dialect=ctx.dialect)
    except ParseError:
        return None
    added = sorted(
        _conjunct_texts(after_tree, ctx.dialect) - _conjunct_texts(before_tree, ctx.dialect)
    )
    if added:
        return f"filters on `{added[0]}`"
    if _inner_joins(after_tree) > _inner_joins(before_tree):
        return "has an inner join, which drops every row without a match"
    if after_tree.find(exp.Limit) is not None and before_tree.find(exp.Limit) is None:
        return "has a LIMIT"
    return None


@dataclass
class SnapshotMovedRule(Rule):
    """A snapshot's table is somewhere else now, and its history is not."""

    rule_id: str = field(init=False, default="F9006")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)
    requires_compiled_sql: bool = field(init=False, default=False)

    def check(self, ctx: RuleContext) -> list[Finding]:
        before, after = ctx.before, ctx.after
        if before is None or after is None or before.history is None or after.history is None:
            return []
        if not before.relation_name or not after.relation_name:
            return []
        was, now = _relation(before), _relation(after)
        if was == now:
            return []
        return [
            _finding(
                self,
                ctx,
                title="Snapshot moved: its history stays behind",
                confidence=Confidence.PROVEN,
                note=f"{was} -> {now}",
                consequence=(
                    f"The next run finds no table at {now} and starts one from nothing: "
                    "every key's first version is dated to that run. The history up to now "
                    f"stays in {was}, which nothing builds any more, and every model reading "
                    "the snapshot reads only what was recorded after the move."
                ),
                suggestion=(
                    f"Copy {was} to {now} before the next run, or leave the snapshot where it is."
                ),
            )
        ]


@dataclass
class ReadsEveryVersionRule(Rule):
    """A model reads a snapshot without taking one version per key.

    Invisible on the day it merges: a snapshot holds one version of nearly everything
    until something changes. From then on each change adds a row per key here, and every
    sum over it grows by itself.
    """

    rule_id: str = field(init=False, default="F9007")
    family: str = field(init=False, default=FAMILY)
    severity: Severity = field(init=False, default=Severity.HIGH)

    def check(self, ctx: RuleContext) -> list[Finding]:
        after = ctx.after
        if after is None or after.is_snapshot or after.analysable_sql is None:
            return []
        parents = history.snapshot_parents(after, ctx.after_snapshot)
        if not parents:
            return []
        try:
            after_tree = parse_sql(after.analysable_sql, dialect=ctx.dialect)
        except ParseError:
            return []
        before_tree: exp.Expression | None = None
        if ctx.before is not None and ctx.before.analysable_sql is not None:
            try:
                before_tree = parse_sql(ctx.before.analysable_sql, dialect=ctx.dialect)
            except ParseError:
                before_tree = None

        findings: list[Finding] = []
        for name, snapshot in sorted(parents.items()):
            reads = history.reads_of(after_tree, snapshot)
            if not reads or all(read.one_version for read in reads):
                continue
            was_restricted = before_tree is not None and history.restricted_to_one_version(
                before_tree, snapshot
            )
            read_before = before_tree is not None and bool(history.reads_of(before_tree, snapshot))
            if was_restricted:
                # A restriction removed: whatever the output looks like, it now carries a
                # row per version where it carried one per key.
                how = "no longer restricts its read to one version"
            elif read_before:
                continue  # it read every version before as well: not this change
            elif history.keeps_versions(after_tree, snapshot):
                continue  # hands the versions on: a history model, which is the point
            else:
                how = "reads it without restricting to one version"
            valid_to = snapshot.history.valid_to if snapshot.history else "dbt_valid_to"
            findings.append(
                _finding(
                    self,
                    ctx,
                    title=f"Reads every version of snapshot {name}",
                    confidence=Confidence.LIKELY,
                    note=f"{ctx.model_name} {how} of {name}",
                    related_model=name,
                    consequence=(
                        f"{name} keeps a row for every version of each key. Read whole, each "
                        "key counts once per change ever recorded against it: today most "
                        "keys have one version, so the result looks right, and every later "
                        "change at the source adds a duplicate here and to any sum over it."
                    ),
                    suggestion=(
                        f"Take the current version (`where {valid_to} is null`), or join as "
                        f"of a date between dbt_valid_from and {valid_to}."
                    ),
                )
            )
        return findings


RULES: tuple[Rule, ...] = (
    SnapshotKeyNotUniqueRule(),
    SnapshotRekeyedRule(),
    ChangeDetectionChangedRule(),
    VolatileChangeDetectionRule(),
    HardDeletesRule(),
    SnapshotMovedRule(),
    ReadsEveryVersionRule(),
)
