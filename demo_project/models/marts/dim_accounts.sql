{{ config(materialized='table') }}

-- Account dimension. One row per account_id.

select
    account_id,
    account_code,
    account_name,
    account_type,
    entity_code,
    is_intercompany
from {{ ref('stg_accounts') }}
