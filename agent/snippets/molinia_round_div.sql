{#-
  molinia_round_div(num, den, scale=2, div0=false)

  Exact ROUND(num / den, scale) with Snowflake NUMBER semantics (round half away
  from zero), computed in 128-bit integers so no DOUBLE is ever involved.
  DuckDB turns every "/" on DECIMAL or INTEGER operands into DOUBLE, and DOUBLE
  rounding at exact half cents disagrees with Snowflake (MIGRATION_RULES.md N2-N7).

  num, den : DECIMAL or integer SQL expressions (strings), at most 10 decimal
             places each; |value| < 1e18 for scale <= 8, < 1e14 for scale 12.
  scale    : decimal places of the result (0-12). Returns DECIMAL(38, scale).
  div0     : true reproduces Snowflake DIV0 (den = 0 -> 0); false -> NULL.
  To mirror a Snowflake division, pass Snowflake's quotient scale
  max(s_num, min(s_num + 6, 12)) and keep any outer ROUND(..., n) as is.
-#}
{% macro molinia_round_div(num, den, scale=2, div0=false) -%}
{%- set n = "cast(cast((" ~ num ~ ") as decimal(38,10)) * 10000000000 as hugeint)" -%}
{%- set d = "cast(cast((" ~ den ~ ") as decimal(38,10)) * 10000000000 as hugeint)" -%}
(case when ({{ den }}) = 0 then {{ "0" if div0 else "null" }} else
  cast(sign({{ n }}) * sign({{ d }})
       * ((2 * abs({{ n }}) * {{ 10 ** scale }} + abs({{ d }})) // (2 * abs({{ d }})))
       as decimal(38,0)){% if scale > 0 %} * {{ "0." ~ ("0" * (scale - 1)) ~ "1" }}{% endif %}
end)
{%- endmacro %}
