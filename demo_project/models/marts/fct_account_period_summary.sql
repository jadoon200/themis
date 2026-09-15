{{ config(materialized='table') }}

-- Ledger activity per account and period: how many entries, and their net amount.

with entries as (

    select * from {{ ref('stg_gl_entries') }}

),

summary as (

    select
        account_id,
        period_month,
        count(*)            as entry_count,
        sum(amount_txn_ccy) as net_amount_txn_ccy
    from entries
    group by
        account_id,
        period_month

)

select * from summary
