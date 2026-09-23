{{ config(
    materialized='table',
    database=('reference' if target.type == 'duckdb' else 'iceberg'),
    schema='main'
) }}

-- Entity reference data.
--
-- Kept in Iceberg, where slowly-changing reference data is headed at work, while the
-- marts are Hive. A mart that joins it therefore joins across catalogs — the one join
-- Trino cannot push down, so both sides are read in full. On DuckDB it lands in a second
-- attached database, which is the nearest thing that engine has.

select
    entity_code,
    entity_code || '-REF' as reference_code
from {{ ref('stg_accounts') }}
group by entity_code
