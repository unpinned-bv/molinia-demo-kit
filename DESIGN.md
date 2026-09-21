# Molinia partner demo kit — design contract

Every file in this kit follows this contract. If you need to deviate, change this
file in the same edit and say why.

## The method in one paragraph

A coding agent (Claude Code) ports a Snowflake dbt project (`acme_shop/`) to
Molinia, builds it on the
`molinia` target with the kit's only dbt entry point (`tools/dbt_molinia.py run`,
then `test`), and proves with `agent/parity.py` that every model's
output equals what Snowflake produced (Snowflake's outputs were unloaded to Parquet,
moved to EU object storage, and ingested into Molinia as an answer key). Governance
holds throughout: the agent works with a least-privilege service-account key, every
call is audited, and a PII table stays masked for agent keys.

## Environments

### Snowflake (source)
- Account identifier and user from `.secrets/snowflake.env`; key-pair auth with the
  unencrypted PKCS#8 key at `.secrets/snowflake_rsa_key.p8` (the user registers the
  public key on their user themselves; never run `ALTER USER` from code).
- Role `SYSADMIN` (fallback `ACCOUNTADMIN` only if SYSADMIN lacks a grant).
- Warehouse `DEMO_WH`: XSMALL, `AUTO_SUSPEND = 60`, `AUTO_RESUME = TRUE`,
  `INITIALLY_SUSPENDED = TRUE`. Trial credits are real money: keep runs short.
- Database `MOLINIA_DEMO`; raw schema `RAW`; dbt target schema `ANALYTICS`, so
  dbt's default `generate_schema_name` produces `ANALYTICS_STAGING`,
  `ANALYTICS_INTERMEDIATE`, `ANALYTICS_MARTS`.
- Internal stage `MOLINIA_DEMO.PUBLIC.MOLINIA_EXPORT` (Parquet unload target).
- Non-secret connection settings live in `.secrets/snowflake.env`
  (`SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE`,
  `SNOWFLAKE_DATABASE`, `SNOWFLAKE_PRIVATE_KEY_PATH`). Tools load it; dbt reads the
  same variables through `env_var()`.

### Molinia (target)
- API base from `MOLINIA_API_URL` (all routes under `/api`; the apex
  `molinia.eu` is the marketing site — never use it as an API host).
- Org public id from `MOLINIA_ORG_ID`. A free-plan org has the rate limits
  below.
- **Rate limits (verified in code):** 60 HTTP requests/min per client IP for the
  whole API, and 30 org-engine queries/min for a free-plan org. Every tool and the
  dbt adapter must pace itself and back off on HTTP 429.
- Object storage: an S3-compatible bucket (`MINIO_ENDPOINT`, `MINIO_BUCKET` in
  `.secrets/minio.env`), registered in Molinia as a storage connection with a data
  source and a storage location (`molinia.data_source_id` and
  `molinia.location_id` in `engagement.yml`; location path `""` = bucket root).
  Files travel Snowflake stage → local `exports/` → bucket, and `exports/` leaves
  the kit once uploaded (rule H11): ingest plans from the bucket listing.
- Export layout in the bucket (and mirrored locally under `exports/`):
  - `acme-snowflake-export/raw/<table>.parquet` — one file per raw table
  - `acme-snowflake-export/expected/<model>.parquet` — one file per dbt model
- Ingest targets in Molinia (ingest always lands in schema `main`):
  - raw tables → `main.raw_<table>` (e.g. `main.raw_orders`)
  - answer key → `main.sf_<model>` (e.g. `main.sf_fct_orders`)
- dbt on Molinia: profile target `molinia`, `schema: analytics`, `threads: 1`,
  no `warehouse_id` (org engine path). Molinia's `generate_schema_name` uses a
  custom schema AS-IS, so models land in `staging`, `intermediate`, `marts`.
- Keys in `.secrets/molinia.env` (the user pastes them; tools never print them):
  - `MOLINIA_API_URL` — the API host, e.g. `https://app.molinia.eu`
  - `MOLINIA_ORG_ID` — the org's public id (`org_…`)
  - `MOLINIA_API_KEY` — service account `demo-dbt-build`, role `DEMO_DBT_BUILD`
    (api:query, api:ingest, api:namespace, api:history, table:select/insert/
    update/delete/create/drop). Used by dbt, ingest, parity.
  - `MOLINIA_READONLY_KEY` — service account `demo-agent-readonly`, built-in role
    `AGENT_READONLY`. Used only for the masked-read governance moment.
- Optional `.secrets/minio.env` (`MINIO_ENDPOINT`, `MINIO_BUCKET`,
  `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`) — if absent, the user uploads
  `exports/acme-snowflake-export/` through the MinIO console by hand.
