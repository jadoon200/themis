{{ config(materialized='table') }}

-- Entity dimension published under an enforced contract.
--
-- The contract is a promise to consumers about the columns this model emits, so
-- dropping one is a breaking change for them regardless of whether the SQL still runs.

select
    entity_code,
    reference_code
from {{ ref('stg_entity_reference') }}
