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

    Whether a DEFECT actually changes the numbers is settled by execution, and one
    that turns out not to is reported rather than quietly counted as one.

    LATENT exists because the execution oracle has a real blind spot. It asks "did the
    numbers move", which is the right question for most defects and the wrong one for
    three kinds:

    - **cost** — dropping an incremental guard reprocesses all history and produces
      byte-identical output at many times the price;
    - **lineage** — replacing ref() with a literal name reads the same table today,
      while removing the DAG edge that guarantees build order and keeps dev out of
      production;
    - **latent** — narrowing a late-arrival window loses nothing until something
      actually arrives late.

    Scoring these against execution would count a correct flag as a false positive and
    push the tool towards ignoring them. They are scored on detection instead, and
    reported separately so the distinction stays visible.

    GENERATED marks a mutation produced mechanically from the code rather than chosen
    by anyone. Whether it is a defect is unknown in advance and settled by execution.
    These are the only cases in the corpus not selected with knowledge of the rules, so
    a generated mutation that moves the numbers and goes unreported is the most useful
    signal available: a real defect class nobody anticipated.

    UNRULED is a defect deliberately outside every rule family. It exists to keep the
    corpus honest: every other mutation is a class somebody wrote a rule for, so the
    rules will always win on them. An unruled defect tests the safety net instead —
    whether a measured change nobody anticipated is still reported rather than passing
    as a clean review.
    """

    DEFECT = "defect"
    CONTROL = "control"
    LATENT = "latent"
    UNRULED = "unruled"
    GENERATED = "generated"


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
_INCREMENTAL = "models/marts/fct_revenue_incremental.sql"
_MART_REVENUE = "models/marts/fct_revenue.sql"
_STG_CONTRACTS = "models/staging/stg_contracts.sql"
_CONTRACT_MART = "models/marts/dim_entity_contract.sql"
_DIM_ENTITIES = "models/marts/dim_entities.sql"
_REVENUE_FILTER = (
    "    where {{ external_revenue_filter('accounts.account_type', 'accounts.is_intercompany') }}"
)


_ALL_INJECTED: tuple[Mutation, ...] = (
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
        id="incremental_guard_removed",
        kind=Kind.LATENT,
        expects_family="F5",
        description="is_incremental() guard dropped, so every run reprocesses all history",
        relative_path=_INCREMENTAL,
        find="""    {% if is_incremental() %}
    where posting_date >= (
        select coalesce(max(posting_date), date '1900-01-01') - interval '3' day
        from {{ this }}
    )
    {% endif %}
