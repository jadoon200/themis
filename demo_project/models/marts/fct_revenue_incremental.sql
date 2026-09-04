{{ config(
    materialized='incremental',
    unique_key='entry_id',
    incremental_strategy='delete+insert',
    on_schema_change='fail',
    properties={'partitioned_by': "ARRAY['period_month']"},
    pre_hook="{{ partition_overwrite_hook() }}",
    tags=['recon']
) }}

-- Recognised revenue, built incrementally.
--
-- The lookback window is deliberate: entries are frequently posted a few days after
-- their value date, so a window that only picks up rows newer than the current maximum
-- would silently miss every late arrival. Narrowing it loses data with no error.

with source as (

    select * from {{ ref('int_revenue_recognized') }}

    {% if is_incremental() %}
    where posting_date >= (
        select coalesce(max(posting_date), date '1900-01-01') - interval '3' day
        from {{ this }}
    )
    {% endif %}

)

select
    entry_id,
    account_id,
    contract_id,
    posting_date,
    period_month,
    entity_code,
    currency_code,
    amount_txn_ccy,
    amount_usd
from source
