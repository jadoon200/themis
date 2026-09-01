{{ config(materialized='view') }}

-- Ledger entries converted to USD at the rate effective for their posting period.
--
-- The join to stg_fx_rates carries BOTH the currency and the period. stg_fx_rates has
-- one row per currency per month, so dropping the rate_period predicate would match
-- every month's rate and multiply every entry -- inflating revenue silently, since
-- nothing about the result looks malformed.

with entries as (

    select * from {{ ref('stg_gl_entries') }}

),

rates as (

    select * from {{ ref('stg_fx_rates') }}

),

converted as (

    select
        entries.entry_id,
        entries.account_id,
        entries.posting_date,
        entries.period_month,
        entries.currency_code,
        entries.entry_type,
        entries.contract_id,
        entries.is_reversal,
        entries.amount_txn_ccy,
        rates.rate                                          as fx_rate,
        {{ money('entries.amount_txn_ccy * rates.rate') }}   as amount_usd
    from entries
    inner join rates
        on entries.currency_code = rates.currency_code
        and entries.rate_period = rates.rate_date

)

select * from converted
