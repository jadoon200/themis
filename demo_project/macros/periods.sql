{#
  Period helpers.

  FX rates are published monthly and keyed on the first day of the month. Every
  as-of join must truncate the posting date the same way, or entries silently match
  the wrong rate -- or, worse, match every rate and multiply the row.
#}

{% macro period_start(date_expr) %}
    cast(date_trunc('month', {{ date_expr }}) as date)
{% endmacro %}


{#- Revenue accounts only; intercompany is excluded from external reporting. -#}
{% macro external_revenue_filter(account_type_col, intercompany_col) %}
    {{ account_type_col }} = 'revenue' and not {{ intercompany_col }}
{% endmacro %}
