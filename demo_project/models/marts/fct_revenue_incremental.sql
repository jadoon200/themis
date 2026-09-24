{#
    The shape most tables at work are written in: Hive, partitioned by period, loaded
    incrementally by overwriting whole partitions.

    Hive refuses to delete individual rows ("only supported for transactional tables"),
    so `delete+insert` and `merge` cannot work here. They fail on the *second* run — the
    first is a plain CREATE TABLE AS and succeeds — which is exactly how such a model gets
    merged and breaks the next day. `append` with the partition-overwrite session setting
    is the working idiom: every partition the query selects replaces the one in the table.

    Two rules follow from that, and both are defects when broken:
    - the partition column comes last; Hive requires partition keys at the end;
    - the incremental filter selects whole partitions, never part of one. Selecting only
      the last few days of a month and overwriting that month deletes the rest of it.

    DuckDB ignores `partitioned_by` and has no partitions to overwrite; the hook emits a
    no-op there. It builds on DuckDB and is only measured on Trino.
#}
{{ config(
    materialized='incremental',
    incremental_strategy='append',
    on_schema_change='fail',
    properties={'partitioned_by': "ARRAY['period_month']"},
    pre_hook="{{ partition_overwrite_hook() }}",
    tags=['recon']
) }}

-- Recognised revenue, built incrementally.
--
-- The lookback is deliberate: entries are frequently posted after their period closed,
-- so reprocessing only the latest period would never pick up a late entry for the one
-- before it. It is expressed in periods because a period is what gets overwritten.

with source as (

    select * from {{ ref('int_revenue_recognized') }}

    {% if is_incremental() %}
    where period_month >= (
        select coalesce(max(period_month), date '1900-01-01') - interval '1' month
        from {{ this }}
    )
    {% endif %}

)

select
    entry_id,
    account_id,
    contract_id,
    posting_date,
    entity_code,
    currency_code,
    amount_txn_ccy,
    amount_usd,
    period_month
from source
