{% snapshot snap_fx_rates %}
{{ config(
    target_database=('iceberg' if target.type == 'trino' else target.database),
    target_schema='snapshots',
    unique_key="currency_code || '|' || cast(rate_date as varchar)",
    strategy='check',
    check_cols=['rate'],
    invalidate_hard_deletes=True
) }}

-- FX rates as they were published, in the spelling most existing projects still use: a
-- fixed target_schema, a key concatenated from its columns, and the boolean that came
-- before hard_deletes. A fixed target_schema puts the snapshot in the same table whatever
-- the target says, so a review builds around it and reads it where it is.

select
    currency_code,
    rate_date,
    rate,
    rate_source
from {{ ref('stg_fx_rates') }}

{% endsnapshot %}
