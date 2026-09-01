"""Known defects, injected on purpose.

Each mutation is a small, realistic edit to the demo project — the kind of change that
turns up in a real pull request and looks entirely reasonable in a diff. Some are
genuine defects; some are behaviour-preserving refactors. Which is which is *not*
declared here: the execution oracle decides by building both revisions and comparing
the results, so the corpus labels itself.

That matters because a hand-labelled corpus encodes the author's belief about what the
tool should catch, and then measures the tool against that belief. Letting execution
decide measures it against the data instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Kind(StrEnum):
    """What the mutation is meant to be.

    Only used to report results by group. Whether a DEFECT actually changes the numbers
    is settled by execution, and a DEFECT that turns out not to is reported as such
    rather than quietly counted as one.
    """

    DEFECT = "defect"
    CONTROL = "control"


@dataclass(frozen=True)
class Mutation:
    """One injected change."""

    id: str
    kind: Kind
    # The rule family expected to notice. Empty for controls, which expect silence.
    expects_family: str
    description: str
    relative_path: str
    find: str
    replace: str

    def apply(self, project_dir: Path) -> bool:
        """Apply to a checked-out project. False if the anchor text is not present."""
        target = project_dir / self.relative_path
        if not target.exists():
            return False
        source = target.read_text()
        if self.find not in source:
            return False
        target.write_text(source.replace(self.find, self.replace, 1))
        return True


_INT_CONVERTED = "models/intermediate/int_gl_entries_converted.sql"
_INT_REVENUE = "models/intermediate/int_revenue_recognized.sql"
_MART_SUMMARY = "models/marts/fct_regulatory_summary.sql"
_STG_ENTRIES = "models/staging/stg_gl_entries.sql"
_MACRO_MONEY = "macros/money.sql"
_REVENUE_FILTER = (
    "    where {{ external_revenue_filter('accounts.account_type', 'accounts.is_intercompany') }}"
)


DEFECTS: tuple[Mutation, ...] = (
    Mutation(
        id="fanout_drop_join_predicate",
        kind=Kind.DEFECT,
        expects_family="F1",
        description="FX join loses its period predicate, so every entry matches every month's rate",
        relative_path=_INT_CONVERTED,
        find="""        on entries.currency_code = rates.currency_code
        and entries.rate_period = rates.rate_date""",
        replace="        on entries.currency_code = rates.currency_code",
    ),
    Mutation(
        id="join_left_to_inner",
        kind=Kind.DEFECT,
        expects_family="F1",
        description="Contract join tightened to INNER, silently dropping entries with no contract",
        relative_path=_INT_REVENUE,
        find="    left join contracts",
        replace="    inner join contracts",
    ),
    Mutation(
        id="grain_drop_group_by_key",
        kind=Kind.DEFECT,
        expects_family="F1",
        description=(
            "Currency dropped from the regulatory summary's grain, mixing currencies in one total"
        ),
        relative_path=_MART_SUMMARY,
        find="""    group by
        period_month,
        entity_code,
        currency_code""",
        replace="""    group by
        period_month,
        entity_code""",
    ),
    Mutation(
        id="money_cast_to_double",
        kind=Kind.DEFECT,
        expects_family="F3",
        description=(
            "money() macro switched to DOUBLE, drifting cents across every model that uses it"
        ),
        relative_path=_MACRO_MONEY,
        find="    cast({{ expr }} as decimal(38, 6))\n{% endmacro %}",
        replace="    cast({{ expr }} as double)\n{% endmacro %}",
    ),
    Mutation(
        id="decimal_scale_reduced",
        kind=Kind.DEFECT,
        expects_family="F3",
        description="Monetary scale cut from 6 to 2, truncating converted amounts",
        relative_path=_INT_CONVERTED,
        find="{{ money('entries.amount_txn_ccy * rates.rate') }}   as amount_usd",
        replace="cast(entries.amount_txn_ccy * rates.rate as decimal(18, 2)) as amount_usd",
    ),
    Mutation(
        id="filter_dropped_reversals",
        kind=Kind.DEFECT,
        expects_family="F2",
        description="Reversal entries filtered out, overstating gross revenue",
        relative_path=_INT_REVENUE,
        find=_REVENUE_FILTER,
        replace=_REVENUE_FILTER + "\n        and not converted.is_reversal",
    ),
    Mutation(
        id="intercompany_filter_removed",
        kind=Kind.DEFECT,
        expects_family="F2",
        description="Intercompany exclusion dropped, inflating external revenue",
        relative_path=_INT_REVENUE,
        find=_REVENUE_FILTER,
        replace="    where accounts.account_type = 'revenue'",
    ),
    Mutation(
        id="period_boundary_shifted",
        kind=Kind.DEFECT,
        expects_family="F4",
        description="Rate period truncated to year, pulling the wrong FX rate for every month",
        relative_path=_STG_ENTRIES,
        find="{{ period_start('cast(posting_date as date)') }}  as rate_period",
        replace="cast(date_trunc('year', cast(posting_date as date)) as date) as rate_period",
    ),
    Mutation(
        id="sign_convention_flipped",
        kind=Kind.DEFECT,
        expects_family="F3",
        description="Debit/credit sign inverted, flipping the sense of every amount",
        relative_path="macros/money.sql",
        find="""        when {{ entry_type_expr }} = 'credit' then {{ minor_to_major(amount_expr) }}
        else -1 * {{ minor_to_major(amount_expr) }}""",
        replace=(
            "        when {{ entry_type_expr }} = 'debit'"
            " then {{ minor_to_major(amount_expr) }}\n"
            "        else -1 * {{ minor_to_major(amount_expr) }}"
        ),
    ),
)


CONTROLS: tuple[Mutation, ...] = (
    Mutation(
        id="control_rename_ctes",
        kind=Kind.CONTROL,
        expects_family="",
        description="CTEs renamed — a routine tidy-up that changes nothing",
        relative_path=_INT_CONVERTED,
        find="""with entries as (

    select * from {{ ref('stg_gl_entries') }}

),

