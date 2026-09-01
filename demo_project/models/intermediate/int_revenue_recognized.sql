{{ config(materialized='view') }}

-- External revenue entries with their contract treatment attached.
--
-- Reversals are retained deliberately: they carry negative amounts and netting them
-- out here would overstate gross revenue downstream.

with converted as (

    select * from {{ ref('int_gl_entries_converted') }}

),

accounts as (

    select * from {{ ref('stg_accounts') }}

),

contracts as (

    select * from {{ ref('stg_contracts') }}

),

joined as (

    select
        converted.entry_id,
        converted.account_id,
        converted.posting_date,
        converted.period_month,
        converted.currency_code,
        converted.contract_id,
        converted.is_reversal,
        converted.amount_txn_ccy,
        converted.amount_usd,
        accounts.account_code,
        accounts.account_name,
        accounts.account_type,
        accounts.entity_code,
        contracts.customer_id,
        coalesce(contracts.recognition_method, 'point_in_time') as recognition_method,
        contracts.term_months
    from converted
    inner join accounts
        on converted.account_id = accounts.account_id
    left join contracts
        on converted.contract_id = contracts.contract_id
    where {{ external_revenue_filter('accounts.account_type', 'accounts.is_intercompany') }}

)

select * from joined