- Masking (created by an admin in the console AFTER ingest): on
  `main.raw_customer_contacts`, `phone` = full, `iban` = partial. No RLS policies
  at all (an RLS policy on a missing table breaks every engine query in the org).
  No model may read `raw_customer_contacts` — service accounts can never be
  unmasked, so a model reading a masked column would write masked values.

## Raw data (Snowflake `MOLINIA_DEMO.RAW`)

Generated in Snowflake by `setup/snowflake/01_raw_data.sql`, fully deterministic:
ids from `ROW_NUMBER() OVER (ORDER BY SEQ4())` over `TABLE(GENERATOR(ROWCOUNT => n))`
(SEQ4 alone may have gaps), every attribute derived from the id with arithmetic
and `HASH(...)`. No `RANDOM`, `UUID_STRING`, `CURRENT_*`. Target total Parquet
size under 10 MB. Unquoted identifiers (Snowflake upper-cases them).

| Table | Rows (≈) | Columns (Snowflake types) | Notes |
|---|---|---|---|
| CUSTOMERS | 2,000 | CUSTOMER_ID NUMBER(38,0), FIRST_NAME VARCHAR, LAST_NAME VARCHAR, EMAIL VARCHAR, COUNTRY_CODE VARCHAR(2), SIGNUP_TS TIMESTAMP_NTZ, MARKETING_OPT_IN BOOLEAN | ~2% duplicate emails differing only in case/whitespace (later signup); some lowercase country codes; some NULL opt-in; some names with surrounding spaces |
| CUSTOMER_CONTACTS | 2,000 | CUSTOMER_ID, PHONE VARCHAR, IBAN VARCHAR, DATE_OF_BIRTH DATE | PII table; masked in Molinia; not used by any model |
| PRODUCTS | 150 | PRODUCT_ID, SKU VARCHAR, PRODUCT_NAME VARCHAR, CATEGORY_CODE VARCHAR(2) ('EL','HO','SP','BO','TO'), UNIT_PRICE NUMBER(10,2), IS_ACTIVE BOOLEAN | a few NULL UNIT_PRICE; some lowercase SKUs |
| ORDERS | 20,000 | ORDER_ID, CUSTOMER_ID, ORDER_TS TIMESTAMP_NTZ, STATUS VARCHAR, CHANNEL VARCHAR, ORDER_META VARIANT | STATUS mixed case of placed/shipped/delivered/cancelled/returned; ORDER_META = `{"coupon": str\|null, "device": "ios"\|"android"\|"desktop", "shipping": {"method": "standard"\|"express", "cost": "4.95"}, "gift": bool}` with some keys missing; timestamps within 2025-01-01..2026-06-30 |
| ORDER_ITEMS | ~55,000 | ORDER_ITEM_ID, ORDER_ID, PRODUCT_ID, QUANTITY NUMBER, UNIT_PRICE NUMBER(10,2), DISCOUNT_PCT NUMBER(5,2) | 1–5 items per order, except every 499th order (`MOD(ORDER_ID, 499) = 0`, 40 orders), which has no lines at all and is therefore never paid (downstream order totals must default to 0, not NULL); DISCOUNT_PCT in (0, 5, 10, 12.5, 15, 33.33); prices chosen so some exact line amounts land on half cents (the decimal-rounding trap) |
| PAYMENTS | ~22,000 | PAYMENT_ID, ORDER_ID, PAYMENT_METHOD VARCHAR, AMOUNT_CENTS NUMBER, STATUS VARCHAR ('success','failed','refunded'), PAID_TS TIMESTAMP_NTZ | some orders unpaid (every 20th, plus every order without lines), some with a failed then a successful payment |
| WEB_EVENTS | 15,000 | EVENT_ID, CUSTOMER_ID (nullable = anonymous), EVENT_TS TIMESTAMP_NTZ, EVENT_TYPE VARCHAR ('page_view','product_view','add_to_cart','checkout'), PAYLOAD VARIANT | PAYLOAD = `{"session_id": str, "items": [{"sku": str, "qty": int}, ...0-4], "utm": {"source": str, "campaign": str\|null}}`; the `utm` key is absent in ~20% of events (hash bucket < 2 of 10; `OBJECT_CONSTRUCT` drops a SQL-NULL value, so `payload:utm.source` reads NULL); page views carry either `"items": []` or no `items` key at all |

## The dbt project (`acme_shop/`, Snowflake dialect, as the client wrote it)

