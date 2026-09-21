{#
    Convert an integer amount in cents to euros with two decimals.

    DIV0 returns 0 instead of failing when the divisor is 0; the cast keeps
    the result an exact NUMBER so sums of converted amounts stay exact.

    Usage: {{ cents_to_euro('amount_cents') }}
#}
{% macro cents_to_euro(column_name, precision=12) -%}
    (div0({{ column_name }}, 100)::number({{ precision }}, 2))
{%- endmacro %}
