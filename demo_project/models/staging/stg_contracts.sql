{{ config(materialized='view') }}

-- Customer contracts and their revenue recognition treatment. One row per contract_id.

select
    contract_id,
    customer_id,
    cast(contract_start as date) as contract_start,
    cast(contract_end as date)   as contract_end,
    recognition_method,
    term_months,
    -- Counterparty contact detail. Deliberately not carried into any mart.
    customer_email
from {{ ref('raw_contracts') }}