dbt ≥ 1.8, no packages, no seeds, no snapshots, no incremental, no ephemeral.
`dbt_project.yml`: `name: acme_shop`, `profile: acme_shop`; project-wide
Snowflake-only configs `+transient: true`, `+copy_grants: true`,
`+query_tag: acme_dbt`; folders: `staging` (+schema staging, view),
`intermediate` (+schema intermediate, view), `marts` (+schema marts, table).
`models/sources.yml` declares source `raw` with `database: MOLINIA_DEMO`,
`schema: RAW` (the three-part naming the port must remove).

| Model | Key (unique) | Snowflake-isms it must contain (all verified NOT to run on DuckDB 1.5.5 as written) |
|---|---|---|
| stg_customers | customer_id | `NVL`, `IFF`, `TO_VARCHAR(ts, 'YYYY-MM')`, `DATEDIFF(day, …)` with an unquoted date part, `::TIMESTAMP_NTZ` or `::DATE` casts |
| stg_products | product_id | `DECODE(category_code, …)`, `ZEROIFNULL`, `::NUMBER(10,2)` |
| stg_orders | order_id | VARIANT paths `order_meta:coupon::string`, nested `order_meta:shipping.cost`, `TRY_TO_NUMBER(…, 10, 2)`, `DATEADD(day, 30, …)`, `DATE_TRUNC('month', …)` |
| stg_order_items | order_item_id | exact NUMBER money math: `ROUND(quantity * unit_price * (1 - discount_pct / 100), 2)` |
| stg_payments | payment_id | `DIV0(amount_cents, 100)` reached through the `cents_to_euro` macro (`acme_shop/macros/cents_to_euro.sql`; the model file holds only `{{ cents_to_euro('amount_cents') }}`, the compiled SQL holds `div0(…)::number(12, 2)`), `IFF`. Deviation from "DIV0 in the model": putting it in a macro makes the port rewrite a macro, not just model files, which is how real client projects hide dialect code |
| int_order_lines | order_item_id | joins; `RATIO_TO_REPORT((line_amount * 100)::FLOAT) OVER (PARTITION BY order_id)`, **unrounded** (deviation from "rounded": a NUMBER ratio is first rounded to an internal scale and exact 4-dp ties occur, so a rounded share makes parity flaky; float over whole cents sums exactly, so the share is stable run to run and matches a DuckDB DOUBLE port within the parity tolerance) |
| int_web_item_views | (event_id, item_position) | `LATERAL FLATTEN(input => payload:items)`, `f.index` (0-based), `f.value:sku::string`, `payload:utm.source::string` |
| dim_customers | customer_id | `QUALIFY ROW_NUMBER() OVER (PARTITION BY email ORDER BY signup_ts, customer_id) = 1`, `COUNT_IF`, `ZEROIFNULL`, `DATEDIFF(day, …)` |
| fct_orders | order_id | `{{ config(cluster_by=['order_date']) }}`, `DIV0`, `IFF`, `ZEROIFNULL`, `::NUMBER(12,2)` |
| rpt_monthly_revenue | (order_month, country_code) | `DIV0` average order value rounded to cents, `RATIO_TO_REPORT` percentage, `LISTAGG(DISTINCT channel, ',') WITHIN GROUP (ORDER BY channel)` |

Rules for model SQL: every window function has a total order (tie-breaker on the
key); no non-deterministic functions; every model's output must be identical on
every run. Tests in YAML: `unique` + `not_null` on each key (composite keys via a
singular test or a concatenated key column), `accepted_values` on order status,
`relationships` from fct_orders.customer_id to stg_customers.customer_id, plus one
singular test in `tests/`. The whole `dbt build` must pass on Snowflake.

## Parity (`agent/parity.py`, config `agent/parity.yml`)

For each model: Molinia relation `<schema>.<model>` vs answer key `main.sf_<model>`,
joined on the model's key columns (FULL OUTER JOIN). Report per model: rows in
Snowflake, rows in Molinia, keys only in Snowflake, keys only in Molinia, and per
non-key column the number of rows that differ. Numeric comparison uses a tolerance
of `1e-9 * greatest(1, |a|, |b|)` (so float noise passes but a one-cent difference
fails); everything else compares with `IS DISTINCT FROM` after casting both sides
to a common type; column names match case-insensitively (Parquet from Snowflake
has upper-case names). Queries return counts only — never rows — so the agent never
receives row data. One API call per model, paced under the rate limit. Exit code 0
only when every model matches. `--local-duckdb <file>` runs the same SQL against a
local DuckDB file for testing.

## Tool CLIs (CLAUDE.md and the Makefile reference exactly these)

