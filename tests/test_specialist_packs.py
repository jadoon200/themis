"""Each reviewer gets the evidence its own question turns on, and no more.

A pack built the same way for everyone has to carry everything anyone might need, so
nobody's input is narrow — and narrowing the input is the cheapest grounding available.
The failure this guards is quieter than a wrong answer: a specialist asked whether an
incremental strategy change is safe, while being shown no strategy, can only restate
the rule it was handed and will look like it agreed.
"""

from __future__ import annotations

from themis.llm.context_pack import Section, build_pack
from themis.models import Backend, Confidence, Evidence, Finding, Grain, GrainSource, Severity
from themis.review.specialists import ALL_SPECIALISTS, INCREMENTAL, specialist_for
from themis.rules.registry import ALL_RULES
from themis.snapshot import ColumnSchema, ModelNode, ProjectSnapshot


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "fct": ModelNode(
                name="fct",
                unique_id="model.d.fct",
                file_path="models/fct.sql",
                compiled_sql="select id, amount from up",
                materialization="incremental",
                incremental_strategy="delete+insert",
                unique_key=("entry_id",),
                on_schema_change="fail",
                tags=("regulatory",),
                columns=(ColumnSchema(name="amount", data_type="DOUBLE"),),
                depends_on_models=("model.d.up",),
            ),
            "up": ModelNode(
                name="up",
                unique_id="model.d.up",
                file_path="models/up.sql",
                compiled_sql="select id, amount from raw",
            ),
        },
    )


def _finding(family: str = "F5", rule_id: str = "F5002") -> Finding:
    return Finding(
        rule_id=rule_id,
        family=family,
        title="something changed",
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name="fct", column_name="amount", note="a note"),
        consequence="numbers could move",
        blast_radius=("downstream_a",),
    )


def _pack_text(needs: frozenset[Section]) -> str:
    return build_pack(
        _finding(),
        snapshot=_snapshot(),
        grains={"fct": Grain(model_name="fct", columns=("id",), source=GrainSource.STRUCTURAL)},
        needs=needs,
    ).text


def test_every_rule_family_has_a_reviewer() -> None:
    """A family with no specialist returns unadjudicated findings.

    Which is indistinguishable, in the report, from a specialist that looked and
    declined to change anything.
    """
    for rule in ALL_RULES:
        assert specialist_for(rule.family) is not None, f"{rule.rule_id} ({rule.family})"


def test_no_two_specialists_claim_the_same_family() -> None:
    seen: set[str] = set()
    for specialist in ALL_SPECIALISTS:
        overlap = seen & specialist.families
        assert not overlap, f"{specialist.name} also claims {overlap}"
        seen |= specialist.families


def test_the_incremental_reviewer_is_shown_the_config_it_is_judging() -> None:
    """The gap this whole change existed to close."""
    text = _pack_text(INCREMENTAL.needs)
    assert "delete+insert" in text
    assert "entry_id" in text
    assert "on_schema_change: fail" in text


def test_the_money_reviewer_is_shown_declared_column_types() -> None:
    """`DOUBLE` on a monetary column is the claim F3001 makes; it must not be guessed."""
    text = _pack_text(frozenset({Section.COLUMN_TYPES}))
    assert "amount: DOUBLE" in text


def test_a_reviewer_is_not_shown_evidence_it_did_not_ask_for() -> None:
    text = _pack_text(frozenset({Section.COLUMN_TYPES}))
    assert "How this model is built" not in text
    assert "Downstream models" not in text


def test_the_finding_itself_is_always_present() -> None:
    """Whatever a reviewer asked for, it is still judging one specific flag."""
    text = _pack_text(frozenset())
    assert "F5002" in text
    assert "numbers could move" in text


def test_asking_for_nothing_in_particular_yields_everything() -> None:
    """A caller with no specialist in hand should not silently get a thin pack."""
    full = build_pack(
        _finding(),
        snapshot=_snapshot(),
        grains={},
        needs=None,
    ).text
    assert "How this model is built" in full
    assert "Declared column types" in full


def test_the_contracts_reviewer_is_told_what_actually_reads_the_column() -> None:
    """Model-granular blast radius over-states every column change.

    Fourteen models downstream, thirteen of which never touch the column that moved,
    is not evidence — it is noise the reviewer has to discount, and a model asked to
    discount it will sometimes fail to.
    """
    from themis.analyze.lineage import build_column_graph

    snapshot = ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "fct": ModelNode(
                name="fct",
                unique_id="model.d.fct",
                file_path="models/fct.sql",
                compiled_sql="select id, amount from raw_t",
            ),
            "mart": ModelNode(
                name="mart",
                unique_id="model.d.mart",
                file_path="models/mart.sql",
                compiled_sql="select id, sum(amount) as total from fct group by id",
                depends_on_models=("model.d.fct",),
            ),
        },
        child_map={"fct": ("mart",)},
    )
    pack = build_pack(
        _finding(family="F6", rule_id="F6001"),
        snapshot=snapshot,
        grains={},
        needs=frozenset({Section.COLUMN_CONSUMERS}),
        lineage=build_column_graph(snapshot),
    )
    # Traced through the SQL, so the renamed downstream column is named.
    assert "mart.total" in pack.text


def test_an_unresolved_model_is_reported_as_unknown_not_as_safe() -> None:
    """Silence from lineage must not read to the model as 'nothing depends on it'."""
    from themis.analyze.lineage import ColumnGraph

    graph = ColumnGraph(unresolved={"fct": "unparseable"})
    pack = build_pack(
        _finding(family="F6", rule_id="F6001"),
        snapshot=_snapshot(),
        grains={},
        needs=frozenset({Section.COLUMN_CONSUMERS}),
        lineage=graph,
    )
    assert "unresolved" in pack.text
    assert "nobody knows" in pack.text
