{% snapshot snap_accounts %}
{{ config(
    database=('iceberg' if target.type == 'trino' else target.database),
    schema='history',
    unique_key='account_id',
    strategy='check',
    check_cols=['account_name', 'account_type', 'entity_code', 'is_intercompany'],
    hard_deletes='invalidate'
) }}

-- The chart of accounts as it stood at each point in time. The source has no
-- last-modified time, so a new version is written whenever a checked column differs.

select
    account_id,
    account_code,
    account_name,
    account_type,
    entity_code,
    is_intercompany
from {{ ref('stg_accounts') }}

{% endsnapshot %}
