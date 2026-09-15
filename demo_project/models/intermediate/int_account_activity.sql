{{ config(materialized='view') }}

-- Posted and reversed amounts per account and period, kept as separate rows.
--
-- A reversal is shown beside the postings it reverses rather than netted into them, so
-- the ledger stays readable. That makes this model one row per account, period *and
-- activity type*: the two halves of the UNION ALL share an account and a period whenever
-- an account has both postings and reversals in the same month.

with entries as (

    select * from {{ ref('stg_gl_entries') }}

)

select
    account_id,
    period_month,
    'posted'            as activity_type,
    sum(amount_txn_ccy) as amount_txn_ccy
from entries
where not is_reversal
group by
    account_id,
    period_month

union all

select
    account_id,
    period_month,
    'reversed'          as activity_type,
    sum(amount_txn_ccy) as amount_txn_ccy
from entries
where is_reversal
group by
    account_id,
    period_month
