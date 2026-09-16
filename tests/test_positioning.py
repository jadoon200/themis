"""Putting a finding on the line it is about — and declining when that is not clear.

Every finding used to carry no line, so every SARIF annotation landed on line 1. The
fragments rules record are compiled SQL, rendered by sqlglot; the files people review are
raw Jinja. The tests use the demo project's real files for exactly that reason: a
positioning scheme tested against SQL that looks like its own fragments would pass and
place nothing on a real pull request.
"""

from __future__ import annotations

from pathlib import Path

from themis.analyze.positioning import locate, position_findings
from themis.models import Backend, Confidence, Evidence, Finding, Severity
from themis.snapshot import ModelNode, ProjectSnapshot

DEMO = Path(__file__).resolve().parents[1] / "demo_project" / "models"
CONVERTED = (DEMO / "intermediate" / "int_gl_entries_converted.sql").read_text()
RECOGNISED = (DEMO / "intermediate" / "int_revenue_recognized.sql").read_text()


def _line_of(text: str, needle: str) -> int:
    return next(n for n, line in enumerate(text.splitlines(), start=1) if needle in line)


def test_a_compiled_join_condition_is_placed_on_the_raw_line() -> None:
    fragment = (
        "INNER JOIN rates ON entries.currency_code = rates.currency_code "
        "AND entries.rate_period = rates.rate_date"
    )
    assert locate(CONVERTED, fragment) == _line_of(CONVERTED, "inner join rates")


def test_a_fully_qualified_relation_matches_its_ref() -> None:
    """Compiled SQL names the database and schema; the file only says ref('table').
    Counting those words against the file failed a join that was right there."""
    raw = (
        "select *\n"
        "from {{ ref('stg_gl_entries') }} as entries\n"
        "left join {{ ref('stg_contracts') }} as contracts\n"
        "    on entries.contract_id = contracts.contract_id\n"
    )
    fragment = (
        'LEFT JOIN "themis_demo"."main"."stg_contracts" AS contracts '
        "ON entries.contract_id = contracts.contract_id"
    )
    assert locate(raw, fragment) == 3


def test_a_macro_call_is_found_from_what_it_compiled_to() -> None:
    """The file says money(...); the fragment says CAST(ROUND(...)). They share the
    identifiers, which is all positioning needs."""
    fragment = "CAST(ROUND(entries.amount_txn_ccy * rates.rate, 2) AS DECIMAL(38, 2))"
    assert locate(CONVERTED, fragment) == _line_of(CONVERTED, "{{ money(")


def test_a_predicate_is_placed_where_it_is_written() -> None:
    fragment = "COALESCE(contracts.recognition_method, 'point_in_time')"
    assert locate(RECOGNISED, fragment) == _line_of(RECOGNISED, "coalesce(contracts")


def test_words_in_a_comment_do_not_count() -> None:
    """The converted model's header comment talks about rate_period and stg_fx_rates. A
    match there would point a reviewer at prose."""
    fragment = "entries.rate_period = rates.rate_date"
    line = locate(CONVERTED, fragment)
    assert line is not None
    assert not CONVERTED.splitlines()[line - 1].lstrip().startswith("--")


def test_a_fragment_that_is_not_in_the_file_is_not_placed() -> None:
    assert locate(CONVERTED, "customers.segment_code = segments.segment_code") is None


def test_a_fragment_too_generic_to_place_is_declined() -> None:
    """`amount > 0` could be anywhere. One distinctive word is not a position."""
    assert locate(CONVERTED, "amount > 0") is None


def _finding(family: str, sql_after: str | None, line: int | None = None) -> Finding:
    return Finding(
        rule_id=f"{family}001",
        family=family,
        title="t",
        severity=Severity.HIGH,
        confidence=Confidence.LIKELY,
        evidence=Evidence(model_name="int_gl_entries_converted", sql_after=sql_after, line=line),
        consequence="c",
    )


def _snapshot() -> ProjectSnapshot:
    return ProjectSnapshot(
        revision="r",
        backend=Backend.MANIFEST,
        models={
            "int_gl_entries_converted": ModelNode(
                name="int_gl_entries_converted",
                unique_id="model.d.int_gl_entries_converted",
                file_path="models/intermediate/int_gl_entries_converted.sql",
                raw_sql=CONVERTED,
            )
        },
    )


def test_findings_are_given_the_line_their_evidence_is_on() -> None:
    placed = position_findings(
        [_finding("F1", "entries.currency_code = rates.currency_code")], _snapshot()
    )
    assert placed[0].evidence.line == _line_of(CONVERTED, "on entries.currency_code")


def test_a_config_finding_with_no_fragment_goes_to_the_config_block() -> None:
    placed = position_findings([_finding("F5", None)], _snapshot())
    assert placed[0].evidence.line == _line_of(CONVERTED, "config(")


def test_a_line_a_rule_already_set_is_kept() -> None:
    placed = position_findings([_finding("F1", "entries.currency_code", line=7)], _snapshot())
    assert placed[0].evidence.line == 7


def test_a_whole_model_finding_stays_unplaced() -> None:
    """'This model's totals moved' is about the model. There is no line to point at."""
    placed = position_findings([_finding("X", None)], _snapshot())
    assert placed[0].evidence.line is None