rates as (

    select * from {{ ref('stg_fx_rates') }}

),""",
        replace="""with ledger_entries as (

    select * from {{ ref('stg_gl_entries') }}

),

fx as (

    select * from {{ ref('stg_fx_rates') }}

),

entries as (select * from ledger_entries),

rates as (select * from fx),""",
    ),
    Mutation(
        id="control_add_comments",
        kind=Kind.CONTROL,
        expects_family="",
        description="Explanatory comments added",
        relative_path=_MART_SUMMARY,
        find="with revenue as (",
        replace=(
            "-- Pull recognised revenue, then aggregate to the reporting grain.\nwith revenue as ("
        ),
    ),
    Mutation(
        id="control_reformat_join",
        kind=Kind.CONTROL,
        expects_family="",
        description="Join reformatted across lines, semantics untouched",
        relative_path=_INT_REVENUE,
        find="""    inner join accounts
        on converted.account_id = accounts.account_id""",
        replace="""    inner join
        accounts
        on
            converted.account_id
            = accounts.account_id""",
    ),
    Mutation(
        id="control_reorder_projection",
        kind=Kind.CONTROL,
        expects_family="",
        description="Select list reordered — column order is not semantics",
        relative_path="models/marts/fct_revenue.sql",
        find="""    entry_id,
    account_id,
    contract_id,
    customer_id,""",
        replace="""    account_id,
    entry_id,
    customer_id,
    contract_id,""",
    ),
    Mutation(
        id="control_widen_decimal",
        kind=Kind.CONTROL,
        expects_family="",
        description="Monetary precision widened — loses nothing",
        relative_path=_INT_CONVERTED,
        find="{{ money('entries.amount_txn_ccy * rates.rate') }}   as amount_usd",
        replace="cast(entries.amount_txn_ccy * rates.rate as decimal(38, 9)) as amount_usd",
    ),
)


ALL: tuple[Mutation, ...] = DEFECTS + CONTROLS


def select(name: str) -> tuple[Mutation, ...]:
    """Resolve a mutation selector: 'all', 'defects', 'controls', or an id."""
    if name == "all":
        return ALL
    if name == "defects":
        return DEFECTS
    if name == "controls":
        return CONTROLS
    matched = tuple(m for m in ALL if m.id == name)
    if not matched:
        raise KeyError(f"unknown mutation {name!r}")
    return matched