Run from the kit root with `.venv/bin/python`; every tool loads `.secrets/*.env`.

- `tools/sf.py run <file.sql>` — execute a multi-statement SQL file on Snowflake.
- `tools/sf.py unload` — run `setup/snowflake/02_unload.sql` (raw tables + every
  model to the stage, Parquet, `HEADER = TRUE`, `SINGLE = TRUE`, `OVERWRITE = TRUE`)
  then `GET` everything into `exports/acme-snowflake-export/{raw,expected}/`.
- `tools/sf.py dbt -- <dbt args>` — run dbt in `acme_shop/` against the
  `snowflake` target with the env loaded.
- `tools/dbt_molinia.py <parse|ls|compile|build|run|test|retry> [dbt args]` —
  run dbt in `acme_shop/` with `--target molinia --profiles-dir .` and the
  Molinia key loaded (never Snowflake/MinIO settings or the read-only key).
  Refuses `--target`, `show`, `seed`, `snapshot`, `docs`, `run-operation`.
  Added in the agent-docs review: it is the agent's only
  dbt entry point, so no command the agent runs names `.secrets/`.
- `tools/minio_upload.py` — upload `exports/acme-snowflake-export/` to the bucket
  (needs `.secrets/minio.env`; otherwise prints the manual steps and exits 2).
- `tools/molinia.py ingest [--only raw|expected]` — ingest every exported file
  into `main.raw_*` / `main.sf_*` through data source 1.
- `tools/molinia.py reset` — drop Molinia schemas `staging`, `intermediate`,
  `marts` (the agent's output) so the demo can run again; never touches `main`.
- `tools/molinia.py status` — table list with row counts.
- `tools/molinia.py query "<sql>" [--readonly]` — one statement, prints a table.
- `agent/parity.py [--model <name> ...] [--local-duckdb <file>]`.
- `agent/localcheck.py [--project <dir>] [--only <name> ...] [--types] [--values]
  [--list]` — free local dry-run: render every model with jinja2 and run it on
  local DuckDB 1.5.5 against synthetic `raw_*` tables that carry the real ingest
  types. No network, no keys, no data files; it reads only the project
  directory. It proves the SQL binds and what types it produces, never that the
  values equal Snowflake's — that is parity's job, and the tool says so in its
  own `--help`. `--only` reports on the names given and builds their `ref()`
  upstreams anyway (so a downstream model binds), and rejects a name that is
  neither a model nor a singular test, the way `parity.py --model` does: an
  unknown name must never exit 0 with nothing checked. Added after the 2026-09-19 rehearsal, whose own scratch version
  of it caught every binding error before a paid build.
- `Makefile` wraps these: `sf-setup sf-raw sf-build sf-unload minio-upload
  molinia-ingest molinia-reset molinia-status parity demo-reset localcheck`,
  plus `test` (the local unit tests in `agent/tests/`, no network) and `help`.

## Layout

```
molinia-demo-kit/
  README.md            what this is, setup, commands
  DESIGN.md            this contract
  CLAUDE.md            instructions for the live migration agent
  engagement.yml       every per-client value (project dir, stage, schemas, prefixes,
                       ids, forbidden tables, service accounts); tools read nothing else
  Makefile             `make help` lists every target
  requirements.txt     Python 3.12 dependencies (dbt-molinia separately, see README)
  .secrets/            keys and env files; only the *.env.example templates are tracked
  .venv/               not tracked: the Python environment
  fixtures/acme_shop/  the client's Snowflake dbt project, pristine; `make setup`
                       copies it to acme_shop/ and makes that its own git repo
  acme_shop/           not tracked: the working copy the agent migrates on a branch
  setup/snowflake/     00_setup.sql, 01_raw_data.sql, 02_unload.sql
  tools/               sf.py, minio_upload.py, molinia.py, dbt_molinia.py,
                       engagement.py (show/check), _engagement.py (the loader)
  partner/             assess.py + assesslib/: scope a client estate before quoting
  agent/               MIGRATION_RULES.md, PROMPT.md, parity.py, parity.yml,
                       localcheck.py, snippets/, tests/
  agent/snippets/      files the rulebook tells the agent to copy into acme_shop/
                       (molinia_round_div.sql). The rulebook prints the same
                       bytes for reading; a unit test fails if they drift apart,
                       so nothing is ever extracted from Markdown by hand.
  exports/             not tracked: unloaded Parquet (unmasked PII + every answer-key
                       row). Moved out of the kit once uploaded, before any agent run
                       (rule H11); ingest plans from the bucket listing, not from here
```

The `.gitignore` is an allowlist: a new top-level file stays untracked until it is
added there on purpose.
