# Acme Shop analytics

dbt project behind the Acme Shop finance and marketing dashboards. It turns the
nightly extracts of the web shop (orders, payments, catalogue, clickstream) into
a small star schema on Snowflake.

## Running it

Connection settings come from environment variables (see `profiles.yml`); we
authenticate with a key pair, so no password is involved.

```bash
export SNOWFLAKE_ACCOUNT=...            # e.g. abcdefg-xy12345
export SNOWFLAKE_USER=...
export SNOWFLAKE_ROLE=SYSADMIN
export SNOWFLAKE_WAREHOUSE=DEMO_WH
export SNOWFLAKE_DATABASE=MOLINIA_DEMO
export SNOWFLAKE_PRIVATE_KEY_PATH=~/.ssh/snowflake_rsa_key.p8

dbt debug --profiles-dir .
dbt build --profiles-dir . --target snowflake
```

`dbt build` runs every model and every test (source tests included). Models land
in `ANALYTICS_STAGING`, `ANALYTICS_INTERMEDIATE` and `ANALYTICS_MARTS`.

## Layout

| Layer | Materialisation | Models |
|---|---|---|
| `staging/` | view | `stg_customers`, `stg_products`, `stg_orders`, `stg_order_items`, `stg_payments`: one per source table, renamed, typed, cleaned |
| `intermediate/` | view | `int_order_lines` (lines with order and product context), `int_web_item_views` (one row per item in a clickstream event) |
| `marts/` | table | `fct_orders`, `dim_customers`, `rpt_monthly_revenue` |

Sources are declared in `models/sources.yml` (`MOLINIA_DEMO.RAW`).
`CUSTOMER_CONTACTS` holds PII and is deliberately not modelled.

## Conventions

- **Money is exact.** Amounts stay `NUMBER` end to end; an order line is rounded
  to the cent once, half away from zero, exactly as the invoice does it. Cents
  from the payment provider go through the `cents_to_euro` macro.
- **Ratios are floats.** Shares (`share_of_order`, `revenue_share_pct`) are
  computed in `FLOAT`; nobody reconciles against them.
- **Reproducible.** No `CURRENT_DATE`: age and recency metrics use the
  `as_of_date` variable in `dbt_project.yml`. Every window function has a
  unique tie-breaker, so a rebuild of the same data gives the same rows.
- **Duplicate accounts.** People sometimes sign up twice with the same e-mail
  address in a different case. `stg_customers` keeps every account;
  `dim_customers` keeps the earliest one and credits it with the orders and web
  activity of the later ones.
- `fct_orders.customer_id` is the account that placed the order, so it is
  tested against `stg_customers`, not `dim_customers`.

## Tests

Keys are `unique` + `not_null`; statuses and codes have `accepted_values`.
Two singular tests live in `tests/`: order totals reconcile with their lines,
and `rpt_monthly_revenue` has one row per month and country.
