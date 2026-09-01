{{ config(materialized='view') }}

-- Monthly FX rates to USD. Grain is (currency_code, rate_date) -- there are several
-- rows per currency, one per month, which is exactly why every join to this model
-- must carry the date predicate as well as the currency.

with source as (

    select * from {{ ref('raw_fx_rates') }}

)

select
    currency_code,
    cast(rate_date as date) as rate_date,
    {{ money('rate') }}     as rate,
    rate_source
from source
