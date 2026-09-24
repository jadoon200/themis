{{ config(materialized='table') }}

-- Contract terms in force now. One row per contract_id: the snapshot keeps every version
-- of a contract, and only the current one has no dbt_valid_to.

select
    contract_id,
    customer_id,
    recognition_method,
    term_months,
    -- Iceberg keeps microseconds and this Hive table can hold milliseconds, so the
    -- snapshot's timestamp has to be narrowed on the way across.
    cast(dbt_valid_from as timestamp(3)) as terms_since
from {{ ref('snap_contracts') }}
where dbt_valid_to is null
