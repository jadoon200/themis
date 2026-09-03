{{ config(materialized='table', database='reference', schema='main') }}

-- Entity reference data, materialised into a second catalog.
--
-- Its purpose is to make a cross-catalog join real: in Trino such a join cannot be
-- pushed down, so both sides are read in full and joined on the coordinator.

select
    entity_code,
    entity_code || '-REF' as reference_code
from {{ ref('stg_accounts') }}
group by entity_code
