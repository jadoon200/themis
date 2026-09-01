{{ config(materialized='table') }}

-- Recognised external revenue, one row per entry.
--
-- Grain: entry_id. Nothing in the project asserts that, which is the normal state of
-- affairs here -- THEMIS derives it from the structure instead.

select
    entry_id,
    account_id,
    contract_id,
    customer_id,
    posting_date,
    period_month,
    entity_code,
    currency_code,
    account_code,
    account_name,
    recognition_method,
    is_reversal,
    amount_txn_ccy,
    amount_usd
from {{ ref('int_revenue_recognized') }}
