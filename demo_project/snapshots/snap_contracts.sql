{% snapshot snap_contracts %}
{{ config(
    database=('iceberg' if target.type == 'trino' else target.database),
    schema='history',
    unique_key='contract_id',
    strategy='timestamp',
    updated_at='updated_at',
    hard_deletes='invalidate'
) }}

-- Contract terms as they stood at each point in time: one row per contract per version.
--
-- In Iceberg, because a snapshot is written with MERGE and Hive cannot merge. This is the
-- shape slowly-changing data takes at work, where Dagster runs the snapshots.

select
    contract_id,
    customer_id,
    contract_start,
    contract_end,
    recognition_method,
    term_months,
    updated_at
from {{ ref('stg_contracts') }}

{% endsnapshot %}
