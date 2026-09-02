{{ config(materialized='table') }}

-- Entity labels, built through a macro that queries at compile time.
--
-- Present so the generated-SQL case stays exercised: this model's compiled SQL is a
-- function of stg_accounts' contents, so it changes when that data changes even if
-- nobody edits this file.

select distinct
    entity_code,
    {{ entity_label_case('entity_code') }} as entity_label
from {{ ref('stg_accounts') }}
