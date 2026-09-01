{#
  Monetary casting helpers.

  Money is DECIMAL, never DOUBLE. Binary floating point drifts cents across large
  SUMs, and in a ledger that difference is a reconciliation break rather than a
  rounding curiosity. These macros exist so the scale is declared in exactly one
  place -- which also means a change here is a change to every model that uses them.
#}

{% macro money(expr) %}
    cast({{ expr }} as decimal(38, 6))
{% endmacro %}


{#- Ledger amounts are stored in minor units (cents) to keep them integral. -#}
{% macro minor_to_major(expr) %}
    cast({{ expr }} as decimal(38, 6)) / cast(100 as decimal(38, 6))
{% endmacro %}


{#-
  Signed amount: debits reduce revenue, credits increase it. Getting this backwards
  flips the sign of a whole mart, so it lives in one macro rather than being retyped
  per model.
-#}
{% macro signed_amount(amount_expr, entry_type_expr) %}
    case
        when {{ entry_type_expr }} = 'credit' then {{ minor_to_major(amount_expr) }}
        else -1 * {{ minor_to_major(amount_expr) }}
    end
{% endmacro %}
