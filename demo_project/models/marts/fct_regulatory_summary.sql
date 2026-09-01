{{ config(
    materialized='table',
    tags=['regulatory', 'recon']
) }}

-- Period revenue by entity, as reported externally.
--
-- Figures here reach a regulatory submission, so they must be exact -- no approximate
-- aggregates -- and the grain must hold: (period_month, entity_code). A fan-out
-- anywhere upstream lands here as an overstatement that nothing in the output flags.

with revenue as (

    select * from {{ ref('fct_revenue') }}

),

aggregated as (

    select
        period_month,
        entity_code,
        currency_code,
        count(*)                        as entry_count,
        count(distinct contract_id)     as contract_count,
        sum(amount_txn_ccy)             as revenue_txn_ccy,
        sum(amount_usd)                 as revenue_usd
    from revenue
    group by
        period_month,
        entity_code,
        currency_code

)

select * from aggregated
