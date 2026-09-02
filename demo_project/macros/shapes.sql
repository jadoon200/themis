{#
  Structurally varied macros.

  These exist to exercise macro *shapes* rather than any particular macro. Real
  projects churn their macro bodies constantly, so THEMIS must not depend on
  recognising specific ones — it has to handle the forms dbt allows: nesting, loops,
  set blocks, relation arguments, and macros that emit whole clauses rather than
  expressions.
#}

{#- A macro that emits a full clause, not an expression. -#}
{% macro revenue_columns(prefix='') %}
    {{ prefix }}amount_txn_ccy,
    {{ prefix }}amount_usd
{% endmacro %}


{#- Loop + set: the output length depends on an argument. -#}
{% macro sum_by(columns, alias_prefix='total') %}
    {%- set parts = [] -%}
    {%- for column in columns -%}
        {%- do parts.append("sum(" ~ column ~ ") as " ~ alias_prefix ~ "_" ~ column) -%}
    {%- endfor -%}
    {{ parts | join(',\n    ') }}
{% endmacro %}


{#- Three levels of nesting: models reference only the outermost. -#}
{% macro rounded_money(expr, places=2) %}
    round({{ money(expr) }}, {{ places }})
{% endmacro %}

{% macro reporting_amount(expr) %}
    {{ rounded_money(expr, 2) }}
{% endmacro %}


{#- Takes a relation, which is how dbt_utils-style macros are usually written. -#}
{% macro latest_partition(relation, column='posting_date') %}
    (select coalesce(max({{ column }}), date '1900-01-01') from {{ relation }})
{% endmacro %}
