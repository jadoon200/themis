{{ config(materialized='table') }}

-- The FX rates in force now, from the rate snapshot. One row per currency and month.

select
    currency_code,
    rate_date,
    rate
from {{ ref('snap_fx_rates') }}
where dbt_valid_to is null