""",
        replace="",
    ),
    Mutation(
        id="incremental_strategy_to_append",
        kind=Kind.DEFECT,
        expects_family="F5",
        description="Strategy switched to append, which never deduplicates",
        relative_path=_INCREMENTAL,
        find="    incremental_strategy='delete+insert',",
        replace="    incremental_strategy='append',",
    ),
    Mutation(
        id="incremental_lookback_narrowed",
        kind=Kind.LATENT,
        expects_family="F5",
        description="Late-arrival window cut from 3 days to 1, silently dropping late rows",
        relative_path=_INCREMENTAL,
        find="- interval '3' day",
        replace="- interval '1' day",
    ),
    Mutation(
        id="incremental_key_changed",
        kind=Kind.DEFECT,
        expects_family="F5",
        description="unique_key changed, so existing rows match differently",
        relative_path=_INCREMENTAL,
        find="    unique_key='entry_id',",
        replace="    unique_key='account_id',",
    ),
    Mutation(
        id="column_removed_with_consumers",
        kind=Kind.DEFECT,
        expects_family="F6",
        description="currency_code dropped from fct_revenue while downstream still selects it",
        relative_path=_MART_REVENUE,
        find="    currency_code,\n",
        replace="",
    ),
    Mutation(
        id="hardcoded_table_reference",
        kind=Kind.LATENT,
        expects_family="F6",
        description="ref() replaced by a literal table name, cutting the DAG edge",
        relative_path=_MART_REVENUE,
        find="from {{ ref('int_revenue_recognized') }}",
        replace='from "themis_demo"."main"."int_revenue_recognized"',
    ),
    Mutation(
        id="cartesian_join_introduced",
        kind=Kind.DEFECT,
        expects_family="F8",
        description="Join condition reduced to a tautology, pairing every row with every row",
        relative_path=_INT_CONVERTED,
        find=(
            "        on entries.currency_code = rates.currency_code\n"
            "        and entries.rate_period = rates.rate_date"
        ),
        replace="        on 1 = 1",
    ),
    Mutation(
        id="pii_column_exposed",
        kind=Kind.DEFECT,
        expects_family="F7",
        description="Counterparty email carried into a published mart",
        relative_path=_MART_REVENUE,
        find="    amount_txn_ccy,\n    amount_usd",
        replace="    amount_txn_ccy,\n    amount_usd,\n    customer_email",
    ),
    Mutation(
        id="approx_aggregate_in_regulatory",
        kind=Kind.LATENT,
        expects_family="F7",
        description=(
            "Exact contract count replaced by an approximation in a regulatory model — "
            "identical on small data, wrong at scale"
        ),
        relative_path=_MART_SUMMARY,
        find="count(distinct contract_id)     as contract_count",
        replace="approx_distinct(contract_id)    as contract_count",
    ),
    Mutation(
        id="grain_unprovable_on_regulatory",
        kind=Kind.LATENT,
        expects_family="F7",
        description=(
            "A join in the final select of a regulatory model, so its grain can no "
            "longer be derived and no fan-out check on it means anything"
        ),
        relative_path=_MART_SUMMARY,
        find="select * from aggregated",
        replace=(
            "select aggregated.*\n"
            "from aggregated\n"
            "left join {{ ref('dim_accounts') }} as accounts\n"
            "    on accounts.entity_code = aggregated.entity_code"
        ),
    ),
    Mutation(
        id="contract_column_dropped",
        kind=Kind.DEFECT,
        expects_family="F6",
        description="A column promised by an enforced contract is no longer produced",
        relative_path=_CONTRACT_MART,
        find="    entity_code,\n    reference_code",
        replace="    entity_code",
    ),
    Mutation(
        id="select_star_introduced",
        kind=Kind.LATENT,
        expects_family="F6",
        description=(
            "An explicit column list replaced by a star, so the schema now follows "
            "whatever upstream emits"
        ),
        relative_path=_MART_REVENUE,
        find="select\n    entry_id,",
        replace="select\n    *,\n    entry_id as entry_id_alias,\n    entry_id,",
    ),
    Mutation(
        id="cross_catalog_join_introduced",
        kind=Kind.LATENT,
        expects_family="F8",
        description=(
            "A join across two catalogs, which Trino cannot push down — both sides cross the wire"
        ),
        relative_path=_MART_SUMMARY,
        find="select * from aggregated",
        replace=(
            "select aggregated.*\n"
            "from aggregated\n"
            "left join {{ ref('stg_entity_reference') }} as ref\n"
            "    on ref.entity_code = aggregated.entity_code"
        ),
    ),
    Mutation(
        id="unordered_limit_introduced",
        kind=Kind.LATENT,
        expects_family="F8",
        description="A LIMIT with no ORDER BY, so which rows survive is undefined",
        relative_path=_DIM_ENTITIES,
        find="from {{ ref('stg_accounts') }}",
        replace="from {{ ref('stg_accounts') }}\nlimit 100",
    ),
    Mutation(
        id="partition_pruning_lost",
        kind=Kind.LATENT,
        expects_family="F8",
        description=(
            "A date filter wrapped in a function, so the engine can no longer prune "
            "partitions and scans everything"
        ),
        relative_path=_INCREMENTAL,
        find="    where posting_date >= (",
        replace="    where date_trunc('day', posting_date) >= (",
    ),
    Mutation(
        id="generated_sql_model_touched",
        kind=Kind.LATENT,
        expects_family="F6",
        description=(
            "A model whose SQL is built from query results is edited, so its diff "
            "cannot be trusted to reflect the change"
        ),
        relative_path=_DIM_ENTITIES,
        find="select distinct\n    entity_code,",
        replace="select distinct\n    upper(entity_code) as entity_code,",
    ),
    Mutation(
        id="unruled_fx_inverted",
        kind=Kind.UNRULED,
        expects_family="X",
        description=(
            "FX conversion inverted — dividing where the code multiplied. "
            "Arithmetically ordinary, structurally invisible, outside every rule"
        ),
        relative_path=_INT_CONVERTED,
        find="{{ money('entries.amount_txn_ccy * rates.rate') }}   as amount_usd",
        replace="{{ money('entries.amount_txn_ccy / rates.rate') }}   as amount_usd",
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


DEFECTS: tuple[Mutation, ...] = tuple(m for m in _ALL_INJECTED if m.kind is Kind.DEFECT)
LATENT: tuple[Mutation, ...] = tuple(m for m in _ALL_INJECTED if m.kind is Kind.LATENT)
UNRULED: tuple[Mutation, ...] = tuple(m for m in _ALL_INJECTED if m.kind is Kind.UNRULED)


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
        id="control_extract_final_cte",
        kind=Kind.CONTROL,
        expects_family="",
        description="Final select wrapped in a `final` CTE — a routine dbt house-style refactor",
        relative_path=_MART_SUMMARY,
        find="select * from aggregated",
        replace="""select * from (

    select * from aggregated

) as final""",
    ),
    Mutation(
        id="control_trailing_comment",
        kind=Kind.CONTROL,
        expects_family="",
        description="Trailing comment appended to a staging model",
        relative_path=_STG_ENTRIES,
        find="select * from typed",
        replace="select * from typed  -- one row per ledger entry",
    ),
)


ALL: tuple[Mutation, ...] = DEFECTS + LATENT + UNRULED + CONTROLS


def select(name: str) -> tuple[Mutation, ...]:
    """Resolve a mutation selector: 'all', 'defects', 'controls', or an id."""
    if name == "all":
        return ALL
    if name == "defects":
        return DEFECTS
    if name == "controls":
        return CONTROLS
    if name == "latent":
        return LATENT
    if name == "unruled":
        return UNRULED
    matched = tuple(m for m in ALL if m.id == name)
    if not matched:
        raise KeyError(f"unknown mutation {name!r}")
    return matched
