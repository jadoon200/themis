{{ config(materialized='view') }}

-- General ledger entries, typed and signed. One row per entry_id.

with source as (

    select * from {{ ref('raw_gl_entries') }}

),

typed as (

    select
        entry_id,
        account_id,
        cast(posting_date as date)                     as posting_date,
        cast(period_month as date)                     as period_month,
        currency_code,
        amount_minor,
        entry_type,
        contract_id,
        is_reversal,
        {{ signed_amount('amount_minor', 'entry_type') }} as amount_txn_ccy,
        {{ period_start('cast(posting_date as date)') }}  as rate_period
    from source

)

select * from typed
