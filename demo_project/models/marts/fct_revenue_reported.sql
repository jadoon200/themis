{{ config(materialized='table', tags=['regulatory']) }}

-- Reported revenue, rounded for external presentation.
--
-- Deliberately built from the varied macro shapes: a macro that emits a column list,
-- one that loops to build aggregates, and one nested three deep. A change to any of
-- them reaches this model, and none of them is named after its file.

with revenue as (

    select * from {{ ref('fct_revenue') }}

),

reported as (

    select
        period_month,
        entity_code,
        {{ sum_by(['amount_usd']) }},
        {{ reporting_amount('sum(amount_txn_ccy)') }} as reported_txn_ccy
    from revenue
    group by
        period_month,
        entity_code

)

select * from reported
