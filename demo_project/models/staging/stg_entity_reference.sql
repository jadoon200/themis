{{ config(
    materialized='table',
    database=('reference' if target.type == 'duckdb' else target.database),
    schema='main'
) }}

-- Entity reference data.
--
-- On DuckDB this lands in a second attached catalog so a cross-catalog join is real and
-- measurable. Trino's memory connector is a single catalog, so there it sits alongside
-- everything else — the federated case is exercised on DuckDB, and the dialect case on
-- Trino.

select
    entity_code,
    entity_code || '-REF' as reference_code
from {{ ref('stg_accounts') }}
group by entity_code
