{#
  A macro that builds SQL from the rows of a query.

  This shape exists in real projects: a lookup table drives a generated CASE expression,
  so the compiled SQL is a function of the data rather than only of the code. It is here
  so THEMIS keeps being exercised against it — two compilations of identical code differ
  whenever that table moves, and a structural diff would attribute the difference to
  whoever happened to open the pull request.
#}

{% macro entity_label_case(column) %}
    {%- set rows = run_query(
        "select entity_code, account_name from " ~ ref('stg_accounts') ~ " group by 1, 2"
    ) -%}
    {%- set ns = namespace(result='') -%}
    {%- if execute -%}
        {%- for row in rows.rows -%}
            {%- set ns.result = ns.result ~ "when " ~ column ~ " = '" ~ row[0] ~ "' then '" ~ row[0] ~ "' " -%}
        {%- endfor -%}
    {%- endif -%}
    case {{ ns.result }} else 'UNKNOWN' end
{% endmacro %}
