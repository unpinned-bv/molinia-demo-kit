# Snowflake → Molinia migration rules (dbt)

The rulebook a migration agent follows, and that the delivery team owns and
grows: every time parity teaches you something, add a rule.

- **Engine.** Molinia runs DuckDB 1.5.5. Every Molinia-side rewrite below was
  executed on local DuckDB 1.5.5 (the kit venv) on 2026-09-19. Appendix A has
  the snippet → result log. Appendix B has the money-math proof, and Appendix C
  has a dbt-molinia run against a local fake endpoint.
- **Snowflake side.** Snowflake behaviour quoted here comes from Snowflake's
  documentation. This rulebook was built without querying Snowflake. The final
  arbiter is always the answer key (`main.sf_<model>`) plus `agent/parity.py`.
- **Rule ids** (S2, N4, J1 …) are what commit messages and review comments cite.

## 0. How to use this file

- **Agent:** read sections 1 to 9 before touching code. Apply the *smallest*
  rewrite that makes a model run **and** match. Don't refactor, rename,
  reformat or "improve" anything. A senior reviews the diff, and every changed
  line costs review time.
- **Reviewer:** every changed line should map to a rule id. If it doesn't, ask why.
- **Growing the rulebook:** a new rule needs a rule id, the Snowflake form, the
  Molinia form, why it's needed, and a verification snippet in Appendix A.

## 1. Hard rules

| id | rule |
|---|---|
| H1 | **Never touch Snowflake.** No `tools/sf.py`, no `make sf-*`, no `--target snowflake`. The answer key is already in Molinia. |
| H2 | **`main.raw_customer_contacts` is off-limits.** No model, test, source reference or query reads it, not even `count(*)`. It is masked, and service accounts can never be unmasked, so a model reading it would silently persist masked values. |
| H3 | **Never print keys.** Don't `cat`, `grep`, `echo`, `env`, `printenv`, `set` or `source` anything from `.secrets/` or any `MOLINIA_*`/`SNOWFLAKE_*` value, and never name `.secrets/` in a command. The kit's tools load the key themselves: `tools/dbt_molinia.py` for dbt, `agent/parity.py` and `tools/molinia.py`. Never commit a key. |
| H4 | **One key.** dbt and parity use `MOLINIA_API_KEY` (service account `demo-dbt-build`). Never `MOLINIA_READONLY_KEY`, never `tools/molinia.py query --readonly`. |
| H5 | **Never set `warehouse_id`.** The demo runs on the org engine only. |
| H6 | **No hand-written DDL or DML.** dbt may create and replace relations in `staging`, `intermediate` and `marts` only. Never DROP/ALTER/CREATE/INSERT/UPDATE/DELETE yourself. Never touch schema `main` (`raw_*`, `sf_*`). Never run `tools/molinia.py ingest`, `empty` or `reset`, `make demo-reset` or `make molinia-*`. |
| H7 | **No governance changes.** Never create or alter RLS or masking policies, roles, grants or service accounts. An RLS policy on a missing table breaks every engine query in the org. |
| H8 | **Counts, not rows.** Diagnostic queries return aggregates only. No `SELECT *`, no `LIMIT n` row dumps, and no text min/max over data. Parity already returns counts only. |
| H9 | **Don't move the goalposts.** Never edit `agent/parity.py`, `agent/parity.yml` or the answer key. Never delete or loosen a dbt test, and never change a model's key, grain, column names or column meaning to make parity pass. |
| H10 | **Deterministic models only.** No `RANDOM`, `UUID_STRING`, `SAMPLE`, `CURRENT_*`/`SYSDATE`. Every window ordering is total, with a tie-breaker on the key. |
| H11 | **Read only what the job needs.** Read `acme_shop/`, this file, `agent/parity.yml` and your own `agent/scratch/`, nothing else. You may *run* the kit's tools without reading them: `tools/dbt_molinia.py`, `agent/parity.py`, `agent/localcheck.py`, `tools/molinia.py query`, and `cp agent/snippets/molinia_round_div.sql acme_shop/macros/`. Never read `exports/`, any `*.parquet` file, or other files inside or outside the kit. The unloaded Parquet holds unmasked PII and every answer-key row. Parity exists so that you never need rows, and checking parity against local files would prove nothing. |

## 2. Project-level changes (P)

**P1. Add the Molinia target.** Add this to `profiles.yml` under the existing profile, next to `snowflake`. Keep `target: snowflake` as the default. `tools/dbt_molinia.py` passes `--target molinia` for you:

```yaml
    molinia:
      type: molinia
      host: "{{ env_var('MOLINIA_API_URL') }}"
      org_id: "{{ env_var('MOLINIA_ORG_ID') }}"
      token: "{{ env_var('MOLINIA_API_KEY') }}"
      schema: analytics
      threads: 1
      requests_per_minute: 25
      retry_max_wait_seconds: 600
```

`threads: 1` is required by the rate limits (section 6). There is no `warehouse_id` (H5) and no `database`. `requests_per_minute` and `retry_max_wait_seconds` only take effect with the kit's pre-release adapter (section 5). The released adapter ignores them without an error, which is why the adapter check in `CLAUDE.md` looks for the field itself.

**P2. Remove Snowflake-only configs.** `+transient`, `+copy_grants`, `+query_tag`,
`cluster_by` (including `{{ config(cluster_by=...) }}`), `secure`,
`snowflake_warehouse`, `automatic_clustering`, `tmp_relation_type` and
`grants:`. dbt-molinia ignores unknown configs, so removing them is for
clarity, with one exception: `grants:` would make dbt issue grant statements,
so it must go. Access control on Molinia lives in the console (RBAC, masking),
not in dbt.

**P3. Point sources at `main`.** Ingest always lands in schema `main`, as `raw_<table>`:

```yaml
sources:
  - name: raw
    schema: main                    # was  database: MOLINIA_DEMO / schema: RAW
    tables:
      - name: orders
        identifier: raw_orders      # one identifier per table
```

Keep every source and table `name`, so no `source()` call changes. Remove
`database:`. Keep the descriptions and tests. A declared source costs nothing
until something selects from it (H2 still applies).

Only the table-level `- name:` takes an `identifier:`. Column entries under
`columns:` also start with `- name:`, so a blind search-and-replace produces
nonsense like `identifier: raw_customer_id` nested in a column list. Edit the
table entries by hand; there are seven.

**P4. Schema names.** dbt-molinia's `generate_schema_name` uses a custom schema
as-is: `+schema: staging` lands in `staging`. Snowflake's dbt default produced
`ANALYTICS_STAGING`. Don't add a `generate_schema_name` override, and remove
the project's own override if it prefixes `target.schema`. `agent/parity.yml`
expects `staging.*`, `intermediate.*` and `marts.*`.

**P5. Two-part names.** dbt-molinia renders `"schema"."name"`, and the org
catalog is implicit. Replace hard-coded Snowflake names
(`MOLINIA_DEMO.RAW.ORDERS`, `ANALYTICS_MARTS.FCT_ORDERS`) with `source()` or
`ref()`. Remove `database=` configs.

**P6. Materializations.** `view` and `table` work. `incremental` is
implemented as delete+insert through a persistent `_dbt_internal` schema, but
it is **unverified**: it has never run against Molinia or the local stub. Port
an incremental model as incremental, then prove it with parity after a full
refresh and again after a second run, and flag it to the reviewer. Seeds,
snapshots and Python models don't work (section 8).

**P7. Port macros too.** Client macros often hide Snowflake-isms, for example a
`DIV0` inside a cents helper. Port macro bodies with the same rules, keeping
the macro name and signature. When a model divides, copy in the kit's macro:
`cp agent/snippets/molinia_round_div.sql acme_shop/macros/` (section 4).

The cents helper needs N8 and N1 together, so here it is finished:

```sql
-- before:  (div0({{ column_name }}, 100)::number({{ precision }}, 2))
-- after:   (({{ column_name }} * 0.01)::decimal({{ precision }}, 2))
```

The divisor is the constant 100, so `DIV0`'s zero guard can never fire; drop
it rather than reaching for `molinia_round_div`.

**P8. Port tests too.** Port `tests/*.sql` and any SQL inside YAML (`where:`,
expression arguments). Generic tests (`unique`, `not_null`, `accepted_values`,
`relationships`) run unchanged.

**P9. Column contract.** Output columns keep their names (case-insensitive),
meaning and type family. Parity fails a model when a column exists on one side
only, and flags type mismatches. DuckDB keeps the source table's spelling of a
column name (`CUSTOMER_ID`) unless you alias it. That is fine because parity
is case-insensitive, so don't add aliases just to lower-case names.

**P10. One statement per model and per hook.** dbt-molinia sends one statement
per HTTP request, so multi-statement hooks fail.

## 3. SQL rewrites

### 3.1 Conditionals and NULLs (S)

| id | Snowflake | Molinia (DuckDB 1.5.5) | note |
|---|---|---|---|
| S1 | `IFF(c, a, b)` | `CASE WHEN c THEN a ELSE b END` | A NULL condition takes the ELSE branch in both. Write `IFF(x, TRUE, FALSE)` as the full CASE, not as bare `x`, so that NULL still becomes FALSE. DuckDB's `if()` exists, but prefer CASE because reviewers read it faster. |
| S2 | `NVL(a, b)`, `IFNULL(a, b)` | `COALESCE(a, b)` | |
| S2 | `ZEROIFNULL(x)` | `COALESCE(x, 0)` | The type widens (DECIMAL(10,2) becomes DECIMAL(12,2)), which is harmless. |
| S2 | `NULLIFZERO(x)` | `NULLIF(x, 0)` | |
| S2 | `NVL2(a, b, c)` | `CASE WHEN a IS NOT NULL THEN b ELSE c END` | |
| S2 | `EQUAL_NULL(a, b)` | `a IS NOT DISTINCT FROM b` | |
| S3 | `DECODE(e, s1, r1, …, d)` | `CASE e WHEN s1 THEN r1 … ELSE d END` | DECODE matches NULL to NULL and CASE doesn't. If a search value can be NULL, use `CASE WHEN e IS NOT DISTINCT FROM s1 THEN …`. DuckDB's own `decode()` converts BLOB to VARCHAR, so the Snowflake call fails with a binder error. |
| S4 | `GREATEST(a, b)`, `LEAST(a, b)` | the same, plus a NULL guard | Snowflake returns NULL if any argument is NULL, and DuckDB ignores NULLs. When an argument can be NULL, write `CASE WHEN a IS NULL OR b IS NULL THEN NULL ELSE greatest(a, b) END`. `GREATEST_IGNORE_NULLS` becomes plain `greatest`. |
| S5 | `CONCAT(a, b)`, `CONCAT_WS(sep, a, b)` | `a \|\| b` | Snowflake returns NULL if any argument is NULL, while DuckDB's `concat`/`concat_ws` skip NULLs. `\|\|` propagates NULL in both engines. |

### 3.2 Numbers and money (N)

**N1. Types.**
- `NUMBER`, `NUMBER(38,0)`, `INT`, `INTEGER` and `BIGINT` (all NUMBER(38,0) in Snowflake) become `DECIMAL(38,0)`, or `BIGINT` for ids and counts. Never use DuckDB `INT`/`INTEGER` for a Snowflake INT: it is 32-bit, and `3000000000::int` fails.
- `NUMBER(p,s)` becomes `DECIMAL(p,s)`.
- Never write bare `DECIMAL`/`NUMERIC`: DuckDB defaults it to **DECIMAL(18,3)**.
- `FLOAT`, `DOUBLE` and `REAL` become `DOUBLE`.

**N2. `/` is never exact on DuckDB.** DECIMAL/DECIMAL, DECIMAL/INTEGER and
INTEGER/INTEGER all return **DOUBLE**. Snowflake NUMBER division returns an
exact NUMBER, rounded to scale `max(s_num, min(s_num + 6, 12))`. Division by
zero also differs: Snowflake raises an error, DuckDB `/` silently returns
`inf`, and `//` returns NULL.

**N3. `+ − ×` on DECIMAL stay exact DECIMAL.** Scales add on `×`, so keep money
in DECIMAL end to end. DuckDB raises an error if a product would need a scale
above 38.

**N4. The half-cent trap.** Rewrite `ROUND(q * p * (1 - d / 100), 2)` as
`ROUND(q * p * (1 - d * 0.01), 2)`. A division by a nonzero power-of-ten
constant becomes a multiplication by the decimal literal (`0.1`, `0.01`,
`0.001` are DECIMAL literals in DuckDB). Example: 10.20 × 0.875 = 8.925
exactly, which Snowflake rounds to **8.93**. The DOUBLE path computes
8.924999999999999 and gives **8.92**. Over 599,970 grid lines, 7,535 differ on
the DOUBLE path and 0 on the DECIMAL path (Appendix B).

**N5. ROUND.**
- `ROUND(DECIMAL, n)` rounds half away from zero, which is Snowflake's NUMBER behaviour (8.925 → 8.93, −8.925 → −8.93).
- `ROUND(DOUBLE, n)` multiplies by 10ⁿ in DOUBLE and then rounds half away. Values at or near a decimal tie are engine-dependent (N10): 1.005 → 1.0, but 2.675 → 2.68.
- `ROUND(x, n, 'HALF_TO_EVEN')` on a NUMBER is **not portable**, so report it (section 8). DuckDB's `round_even()` is not a substitute: on DECIMAL input it returns a DOUBLE and does not round half to even (1.005 → 1.01, −131.045 → −131.05).

**N6. Casts.** `CAST(DECIMAL AS DECIMAL(p,s))` rounds half away from zero, like
Snowflake `::NUMBER(p,s)`. `CAST(DOUBLE AS INTEGER)` rounds half to **even**
(2.5 → 2), while `CAST(DECIMAL AS INTEGER)` rounds half away (2.5 → 3). Never
let a DOUBLE reach an integer or money column.

**N7. Division by data** (averages, shares, `DIV0` of two columns) uses the
macro in section 4:
- `a / b` becomes `{{ molinia_round_div('a', 'b', S) }}`.
- `DIV0(a, b)` becomes `{{ molinia_round_div('a', 'b', S, div0=true) }}`.
- An outer `ROUND(…, n)` stays `round(…, n)`: it is a DECIMAL round, so it is exact.

`S` is Snowflake's quotient scale, `max(s_a, min(s_a + 6, 12))`. So a
numerator scale of 0 gives 6, 2 gives 8, 3 gives 9, 4 gives 10, and 6 or more
gives 12. Read `s_a` off the Snowflake types:

| numerator | scale |
|---|---|
| integer columns, COUNT, COUNT_IF | 0 |
| NUMBER(p,s) | s |
| `ROUND(x, n)`, `::NUMBER(p,n)` | n |
| `SUM(x)` | scale of x |
| `a + b`, `a − b` | max of the two |
| `a × b` | s_a + s_b, which Snowflake caps at max(s_a, s_b, 12) |

**N8. DIV0 by a constant.** `DIV0(amount_cents, 100)` becomes
`amount_cents * 0.01`. It is exact, and DIV0 can't trigger on a nonzero
constant.

**N9. AVG of money.** DuckDB `avg(DECIMAL)` returns DOUBLE. Rewrite
`ROUND(AVG(x), 2)` as `round({{ molinia_round_div('sum(x)', 'count(x)', S) }}, 2)`.
Snowflake's AVG result scale is not in this rulebook's verified set, so parity
decides.

**N10. FLOAT on purpose.** When the client casts to FLOAT (for example
`ratio_to_report(x::float)`), keep float semantics: use `::double` and the same
order of operations. Both engines compute IEEE doubles and can only disagree on
a value that sits exactly on a rounding tie. Parity decides (section 7, row
"±1 in last digit").

**N11. TRY_TO_NUMBER and friends.**
- `TRY_TO_NUMBER(s, p, sc)` and `TRY_TO_DECIMAL(s, p, sc)` become `TRY_CAST(s AS DECIMAL(p, sc))`.
- `TRY_TO_NUMBER(s)` becomes `TRY_CAST(s AS DECIMAL(38,0))`. This rounds '4.95' to 5, as Snowflake does at scale 0.
- `TO_NUMBER` becomes `CAST`, and `TRY_TO_DOUBLE` becomes `TRY_CAST(… AS DOUBLE)`.
- TRY_CAST trims spaces, turns '' into NULL and rounds extra decimals half away ('4.955' → 4.96).
- Formatted input ('1,234.5') becomes NULL, so strip the formatting with `replace()` first.

**N12. Aggregate result types.** Only counting constructs need a cast here.

| aggregate | DuckDB result type | cast? |
|---|---|---|
| `sum(DECIMAL(p,s))`, including `sum(DECIMAL(38,0))` | DECIMAL(38,s) | **no** |
| `count(*)`, `count(x)`, `count(*) FILTER (WHERE c)` | BIGINT | no |
| `count_if(c)` | **HUGEINT** | yes: `count_if(c)::bigint` |
| `sum(BIGINT)`, `sum(INTEGER)` | **HUGEINT** | yes, if it can reach an output column |
| `avg(DECIMAL)` | DOUBLE | see N9 |

A Snowflake `NUMBER(38,0)` id or counter arrives from ingest as
**DECIMAL(38,0)**, not as BIGINT (this is the ingest type, so check it with the
discovery query in section 9). So `sum(order_id)`, `sum(quantity)` and
`sum(amount_cents)` stay DECIMAL and need no cast, and adding one would change
the type family the answer key has. `sum(BIGINT)` only appears when an earlier
rewrite already produced a BIGINT column, for example a `count_if(...)::bigint`
that a later model sums.

### 3.3 Dates and times (D)

**D1. Types.** `TIMESTAMP_NTZ` becomes `TIMESTAMP`. `TIMESTAMP_LTZ`/`TIMESTAMP_TZ`
become `TIMESTAMPTZ`, which depends on a session time zone: avoid it and flag
it to the reviewer. Raw Parquet timestamps may arrive as `TIMESTAMP_NS`. Every
function here works on it, and the client's `::timestamp_ntz` becomes
`::timestamp`, which normalises it.

**D2. Formatting.** `TO_VARCHAR(x, fmt)` and `TO_CHAR(x, fmt)` become
`strftime(x, fmt)`:

| Snowflake | YYYY | YY | MM | MON | MMMM | DD | DY | HH24 | HH12 | AM/PM | MI | SS | FF3 | FF6 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| strftime | `%Y` | `%y` | `%m` | `%b` | `%B` | `%d` | `%a` | `%H` | `%I` | `%p` | `%M` | `%S` | `%g` | `%f` |

- **Timestamps without a format.** Snowflake's default TIMESTAMP_NTZ text is `2025-03-07 14:05:09.000`, while DuckDB's CAST gives `2025-03-07 14:05:09`. Always use `strftime(ts, '%Y-%m-%d %H:%M:%S.%g')`.
- **Numbers, booleans and dates.** `TO_VARCHAR(x)` becomes `CAST(x AS VARCHAR)`. DECIMAL keeps its scale (12.30 → '12.30'), DECIMAL(38,0) → '123', and true → 'true'.

**D3. DATEADD(part, n, x).**
- For a TIMESTAMP, write `x + INTERVAL (n) part`.
- For a DATE plus days, write `x + n`, which stays a DATE.
- For a DATE plus months or years, write `CAST(x + INTERVAL (n) MONTH AS DATE)`.
- `DATE + INTERVAL` returns a **TIMESTAMP** on DuckDB, while Snowflake returns a DATE.
- Month ends clamp like Snowflake: 2025-01-31 + 1 month = 2025-02-28.
- `TIMEADD` and `TIMESTAMPADD` follow the same rule.

**D4. DATEDIFF(part, a, b)** becomes `datediff('part', a, b)` with the part
**quoted**. An unquoted `day` is read as a column. It counts boundaries crossed
for day, month, year and hour (23:59 → 00:01 is 1 day), as Snowflake does.
- **Week is the exception.** DuckDB counts 7-day spans (Sunday → Monday = 0), while Snowflake counts week boundaries (= 1). Use `datediff('day', date_trunc('week', a), date_trunc('week', b)) // 7`.
- Never use `date_sub()`, which counts full intervals.

**D5. DATE_TRUNC(part, x).** DuckDB returns a **TIMESTAMP even for DATE input**,
while Snowflake returns a DATE for a DATE. Write
`CAST(date_trunc('month', x) AS DATE)` when the input is a DATE or the client
casts the result to `::date`. The week starts on Monday in both (Snowflake
default WEEK_START=0). `TRUNC(d, 'MM')` becomes `date_trunc`.

**D6. Names and weekdays.** `DAYNAME` and `MONTHNAME` return 'Fri'/'Mar' on
Snowflake, but DuckDB's `dayname`/`monthname` return full names, so use
`strftime(x, '%a')` and `strftime(x, '%b')`. `DAYOFWEEK` is 0 for Sunday in
both. `DAYOFWEEKISO` becomes `isodow`. `WEEK`/`WEEKOFYEAR` depend on Snowflake's
WEEK_OF_YEAR_POLICY, so flag them and let parity decide.

**D7. Constructors and conversions.**
- `TO_DATE(s)` becomes `CAST(s AS DATE)`, and `TO_DATE(s, fmt)` becomes `CAST(strptime(s, fmt) AS DATE)`.
- `TO_TIMESTAMP(_NTZ)(s)` becomes `CAST(s AS TIMESTAMP)`.
- `DATE_FROM_PARTS` becomes `make_date`, and `TIMESTAMP_FROM_PARTS` becomes `make_timestamp`.
- `LAST_DAY(d)` stays `last_day(d)`.
- `DATE_PART(epoch_second, ts)` becomes `trunc(epoch(ts))::bigint`. Bare `epoch(ts)` returns a DOUBLE with the fraction (1735689600.9), while Snowflake returns whole seconds. For pre-1970 timestamps with a fraction, let parity decide.

**D8. Clock functions.** `CURRENT_DATE`, `CURRENT_TIMESTAMP`, `SYSDATE()` and
`GETDATE()` are forbidden in models (H10). Use a var such as `as_of_date`.

**D9. Time zones.** `CONVERT_TIMEZONE('UTC', 'Europe/Amsterdam', ts)` becomes
`timezone('Europe/Amsterdam', timezone('UTC', ts))`. This was verified locally,
but it needs ICU, which has not been confirmed on Molinia: probe it with one
query before relying on it.

### 3.4 Semi-structured data (J)

**J0. VARIANT columns arrive as VARCHAR.** VARIANT, OBJECT and ARRAY columns
arrive in Molinia as **VARCHAR holding JSON text**, because Snowflake's Parquet
unload writes them as strings. Use the JSON functions on the VARCHAR directly.
Don't cast to DuckDB's `VARIANT` type, which is a different thing.

**J1. Paths.**

| Snowflake | Molinia |
|---|---|
| `v:key::string` | `v ->> '$.key'` (the same as `json_extract_string(v, '$.key')`) |
| `v:a.b::string` | `v ->> '$.a.b'` |
| `v:arr[0]::string` | `v ->> '$.arr[0]'` (JSON paths are 0-based in both) |
| `v['key']`, `GET_PATH(v, 'a.b')` | `v ->> '$.key'`, `v ->> '$.a.b'` (DuckDB `v['key']` on a VARCHAR is string slicing and fails) |
| `v:k::number` / `::int` | `CAST(v ->> '$.k' AS DECIMAL(38,0))` |
| `v:k::number(p,s)` | `CAST(v ->> '$.k' AS DECIMAL(p,s))` |
| `v:k::float` | `CAST(v ->> '$.k' AS DOUBLE)` |
| `v:k::boolean` | `CAST(v ->> '$.k' AS BOOLEAN)` (text 'true'/'false') |
| `v:k::date` / `::timestamp_ntz` | `CAST(v ->> '$.k' AS DATE / TIMESTAMP)` |
| `TRY_TO_NUMBER(v:k::string, 10, 2)` | `TRY_CAST(v ->> '$.k' AS DECIMAL(10,2))` |

A missing key, a JSON `null` and a NULL column all give NULL, which matches
Snowflake's `::string`. Use `->>` (text). `->`/`json_extract` return JSON with
quotes (`'"SAVE10"'`, `'null'`), which then compare wrong.

**J2. LANDMINE: `col:key` does not always fail.** DuckDB 1.5 reads `x:y` as the
prefix-alias syntax `y AS x`. If the table has a column named like the first
path key, `order_meta:status` (and `order_meta:status::string`) **silently
returns the `status` column** under the name `order_meta` (Appendix A, J2).
After rewriting, search the project for a single colon between identifiers
(`[A-Za-z_]:[A-Za-z_]`, which skips `::`), and don't rely on errors.

**J3. Other semi-structured functions.**
- `PARSE_JSON(s)` becomes `CAST(s AS JSON)`, or stays the VARCHAR when you only extract paths.
- `TRY_PARSE_JSON(s)` becomes `CASE WHEN json_valid(s) THEN s END`.
- `OBJECT_CONSTRUCT(k, v, …)` becomes `json_object(k, v, …)`. Snowflake drops pairs whose value is NULL, but DuckDB keeps `"k":null`. `OBJECT_CONSTRUCT_KEEP_NULL` equals `json_object`.
- `ARRAY_SIZE(v:arr)` becomes `CASE WHEN json_type(v, '$.arr') = 'ARRAY' THEN json_array_length(v, '$.arr') END`. Bare `json_array_length` returns 0 for a JSON `null`, where Snowflake returns NULL.
- `IS_NULL_VALUE(v:k)` becomes `json_type(v, '$.k') = 'NULL'`.
- `TYPEOF(v)` is **not portable**, so report it (section 8). `json_type` uses different names: 'UBIGINT' for 1, 'BIGINT' for −1, 'DOUBLE', 'VARCHAR', 'NULL'. Snowflake's names are INTEGER, DECIMAL, VARCHAR, NULL_VALUE and so on. A CASE that maps one to the other is possible, but only parity can prove it.

**J4. Numbers inside JSON** are normalised when extracted as text (`1e2` becomes
`'100.0'`), so cast them to a numeric type instead of comparing text.

### 3.5 LATERAL FLATTEN (F)

**F1.** An inner FLATTEN over an array becomes `unnest … WITH ORDINALITY`:

```sql
-- Snowflake
select e.event_id, f.index as item_position, f.value:sku::string as sku
from events e, lateral flatten(input => e.payload:items) f

-- Molinia
select e.event_id, f.idx - 1 as item_position, f.item ->> '$.sku' as sku
from events e, unnest(json_extract(e.payload, '$.items[*]')) with ordinality as f(item, idx)
```

- `json_extract(x, '$.items[*]')` returns a `JSON[]` list. The list is empty when `items` is missing, JSON null or `[]`, so that event produces no rows, as with FLATTEN.
- **Index base.** WITH ORDINALITY is **1-based** and FLATTEN's `f.index` is **0-based**, so write `idx - 1`. DuckDB list subscripts (`list[1]`) and `generate_subscripts` are 1-based too. JSON paths (`$[0]`) are 0-based.
- `f.value:k::string` becomes `f.item ->> '$.k'`, and `f.value:k::number` becomes `CAST(f.item ->> '$.k' AS DECIMAL(38,0))`.
- `TO_VARCHAR(f.index)` becomes `CAST(f.idx - 1 AS VARCHAR)`.

**F2.** `FLATTEN(…, OUTER => TRUE)` becomes
`LEFT JOIN LATERAL unnest(…) WITH ORDINALITY AS f(item, idx) ON TRUE`.

**F3.** FLATTEN over an object's keys (`f.key`) becomes
`unnest(json_keys(x))`. `RECURSIVE => TRUE` and `f.path`/`f.this` aren't
covered by this rulebook, so report them.

### 3.6 Aggregates and windows (A)

**A1. LISTAGG.** `LISTAGG(DISTINCT x, ',') WITHIN GROUP (ORDER BY x)` becomes
`coalesce(string_agg(DISTINCT x, ',' ORDER BY x), '')`, and
`LISTAGG(x, ',') WITHIN GROUP (ORDER BY y)` becomes
`coalesce(string_agg(x, ',' ORDER BY y), '')`. DuckDB rejects `WITHIN GROUP`
for listagg, and DISTINCT combined with WITHIN GROUP.

**Always write the `coalesce(…, '')`.** When a group has no non-NULL values,
Snowflake returns `''` (documented) and DuckDB returns NULL, which parity
reports as a differing column. When that can't happen the coalesce is a no-op,
so wrap it unconditionally: it costs one function call and saves the query you
would otherwise spend proving that `x` is never all-NULL in a group.

**A2. RATIO_TO_REPORT(x) OVER (PARTITION BY p).** It doesn't exist on DuckDB.
Pick the rewrite by the type the client used:

| x in the client's code | rewrite |
|---|---|
| FLOAT (`x::float`) | `x::double / nullif(sum(x::double) over (partition by p), 0)` (N10) |
| NUMBER | `{{ molinia_round_div('x', 'sum(x) over (partition by p)', S) }}`, where S is 8 for scale 2 |

Keep the client's outer `round(…, n)` and `100 *`. The denominator always
needs a guard, because DuckDB `x / 0` returns `inf`, not NULL.

**A3. ARRAY_AGG.** Snowflake drops NULLs and DuckDB keeps them.
`ARRAY_AGG(x) WITHIN GROUP (ORDER BY x)` becomes
`CAST(to_json(array_agg(x ORDER BY x) FILTER (WHERE x IS NOT NULL)) AS VARCHAR)`,
and DISTINCT works inside the same form. The `to_json` matters: the answer key
holds the array as JSON text (`["a","b"]`, J0). A bare DuckDB LIST casts to
`[a, b]`, so every row would differ. When a group has no non-NULL values, this
form returns NULL. If parity shows `'[]'` on the Snowflake side, wrap it in
`coalesce(…, '[]')`.

**A4. NULL order.** Snowflake defaults to ASC NULLS LAST and **DESC NULLS
FIRST**. DuckDB puts NULLs last in both directions. Write NULLS FIRST/LAST
explicitly on every DESC ordering in windows, QUALIFY, string_agg and final
ORDER BY.

**A5. Window frames.** Write the frame explicitly for LAST_VALUE and NTH_VALUE
(`rows between unbounded preceding and unbounded following`). DuckDB's default
frame with ORDER BY ends at the current row. FIRST_VALUE isn't affected.

**A6. QUALIFY** works as-is. Every ROW_NUMBER, RANK and FIRST_VALUE ordering
must be total, with a tie-breaker on the key, or the engines may keep
different rows.

**A7. COUNT_IF(c)** works but returns HUGEINT, so add `::bigint` (N12).
`count(*) FILTER (WHERE c)` returns the same value as BIGINT already, so it
needs no cast, but `count_if(c)::bigint` is the smaller diff.

**A8. Other aggregates.**
- `BOOLAND_AGG`/`BOOLOR_AGG` become `bool_and`/`bool_or`.
- `MEDIAN` works.
- `APPROX_COUNT_DISTINCT`, `APPROX_PERCENTILE` and HLL give estimates that differ between engines: use the exact `count(distinct)`/`quantile_cont`, or flag them.
- `ANY_VALUE` isn't deterministic, so flag it.
- `HASH_AGG` isn't portable (section 8).

**A9. Syntax that ports unchanged.** `GROUP BY ALL`, `SELECT * EXCLUDE (…)`,
`SELECT * RENAME (…)`, `MERGE` and `QUALIFY` work as written.

### 3.7 Strings, regex, identifiers (X)

**X1. Regex.**
- `REGEXP_SUBSTR(s, p)` becomes `regexp_extract(s, p)`, and `regexp_extract(s, p, group)` handles a group argument. With no match Snowflake returns NULL and DuckDB returns '', so write `nullif(regexp_extract(s, p), '')`.
- `RLIKE`/`REGEXP_LIKE(s, p)` becomes `regexp_full_match(s, p)`: Snowflake anchors the whole string, and DuckDB's `regexp_matches` is a partial match.
- `REGEXP_REPLACE(s, p, r)` becomes `regexp_replace(s, p, r, 'g')`. Snowflake replaces every match, and DuckDB replaces only the first one unless you pass `'g'`.
- **Halve the backslashes** in ported regex literals: Snowflake `'\\d'` becomes DuckDB `'\d'`. Snowflake string literals process backslash escapes and DuckDB's don't, so a straight port matches a literal backslash followed by `d`. A wrong pattern returns no error, just no match, so check each ported regex with a count query.

**X2. Splitting.** `SPLIT(s, d)` becomes `string_split(s, d)`, which returns a
LIST. `SPLIT_PART(s, d, n)` matches for n ≥ 1 and for negative n. For n = 0,
Snowflake treats it as 1 while DuckDB returns '', so write 1.

**X3. Search and case functions.** `CHARINDEX(sub, s)` becomes `instr(s, sub)`
(the arguments swap). `CONTAINS`, `STARTSWITH` and `ENDSWITH` become
`contains`, `starts_with` and `ends_with`. `INITCAP` is missing on DuckDB, so
report it (section 8).

**X4. Hashes.** `HASH(…)` values differ: DuckDB's `hash` is a UBIGINT from a
different algorithm. `MD5` matches (lower-case hex). Keys or columns built from
HASH aren't portable, so report them (section 8).

**X5. Identifiers.** Unquoted and quoted identifiers are case-insensitive on
DuckDB (`"Customer_Id"` finds `CUSTOMER_ID`). Output names follow the source
spelling unless aliased (P9).

**X6. String comparison** is case-sensitive in both engines. `ILIKE` works.

### 3.8 DDL and syntax (Y)

**Y1.** `TOP n` becomes `LIMIT n`. `SAMPLE`/`TABLESAMPLE` are forbidden in
models (H10).

**Y2.** `TRANSIENT`, `CLUSTER BY`, `COPY GRANTS`, `UNDROP`, `CLONE`,
`GRANT`/`REVOKE`, `ALTER … SET TAG` and `SECURE` views have no DuckDB
equivalent: remove them from models and hooks (P2).

**Y3.** Session variables (`SET x = …`, `$x`) become dbt vars.
`IDENTIFIER('…')` becomes a plain name.

**Y4. Select-list aliases: never reference one in the same select list.**
DuckDB has **lateral column aliases** (an expression may use an alias defined
earlier in the same select list) and Snowflake does not. So a reference that
Snowflake rejects, or reads as a table column, can bind to something else on
Molinia, and DuckDB never raises an error, so only parity would catch it.
Measured on DuckDB 1.5.5 (Appendix A, Y4):

| the name you reference | DuckDB 1.5.5 | Snowflake |
|---|---|---|
| exists as a column of the FROM relation **and** as an earlier alias | the **column** wins; the alias you just wrote is ignored | the column |
| only as an earlier alias | the **alias** resolves, silently | error: invalid identifier |
| only as a *later* alias | Binder Error (loud, harmless) | error |

The dangerous row is the first one, because both engines agree and both are
probably not what you meant. Worked example, with `discount_pct` NULL in the raw
table and the port trying to reuse the cleaned value:

```sql
-- WRONG: the second discount_pct is the raw column (NULL), not the alias above
select
    coalesce(discount_pct, 0)                                  as discount_pct,
    round(quantity * unit_price * (1 - discount_pct * 0.01), 2) as line_amount   -- NULL
from {{ source('raw', 'order_items') }}

-- RIGHT: repeat the expression (what the client's Snowflake SQL already did)
select
    coalesce(discount_pct, 0)                                               as discount_pct,
    round(quantity * unit_price * (1 - coalesce(discount_pct, 0) * 0.01), 2) as line_amount
from {{ source('raw', 'order_items') }}
```

Or put the cleaned columns in a CTE and select from it. Repeat the expression,
or add a CTE; never rely on the alias. After porting, list the candidates (every
line that uses a name aliased earlier in the same select block) and eyeball each
one:

```bash
find acme_shop/models acme_shop/tests -name '*.sql' -exec awk '
  { l = tolower($0); sub(/--.*/, "", l) }
  l ~ /(^|[^a-z0-9_])select([^a-z0-9_]|$)/ { split("", A) }
  { c = l; gsub(/[^a-z0-9_]as[ \t]+[a-z_][a-z0-9_]*/, " ", c)
    for (n in A) if (c ~ "(^|[^a-z0-9_.])" n "([^a-z0-9_.(]|$)")
      printf "%s:%d  alias \"%s\" reused in the same select list\n", FILENAME, FNR, n
    t = l
    while (match(t, /[^a-z0-9_]as[ \t]+[a-z_][a-z0-9_]*/)) {
      s = substr(t, RSTART, RLENGTH); sub(/^[^a-z0-9_]as[ \t]+/, "", s)
      if (s !~ /^(varchar|decimal|numeric|number|timestamp|timestamptz|date|double|float|real|bigint|hugeint|integer|int|boolean|json|text|string)$/) A[s] = 1
      t = substr(t, RSTART + RLENGTH) } }' {} +
```

It lists candidates, not defects: on the ported acme_shop it prints 12 lines and
all 12 are correct (the client repeats the expression, which is the RIGHT form
above). Read each one and check that the value you meant is the one that binds.

## 4. The exact-division macro

The kit ships it as a file. Copy it, don't retype or extract it:

```bash
cp agent/snippets/molinia_round_div.sql acme_shop/macros/
```

Appendix B tests it, rendered by jinja2, against Python `decimal` on about
1.8 million cases with zero mismatches. The listing below is the same bytes,
printed here so you can read what you copied (a unit test in `agent/tests/`
fails if the two ever drift apart).

<!-- snippet:molinia_round_div:begin -->
```sql
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
```
<!-- snippet:molinia_round_div:end -->

| Snowflake (numerator scale in brackets) | Molinia |
|---|---|
| `ROUND(DIV0(revenue, orders), 2)` [2] | `round({{ molinia_round_div('revenue', 'orders', 8, div0=true) }}, 2)` |
| `a / b` [2], no ROUND | `{{ molinia_round_div('a', 'b', 8) }}` |
| `ROUND(100 * RATIO_TO_REPORT(x) OVER (PARTITION BY p), 2)`, x NUMBER [2] | `round(100 * {{ molinia_round_div('x', 'sum(x) over (partition by p)', 8) }}, 2)` |
| `IFF(DIV0(paid, total) >= 1, TRUE, FALSE)` [2] | `case when {{ molinia_round_div('paid', 'total', 8, div0=true) }} >= 1 then true else false end` |
| `DIV0(amount_cents, 100)` | `amount_cents * 0.01` (N8, no macro) |
| `ROUND(AVG(price), 2)` [2] | `round({{ molinia_round_div('sum(price)', 'count(price)', 8) }}, 2)` |

**Why the macro rounds twice.** Snowflake rounds a NUMBER quotient to its
scale S first, and the outer ROUND rounds again. Passing S and keeping the
outer `round()` reproduces that exactly. A single rounding straight to 2
decimals differs only when the exact quotient lies within 0.5·10⁻ˢ of a
rounding boundary. For scale-2 money that takes a divisor of 200,000 or more
for an average, or a total of €100.00 or more for a share. So use the two-step
form.

The arguments are SQL strings: quote them. They may be any expression,
including aggregates and window sums. The macro repeats each expression a few
times, which is fine.

## 5. dbt-molinia: facts and limits

- **Adapter version.** The kit venv runs a pre-release dbt-molinia: commit `aec93568` on branch `feat/dbt-molinia-429-backoff`, not yet merged. It adds pacing and 429 retry. The released adapter on `main` has neither, and it ignores the two profile keys without an error. Any reinstall must be pinned to that commit (RUNSHEET section 1).
- **Engine path.** Never set `warehouse_id` (H5): the demo runs on the org engine only.
- **Request cost.** Each SQL statement is one HTTP POST, plus one connectivity check (`SELECT schema_name … LIMIT 1`) each time dbt opens a connection, which is about once per node. The fake-endpoint run in Appendix C made 80 requests for 10 models and 21 tests, half of them connection checks. For acme_shop (10 models, 63 tests including 12 source tests), a full `dbt build` took **164 requests (82 of them connection checks) and 6 minutes** with the installed adapter pacing at 25 per minute (Appendix C). `dbt run` (models only) is about 40 requests: it took **63 s** against dev in the 2026-09-19 rehearsal, including one 55 s pacing wait, and `dbt test` took the remaining 5 minutes. Run them as two commands (`CLAUDE.md`, Commands).
- **Naming.** Relations render two-part (P5). Custom schemas are used as-is (P4).
- **Supported.** `view` and `table` work: Appendix C, and the live run. `incremental` is implemented as delete+insert through the `_dbt_internal` schema, but it is **unverified**. It has never run against Molinia or the local stub, and the adapter's only test of it is a syntax test (P6).
- **Unsupported.**
  - Seeds fail, because bind parameters are refused.
  - Snapshots fail, because temp tables don't survive between requests.
  - `dbt docs generate` fails at the catalog step (no `get_catalog`).
  - Python models don't work.
  - Before this kit, the adapter had been proven against production for only one table and one view model.
- **dbt tests work, with the kit's adapter.** Generic and singular tests run as
  ordinary queries and report properly on the pinned pre-release adapter. On the
  **released** adapter every test is reported as `ERROR: '0' is not of type
  'integer'`. The query ran and returned zero failing rows, but the count comes
  back as the JSON string `'0'` (Molinia returns BIGINT and DECIMAL as strings to
  stay exact beyond 2^53) and dbt's run-results schema requires an integer. That
  is a red `dbt build` on green data. It was found in the 2026-09-19 rehearsal
  and fixed in the adapter. If you ever see it, the venv is on the wrong adapter:
  report it, don't touch the tests (H9), and read the model results, which are
  unaffected.
- **Free local checks.** `dbt parse` and `dbt ls` (`tools/dbt_molinia.py parse` and `ls`) make **no API calls**, so use them to validate Jinja and YAML.
- **Free local dry-run.** `agent/localcheck.py` renders every model with jinja2
  and runs it on local DuckDB 1.5.5 against synthetic `raw_*` tables with the
  real ingest types. No API calls, no keys, no kit data files. It proves the SQL
  binds and shows the column types it produces (`--types`); it proves **nothing**
  about the values, because the fixture rows are invented. Only `agent/parity.py`
  against `main.sf_*` proves equality. Run it after each layer, so the paid build
  finds real problems instead of typos. `--only <name> …` narrows the report to
  those models or singular tests and builds their upstreams anyway; an unknown
  name is an error, never a silent pass.
- **Rate-limit handling.** With the kit's adapter, `requests_per_minute: 25` paces it and `retry_max_wait_seconds: 600` waits out HTTP 429s. If a build finishes with `Molinia query failed (429)`, wait 60 s, then run `tools/dbt_molinia.py retry`, which re-runs only the failed and skipped nodes. A single statement can wait up to 10 minutes, so run every build with the 10-minute Bash timeout, and run `run` and `test` as two commands rather than one `build` (`CLAUDE.md`, Commands). Pacing waits look like a hang: the adapter prints `Molinia adapter: pacing to requests_per_minute=25, waiting 55.4s` and carries on.

## 6. Rate limits

| limit | value | shared by |
|---|---|---|
| HTTP requests per client IP, whole API | 60 / min | dbt, parity, `tools/*`, and the presenter's browser on the same network |
| org-engine queries, free plan | 30 / min | every `query/execute` call, including dbt's connection checks |
| org-engine time | 3600 s per UTC day | everything |

The tools pace themselves: `tools/molinia.py` and `agent/parity.py` stay at 25
or fewer engine queries per minute and wait out 429s. dbt paces at 25 per
minute. Two of them running at once exceed 30 per minute. So:

- **One API-using command at a time.** Never run commands in parallel or in the background.
- **Never loop parity or builds.**
- **Prefer small selections.** Use `-s <model>+` and `--model <name>` over full runs.
- **Budget per migration session:** at most 2 full builds (a `run` plus a `test` counts as one), 3 full parity runs, and about 20 diagnostic queries. The free checks (`parse`, `ls`, `agent/localcheck.py`) are unlimited.

## 7. Parity

Run parity from the kit root:

```bash
.venv/bin/python agent/parity.py                                   # all 10 models, 11 queries
.venv/bin/python agent/parity.py --model stg_orders fct_orders     # a subset
```

It compares `<schema>.<model>` with `main.sf_<model>` using a FULL OUTER JOIN on
the key from `parity.yml`. It returns counts only and exits 0 only when every
model matches.

**Reading the table.**
- The `status` column shows `match`, `MISMATCH`, `MISSING` or `ERROR`.
- `rows sf` and `rows molinia` are the two row counts.
- `only sf` and `only molinia` count keys that have no partner on the other side.
- `cols differing` counts non-key columns with at least one differing row.
- The **Details** block names each differing column, the number of rows and both types.
- Numeric columns compare with a relative tolerance of 1e-9, so float noise passes and **one cent fails**.

| symptom | likely cause | fix |
|---|---|---|
| `MISSING … Molinia relation not found` | the model failed or landed in the wrong schema | Read the dbt output (P4, P1). |
| `MISSING … answer key not found` | an ingest problem, not yours | **Stop and report** (H6). |
| keys only in sf and/or only in molinia | a filter, join or dedupe differs: NULL ordering in QUALIFY (A4, A6), string case or trim, FLATTEN index base or OUTER (F1, F2), a date boundary (D3 to D5), a NULL in a join key | Fix the logic, then rerun that model. |
| `key not unique in Molinia` | join fan-out, unnest duplicates, a missing QUALIFY | Check the join keys and the grain. |
| a numeric column is off on a few rows | the DOUBLE path in money math (N2, N4, N7, N9) | Keep DECIMAL, and use the macro for division. |
| a numeric column is off by ±1 in the last rounded digit on 1 to 3 rows of a FLOAT column | an exact tie under float rounding (N10) | Try the other form (DOUBLE vs macro) once. If rows still differ, **report the count**: the client's FLOAT makes that value engine-dependent, and it is the reviewer's call. |
| a numeric column is off on many rows | the wrong formula or scale, `//` vs `/`, DIV0 vs NULL, `inf` | Re-read the Snowflake expression. |
| a varchar column differs | format (D2), JSON quotes from `->` (J1), '' vs NULL (A1, X1), DAYNAME (D6), trim or case, CONCAT NULLs (S5) | |
| a boolean column differs | IFF with a NULL condition (S1), NVL defaults (S2), JSON boolean extraction (J1) | |
| a date or timestamp column differs | DATEADD or DATE_TRUNC returning a TIMESTAMP (D3, D5), a time zone | |
| a column exists on one side only | a renamed or dropped column, an alias typo | Restore the original name (P9, H9). |
| `type mismatch … compared as VARCHAR` | type family changed (JSON vs VARCHAR, BOOLEAN vs VARCHAR) | Align the type. |
| `error: … 429` | rate limit | Wait 60 s and rerun that one model, not everything. |

**Diagnosing.** Use at most about 5 queries per mismatch, and aggregates only
(H8):

```bash
.venv/bin/python tools/molinia.py query "select count(*) from marts.fct_orders m join main.sf_fct_orders s on s.order_id = m.order_id where m.is_fully_paid is distinct from s.is_fully_paid and s.order_total = 0"
```

**When to stop.** If a model still doesn't match after 3 fix attempts, stop
changing it. Report the parity details, your best hypothesis and the rule it
points to, and move on to the next model.

## 8. Not portable today: report, don't hack

- **Snowflake Scripting.** Control flow (`DECLARE … BEGIN … END`, IF/FOR/LOOP), stored procedures, and tasks that CALL procedures.
- **Python.** Python models, UDFs and UDTFs, including Snowpark. Java and JavaScript UDFs as well.
- **External data.** Iceberg or external-table reads. There is also no direct read from Snowflake: data arrives as a Parquet unload through EU object storage plus Molinia ingest.
- **dbt features.** `dbt seed`, `dbt snapshot` and the `dbt docs generate` catalog (section 5).
- **Values that can't be reproduced.** `HASH`/`HASH_AGG`-derived values, `INITCAP`, `TYPEOF` (J3), `ROUND(…, 'HALF_TO_EVEN')` on NUMBER (N5), approximate aggregates, `RANDOM`/`UUID_STRING`/`SEQUENCE`, and `CLONE`/`UNDROP`/`AT(…)` time-travel syntax.
- **Governance objects.** Masking policies, row access policies and grants are recreated in the Molinia console by an admin, never from dbt.

## 9. Checks: one before you start, the rest before you say "done"

**Step 0. Ask the engine what the source types are: one query, before you port
anything.** Every N, D and J decision below depends on the type ingest
actually produced, and guessing costs a whole build. `typeof()` over the `raw_*`
tables answers it in one aggregate-only call (H8: types, never rows), and
`raw_customer_contacts` is deliberately absent (H2):

```bash
.venv/bin/python tools/molinia.py query "
select 'raw_customers'   as tbl, max(typeof(customer_id) || ' | ' || typeof(signup_ts) || ' | ' || typeof(country_code) || ' | ' || typeof(marketing_opt_in)) as types from main.raw_customers
union all select 'raw_products',   max(typeof(product_id) || ' | ' || typeof(unit_price) || ' | ' || typeof(category_code) || ' | ' || typeof(is_active)) from main.raw_products
union all select 'raw_orders',     max(typeof(order_id) || ' | ' || typeof(order_ts) || ' | ' || typeof(status) || ' | ' || typeof(order_meta)) from main.raw_orders
union all select 'raw_order_items',max(typeof(order_item_id) || ' | ' || typeof(quantity) || ' | ' || typeof(unit_price) || ' | ' || typeof(discount_pct)) from main.raw_order_items
union all select 'raw_payments',   max(typeof(payment_id) || ' | ' || typeof(amount_cents) || ' | ' || typeof(status) || ' | ' || typeof(paid_ts)) from main.raw_payments
union all select 'raw_web_events', max(typeof(event_id) || ' | ' || typeof(customer_id) || ' | ' || typeof(event_ts) || ' | ' || typeof(payload)) from main.raw_web_events"
```

The answer on this dataset (2026-09-19 rehearsal), and what it decides:

| finding | consequence |
|---|---|
| every id and counter is **DECIMAL(38,0)** (Snowflake `NUMBER(38,0)`), not BIGINT | `sum(id)` stays DECIMAL: no `::bigint` (N12). `::decimal(38,0)` in casts, never `::int` (N1). |
| `order_meta` and `payload` are **VARCHAR** holding JSON text | use `->>` on the VARCHAR directly, don't `PARSE_JSON` (J0, J1). |
| money is **DECIMAL(10,2)**, timestamps are **TIMESTAMP** (not TIMESTAMPTZ) | `::timestamp` is safe (D1); keep money in DECIMAL end to end (N3). |

That table is the record of one run, not a guarantee: run the query anyway, it
is one call. `agent/localcheck.py`'s fixture tables encode exactly that answer,
so if the query returns something else, edit its `FIXTURES` to match before you
trust its output.

**Then, before you say "done":**

1. In `acme_shop/`, run `grep -rniE "\b(iff|nvl|nvl2|zeroifnull|nullifzero|div0|decode|to_varchar|to_char|dateadd|try_to_number|ratio_to_report|listagg|flatten|timestamp_ntz)\b|\bnumber\(" models macros tests | grep -v molinia_round_div`. Only comment lines may remain.
2. Run `grep -rnE "[A-Za-z_]:[A-Za-z_]" models macros tests`. The only hits should be inside strings or comments (J2).
3. Run the alias-shadowing scan from Y4 and read every line it prints.
4. Check that no `/` remains on money or NUMBER operands outside the macro, apart from float-on-purpose cases (N2, N10):

   ```bash
   grep -rnE "/ *[a-z_(]" models tests macros | grep -v nullif
   ```

   A bare `grep "/"` matches file paths and prose and tells you nothing. Every
   hit this pattern returns is either inside `molinia_round_div`, a documented
   float-on-purpose case, or a bug.
5. Check that no `transient`, `copy_grants`, `query_tag`, `cluster_by` or `database:` remain (P2, P3).
6. `tools/dbt_molinia.py parse` and `agent/localcheck.py` both pass (free, no API calls).
7. `dbt run` then `dbt test` pass on the molinia target (`CLAUDE.md`, Commands).
8. `agent/parity.py` exits 0, or every residual difference is explained in the report.

---

## Appendix A. Verification log (local DuckDB 1.5.5, 2026-09-19)

These tables were generated by running each snippet on local DuckDB 1.5.5
from the kit venv. Tables used by the snippets:
- `o(order_id, status, order_meta)`, where `order_meta` is VARCHAR JSON with and without keys.
- `w(event_id, payload)`, with two items, an empty list and a missing `items`.
- `ch(g, channel)`.
- `up("CUSTOMER_ID", "EMAIL")`.

`{{ molinia_round_div(…) }}` calls were rendered with jinja2 from the macro in
section 4 before execution.

### A.1 The Snowflake forms fail on DuckDB 1.5.5 (or misbehave)

| probe | snippet | DuckDB 1.5.5 result |
|---|---|---|
| probe | `select iff(true, 1, 2)` | **Catalog**: Catalog Error: Scalar Function with name iff does not exist! |
| probe | `select nvl(null, 1)` | **Catalog**: Catalog Error: Scalar Function with name nvl does not exist! |
| probe | `select zeroifnull(null)` | **Catalog**: Catalog Error: Scalar Function with name zeroifnull does not exist! |
| probe | `select div0(1, 0)` | **Catalog**: Catalog Error: Scalar Function with name div0 does not exist! |
| probe | `select to_varchar(date '2025-03-07', 'YYYY-MM')` | **Catalog**: Catalog Error: Scalar Function with name to_varchar does not exist! |
| probe | `select dateadd(day, 30, date '2025-01-01')` | **Catalog**: Catalog Error: Scalar Function with name dateadd does not exist! |
| probe | `select datediff(day, date '2025-01-01', date '2025-01-05')` | **Binder**: Binder Error: Referenced column "day" was not found because the FROM clause is missing |
| probe | `select decode('EL', 'EL', 'Electronics', 'Other')` | **Binder**: Binder Error: No function matches the given name and argument types 'decode(STRING_LITERAL, STRING_LITERAL, STRING_LITER |
| probe | `select try_to_number('4.95', 10, 2)` | **Catalog**: Catalog Error: Scalar Function with name try_to_number does not exist! |
| probe | `select ratio_to_report(x) over () from (values (1), (3)) t(x)` | **Catalog**: Catalog Error: Aggregate Function with name ratio_to_report does not exist! |
| probe | `select listagg(distinct channel, ',') within group (order by channel) from ch` | **Parser**: Parser Error: cannot use DISTINCT with WITHIN GROUP |
| probe | `select listagg(channel, ',') within group (order by channel) from ch` | **Parser**: Parser Error: Unknown ordered aggregate "listagg". |
| probe | `select regexp_substr('abc123', '[0-9]+')` | **Catalog**: Catalog Error: Scalar Function with name regexp_substr does not exist! |
| probe | `select nvl2(null, 1, 2)` | **Catalog**: Catalog Error: Scalar Function with name nvl2 does not exist! |
| probe | `select initcap('hello world')` | **Catalog**: Catalog Error: Scalar Function with name initcap does not exist! |
| probe | `select 1::number` | **Catalog**: Catalog Error: Type with name number does not exist! |
| probe | `select '2025-01-01'::timestamp_ntz` | **Catalog**: Catalog Error: Type with name timestamp_ntz does not exist! |
| probe | `select order_meta:coupon::string from o` | **Binder**: Binder Error: Referenced column "coupon" not found in FROM clause! |
| probe | `select payload:utm.source::string from w` | **Binder**: Binder Error: Referenced table "utm" not found! |
| probe | `select top 1 order_id from o` | **Parser**: Parser Error: syntax error at or near "1" |
| probe | `create transient table t1 as select 1 as x` | **Parser**: Parser Error: syntax error at or near "transient" |
| probe | `create table t2 (x int) cluster by (x)` | **Parser**: Parser Error: syntax error at or near "cluster" |
| probe | `select * from o sample (2)` | **Parser**: Parser Error: syntax error at or near "2" |

### A.2 The Molinia forms (by rule id)

| rule | snippet | DuckDB 1.5.5 result |
|---|---|---|
| S1 IFF | `select case when null then 'a' else 'b' end as iff_null_cond, if(true, 1, 2) as duck_if` | iff_null_cond='b', duck_if=1 |
| S2 NVL/ZEROIFNULL | `select coalesce(null, 'x') as nvl, coalesce(null::decimal(10,2), 0) as zeroifnull, typeof(coalesce(null::decimal(10,2), 0)) as t` | nvl='x', zeroifnull=0.00, t='DECIMAL(12,2)' |
| S3 DECODE | `select c, case c when 'EL' then 'Electronics' else 'Other' end as simple_case, case when c is not distinct from null then 'none' when c = 'EL' then 'Electronics' else 'Other' end as null_safe from (values ('EL'), (null)) t(c)` | c='EL', simple_case='Electronics', null_safe='Electronics'; c=NULL, simple_case='Other', null_safe='none' |
| S4 GREATEST | `select greatest(1, null, 3) as duck_greatest, case when 1 is null or null is null then null else greatest(1, null) end as sf_like` | duck_greatest=3, sf_like=NULL |
| S5 CONCAT | `select concat('a', null::varchar, 'b') as duck_concat, 'a' \|\| null::varchar \|\| 'b' as sf_like` | duck_concat='ab', sf_like=NULL |
| S5 CONCAT_WS | `select concat_ws('-', 'a', null::varchar, 'b') as duck_concat_ws` | duck_concat_ws='a-b' |
| N1 types | `select typeof(1::decimal) as bare_decimal, typeof(1::int) as int_, typeof(1::decimal(38,0)) as number_` | bare_decimal='DECIMAL(18,3)', int_='INTEGER', number_='DECIMAL(38,0)' |
| N1 types | `select 3000000000::int` | **Conversion**: Conversion Error: Type INT64 with value 3000000000 can't be cast because the value is out of range for the destination t |
| N2 division | `select typeof(10.20::decimal(10,2) / 2.5::decimal(5,2)) as dec_dec, typeof(10.20::decimal(10,2) / 100) as dec_int, typeof(7 / 2) as int_int, 7 / 2 as v` | dec_dec='DOUBLE', dec_int='DOUBLE', int_int='DOUBLE', v=3.5 |
| N2 division | `select 1 / 0 as slash_zero, 1 // 0 as intdiv_zero` | slash_zero=inf, intdiv_zero=NULL |
| N3 multiply | `select typeof(3::decimal(38,0) * 10.20::decimal(10,2) * (1 - 12.5::decimal(5,2) * 0.01)) as t, 3::decimal(38,0) * 10.20::decimal(10,2) * (1 - 12.5::decimal(5,2) * 0.01) as v` | t='DECIMAL(38,6)', v=26.775000 |
| N4 half cent | `select round(10.20::decimal(10,2) * (1 - 12.5::decimal(5,2) / 100), 2) as naive_double, round(10.20::decimal(10,2) * (1 - 12.5::decimal(5,2) * 0.01), 2) as decimal_idiom` | naive_double=8.92, decimal_idiom=8.93 |
| N4 half cent | `select 10.20::decimal(10,2) * (1 - 12.5::decimal(5,2) / 100) as naive_unrounded` | naive_unrounded=8.924999999999999 |
| N5 ROUND | `select round(8.925::decimal(10,3), 2) as dec, round(-8.925::decimal(10,3), 2) as dec_neg, round(1.005::decimal(10,3), 2) as dec2, round(1.005::double, 2) as dbl, round(2.675::double, 2) as dbl_up` | dec=8.93, dec_neg=-8.93, dec2=1.01, dbl=1.0, dbl_up=2.68 |
| N5 round_even | `select typeof(round_even(2.5::decimal(3,1), 0)) as t, round_even(1.005::decimal(10,3), 2) as not_half_even, round_even(-131.045::decimal(10,3), 2) as not_half_even_neg` | t='DOUBLE', not_half_even=1.01, not_half_even_neg=-131.05 |
| N6 casts | `select cast(8.925::decimal(10,3) as decimal(12,2)) as dec_cast, cast(2.5::double as integer) as dbl_to_int, cast(2.5::decimal(3,1) as integer) as dec_to_int` | dec_cast=8.93, dbl_to_int=2, dec_to_int=3 |
| N7 macro (section 4) | `select {{ molinia_round_div('17.85', '2', 2) }} as r, typeof({{ molinia_round_div('17.85', '2', 2) }}) as t` | r=8.93, t='DECIMAL(38,2)' |
| N7 macro (section 4) | `select {{ molinia_round_div('-490.83', '6', 2) }} as one_step, round(-490.83 / 6, 2) as naive_double` | one_step=-81.81, naive_double=-81.8 |
| N7 macro (section 4) | `select {{ molinia_round_div('1.00', '0', 2) }} as plain, {{ molinia_round_div('1.00', '0', 2, div0=true) }} as div0, {{ molinia_round_div('null', '5', 2) }} as null_in` | plain=NULL, div0=0, null_in=NULL |
| N7 macro (section 4) | `select round({{ molinia_round_div('45.90', '2', 8, div0=true) }}, 2) as aov` | aov=22.95 |
| N8 DIV0 by constant | `select 1234::decimal(38,0) * 0.01 as euros, typeof(1234::decimal(38,0) * 0.01) as t, cast(1234::decimal(38,0) * 0.01 as decimal(12,2)) as cast12` | euros=12.34, t='DECIMAL(38,2)', cast12=12.34 |
| N11 TRY_CAST | `select try_cast('4.95' as decimal(10,2)) a, try_cast('4.955' as decimal(10,2)) b, try_cast('' as decimal(10,2)) c, try_cast(' 4.95 ' as decimal(10,2)) d, try_cast('abc' as decimal(10,2)) e, try_cast('4.95' as decimal(38,0)) f` | a=4.95, b=4.96, c=NULL, d=4.95, e=NULL, f=5 |
| N12 aggregate types | `select typeof(sum(x)) as sum_dec, typeof(avg(x)) as avg_dec, typeof(sum(y)) as sum_int, typeof(count_if(y > 0)) as count_if_t from (values (1.10::decimal(10,2), 1)) t(x, y)` | sum_dec='DECIMAL(38,2)', avg_dec='DOUBLE', sum_int='HUGEINT', count_if_t='HUGEINT' |
| N12 no cast on ids | `select typeof(sum(id)) as sum_id, typeof(sum(n)) as sum_bigint, typeof(count_if(n > 0)) as count_if_, typeof(count(*) filter (where n > 0)) as count_filter from (values (1::decimal(38,0), 1::bigint)) t(id, n)` | sum_id='DECIMAL(38,0)', sum_bigint='HUGEINT', count_if_='HUGEINT', count_filter='BIGINT' |
| D2 formats | `select strftime(timestamp '2025-03-07 14:05:09.123456', '%Y-%m') ym, strftime(timestamp '2025-03-07 14:05:09.123456', '%Y-%m-%d %H:%M:%S.%g') ntz_default, strftime(date '2025-03-07', '%d/%m/%Y') dmy, strftime(date '2025-03-07', '%a %b') short_names` | ym='2025-03', ntz_default='2025-03-07 14:05:09.123', dmy='07/03/2025', short_names='Fri Mar' |
| D2 formats | `select cast(timestamp '2025-01-01 10:00:00' as varchar) as ts_txt, cast(true as varchar) as bool_txt, cast(12.30::decimal(10,2) as varchar) as dec_txt, cast(123::decimal(38,0) as varchar) as id_txt` | ts_txt='2025-01-01 10:00:00', bool_txt='true', dec_txt='12.30', id_txt='123' |
| D3 DATEADD | `select date '2025-01-01' + 30 as date_plus_int, typeof(date '2025-01-01' + 30) as t1, date '2025-01-01' + interval 30 day as date_plus_interval, typeof(date '2025-01-01' + interval 30 day) as t2` | date_plus_int=2025-01-31, t1='DATE', date_plus_interval=2025-01-31 00:00:00, t2='TIMESTAMP' |
| D3 DATEADD | `select cast(date '2025-01-31' + interval 1 month as date) as month_end, timestamp '2025-01-31 10:00:00' + interval 1 month as ts_month_end` | month_end=2025-02-28, ts_month_end=2025-02-28 10:00:00 |
| D4 DATEDIFF | `select datediff('day', timestamp '2025-01-01 23:59:00', timestamp '2025-01-02 00:01:00') as day_boundary, datediff('month', date '2025-01-31', date '2025-02-01') as month_boundary, datediff('day', date '2025-01-05', date '2025-01-01') as negative` | day_boundary=1, month_boundary=1, negative=-4 |
| D4 DATEDIFF week | `select datediff('week', date '2025-01-05', date '2025-01-06') as duck_week_sun_mon, datediff('day', date_trunc('week', date '2025-01-05'), date_trunc('week', date '2025-01-06')) // 7 as boundary_weeks` | duck_week_sun_mon=0, boundary_weeks=1 |
| D5 DATE_TRUNC | `select typeof(date_trunc('month', date '2025-03-17')) as on_date, typeof(date_trunc('month', timestamp '2025-03-17 10:00:00')) as on_ts, cast(date_trunc('month', date '2025-03-17') as date) as fixed` | on_date='TIMESTAMP', on_ts='TIMESTAMP', fixed=2025-03-01 |
| D6 names | `select dayname(date '2025-03-07') as duck_dayname, strftime(date '2025-03-07', '%a') as sf_dayname, monthname(date '2025-03-07') as duck_monthname, strftime(date '2025-03-07', '%b') as sf_monthname, dayofweek(date '2025-03-09') as dow_sunday` | duck_dayname='Friday', sf_dayname='Fri', duck_monthname='March', sf_monthname='Mar', dow_sunday=0 |
| D7 TO_DATE | `select cast(strptime('07/03/2025', '%d/%m/%Y') as date) as with_format, '2026-06-30'::date as literal, cast(timestamp '2025-03-07 10:00:00' as date) as from_ts` | with_format=2025-03-07, literal=2026-06-30, from_ts=2025-03-07 |
| D7 epoch | `select epoch(timestamp '2025-01-01 00:00:00.9') as duck_epoch, trunc(epoch(timestamp '2025-01-01 00:00:00.9'))::bigint as sf_like, typeof(trunc(epoch(timestamp '2025-01-01 00:00:00.9'))::bigint) as t` | duck_epoch=1735689600.9, sf_like=1735689600, t='BIGINT' |
| D9 TIMEZONE | `select timezone('Europe/Amsterdam', timezone('UTC', timestamp '2025-07-01 10:00:00')) as utc_to_ams` | utc_to_ams=2025-07-01 12:00:00 |
| J1 paths | `select order_id, order_meta ->> '$.coupon' as coupon, json_extract_string(order_meta, '$.shipping.method') as method, try_cast(order_meta ->> '$.shipping.cost' as decimal(10,2)) as cost, cast(order_meta ->> '$.gift' as boolean) as gift from o order by 1` | order_id=1, coupon='SAVE10', method='express', cost=4.95, gift=True; order_id=2, coupon=NULL, method=NULL, cost=NULL, gift=False; order_id=3, coupon=NULL, method=NULL, cost=NULL, gift=NULL |
| J1 paths | `select json_extract(order_meta, '$.coupon') as json_value, typeof(json_extract(order_meta, '$.coupon')) as t from o order by order_id` | json_value='"SAVE10"', t='JSON'; json_value='null', t='JSON'; json_value=NULL, t='JSON' |
| J1 casts | `select cast('{"n": 2}' ->> '$.n' as integer) as int_, cast('{"n": 2.5}' ->> '$.n' as decimal(10,2)) as dec_, '{"a":{"b":[10,20]}}' ->> '$.a.b[0]' as first_elem_json_0_based` | int_=2, dec_=2.50, first_elem_json_0_based='10' |
| J2 silent alias | `select order_id, order_meta:status from o order by order_id` | order_id=1, order_meta='shipped'; order_id=2, order_meta='placed'; order_id=3, order_meta='returned' |
| J2 silent alias | `select order_id, order_meta:status::string from o order by order_id` | order_id=1, order_meta='shipped'; order_id=2, order_meta='placed'; order_id=3, order_meta='returned' |
| J3 misc | `select json_valid('{"a":') as bad, json_object('a', 1, 'b', null) as obj, json_array_length('[1,2,3]') as n, json_type('{"a":null}', '$.a') as null_type` | bad=False, obj='{"a":1,"b":null}', n=3, null_type='NULL' |
| J3 ARRAY_SIZE | `select v, json_array_length(v, '$.arr') as bare, case when json_type(v, '$.arr') = 'ARRAY' then json_array_length(v, '$.arr') end as sf_like from (values ('{"arr":[1,2]}'), ('{"arr":null}'), ('{}')) t(v)` | v='{"arr":[1,2]}', bare=2, sf_like=2; v='{"arr":null}', bare=0, sf_like=NULL; v='{}', bare=NULL, sf_like=NULL |
| J3 TYPEOF | `select json_type('1') as one, json_type('-1') as minus_one, json_type('1.5') as dec, json_type('"s"') as str, json_type('null') as null_` | one='UBIGINT', minus_one='BIGINT', dec='DOUBLE', str='VARCHAR', null_='NULL' |
| F1 FLATTEN | `select w.event_id, f.idx - 1 as item_position, upper(f.item ->> '$.sku') as sku, cast(f.item ->> '$.qty' as decimal(38,0)) as qty, w.payload ->> '$.utm.source' as utm_source from w, unnest(json_extract(w.payload, '$.items[*]')) with ordinality as f(item, idx) order by 1, 2` | event_id=1, item_position=0, sku='AB-1', qty=2, utm_source='google'; event_id=1, item_position=1, sku='CD-2', qty=1, utm_source='google' |
| F1 index base | `select [10,20,30][1] as list_1_based, '[10,20,30]' ->> '$[0]' as json_0_based` | list_1_based=10, json_0_based='10' |
| F2 OUTER | `select w.event_id, f.idx - 1 as item_position from w left join lateral unnest(json_extract(w.payload, '$.items[*]')) with ordinality as f(item, idx) on true order by 1, 2` | event_id=1, item_position=0; event_id=1, item_position=1; event_id=2, item_position=NULL; event_id=3, item_position=NULL |
| F3 object keys | `select unnest(json_keys('{"b":1,"a":2}')) as key` | key='b'; key='a' |
| A1 LISTAGG | `select g, string_agg(distinct channel, ',' order by channel) as l, coalesce(string_agg(distinct channel, ',' order by channel), '') as sf_like from ch group by g order by g` | g=1, l='app,web', sf_like='app,web'; g=2, l=NULL, sf_like='' |
| A2 RATIO_TO_REPORT (float) | `select x, round(x::double / nullif(sum(x::double) over (), 0), 4) as share from (values (1.00::decimal(10,2)), (3.00)) t(x) order by x` | x=1.00, share=0.25; x=3.00, share=0.75 |
| A2 RATIO_TO_REPORT (NUMBER) | `select x, round({{ molinia_round_div('x', 'sum(x) over ()', 8) }}, 4) as share from (values (1.00::decimal(10,2)), (3.00)) t(x) order by x` | x=1.00, share=0.2500; x=3.00, share=0.7500 |
| A3 ARRAY_AGG | `select array_agg(x) as duck, cast(to_json(array_agg(x order by x) filter (where x is not null)) as varchar) as sf_like from (values (2),(null),(1)) t(x)` | duck=[2, NULL, 1], sf_like='[1,2]' |
| A3 ARRAY_AGG | `select cast(to_json(array_agg(distinct x order by x) filter (where x is not null)) as varchar) as sf_like from (values (2),(null),(1),(2)) t(x)` | sf_like='[1,2]' |
| A3 ARRAY_AGG | `select cast(to_json(array_agg(x order by x) filter (where x is not null)) as varchar) as sf_like, cast(array_agg(x order by x) filter (where x is not null) as varchar) as bare_list from (values ('b'),(null),('a')) t(x)` | sf_like='["a","b"]', bare_list='[a, b]' |
| A4 NULL order | `select list(x order by x desc) as duck_desc, list(x order by x desc nulls first) as sf_desc from (values (1),(null),(3)) t(x)` | duck_desc=[3, 1, NULL], sf_desc=[NULL, 3, 1] |
| A5 LAST_VALUE | `select x, last_value(x) over (order by x) as default_frame, last_value(x) over (order by x rows between unbounded preceding and unbounded following) as full_frame from (values (1),(2),(3)) t(x) order by x` | x=1, default_frame=1, full_frame=3; x=2, default_frame=2, full_frame=3; x=3, default_frame=3, full_frame=3 |
| A6 QUALIFY | `select * from (values (1,'a@x.com',timestamp '2025-01-02'),(2,'a@x.com',timestamp '2025-01-01')) t(customer_id, email, signup_ts) qualify row_number() over (partition by email order by signup_ts, customer_id) = 1` | customer_id=2, email='a@x.com', signup_ts=2025-01-01 00:00:00 |
| A7 COUNT_IF | `select count_if(x > 1)::bigint as count_if_, count(*) filter (where x > 1) as filter_ from (values (1),(2),(3)) t(x)` | count_if_=2, filter_=2 |
| A9 syntax | `select * exclude (b) from (select 1 as a, 2 as b)` | a=1 |
| A9 syntax | `select * rename (a as a2) from (select 1 as a, 2 as b)` | a2=1, b=2 |
| X1 regex | `select regexp_extract('abcdef', '[0-9]+') as duck_no_match, nullif(regexp_extract('abcdef', '[0-9]+'), '') as sf_like, regexp_full_match('abc123', '[0-9]') as rlike_anchored, regexp_matches('abc123', '[0-9]') as partial` | duck_no_match='', sf_like=NULL, rlike_anchored=False, partial=True |
| X1 regex | `select regexp_replace('a-b-c', '-', '') as duck_default_first_only, regexp_replace('a-b-c', '-', '', 'g') as sf_like_all` | duck_default_first_only='ab-c', sf_like_all='abc' |
| X1 backslashes | `select regexp_matches('a1', '\\d') as sf_literal_as_is, regexp_matches('a1', '\d') as halved` | sf_literal_as_is=False, halved=True |
| X2 split | `select split_part('a,b,c', ',', 0) as part0, split_part('a,b,c', ',', 1) as part1, split_part('a,b,c', ',', 9) as out_of_range, string_split('a,b,c', ',') as split_` | part0='', part1='a', out_of_range='', split_=['a', 'b', 'c'] |
| X4 HASH | `select md5('abc') as md5_, hash('abc') as duck_hash, typeof(hash('abc')) as t` | md5_='900150983cd24fb0d6963f7d28e17f72', duck_hash=1924864467101078684, t='UBIGINT' |
| X5 identifiers | `select customer_id, email from up` | CUSTOMER_ID=1, EMAIL='x' |
| X5 identifiers | `select column_name from (describe select customer_id, email as email from up)` | column_name='CUSTOMER_ID'; column_name='email' |
| Y1 TOP n | `select order_id from o order by order_id limit 1` | order_id=1 |
| Y4 alias shadows a column | `select coalesce(d, 0) as d, round(q * p * (1 - d * 0.01), 2) as line from (values (2::decimal(38,0), 10.20::decimal(10,2), null::decimal(5,2))) t(q, p, d)` | d=0.00, line=NULL (the base column won, the alias was ignored, no error) |
| Y4 lateral alias, no such column | `select coalesce(d, 0) as disc, round(q * p * (1 - disc * 0.01), 2) as line from (values (2::decimal(38,0), 10.20::decimal(10,2), null::decimal(5,2))) t(q, p, d)` | disc=0.00, line=20.40 (the alias resolved; Snowflake: invalid identifier) |
| Y4 forward reference | `select round(1 - disc, 2) as line, 0 as disc` | **Binder**: Binder Error: Column "disc" referenced that exists in the SELECT clause - but this column cannot be referenced before it is defined |

## Appendix B. Money-math proof (DuckDB vs Python `decimal` ROUND_HALF_UP)

Python's `decimal.ROUND_HALF_UP` rounds ties away from zero, which is
Snowflake's NUMBER rounding. In each grid, "naive" is the DuckDB expression
with `/`, and "idiom" is the rule in this file.

DuckDB 1.5.5; macro read from molinia_round_div.sql

| grid | cases | naive DOUBLE mismatches | idiom mismatches | example of a naive miss |
|---|---|---|---|---|
| G1 line amount, `* 0.01` idiom (N4); 54,526 exact half cents | 599,970 | 7,535 | **0** | 1 x 0.30 x (1 - 5.00/100): 0.28 vs 0.29 |
| G2 `molinia_round_div(r, n, 2, div0=true)` vs exact | 928,590 | 2,096 | **0** | -490.83 / 6: -81.8 vs -81.81 |
| G2 `round(molinia_round_div(r, n, 8, div0=true), 2)` vs Snowflake two-step | 928,590 | 2,096 | **0** |  |
| G3 share of total to 4 dp (A2), 20,000 exact ties | 39,998 | 2,516 | **0** | 198.17 / 200.00: 0.9908 vs 0.9909 |
| G3 percent `round(100 * macro(.., 8), 2)` | 39,998 | 0 | **0** |  |
| G4 DECIMAL(38,6) / DECIMAL(10,2) to 3 dp | 240,040 | 22 | **0** |  |

PASS: every idiom equals Python decimal ROUND_HALF_UP

## Appendix C. dbt-molinia end to end (local fake endpoint)

This run tested the dbt side without touching Molinia:

- **Adapter and profile.** dbt-core 1.12.5 (kit venv) with dbt-molinia 0.1.0 loaded from the source checkout, using the P1 profile block. This first run shows only that the SQL and the materializations work. It says nothing about pacing: dbt accepts unknown profile keys without complaint, so a clean parse does not prove the adapter reads `requests_per_minute` or `retry_max_wait_seconds`. The acme_shop run below used the kit venv's pre-release adapter (commit `aec93568`, section 5), which implements both.
- **Endpoint.** A local HTTP stub answered `POST /api/orgs/{org}/query/execute` from a DuckDB 1.5.5 file. The raw tables sat in `main` with upper-case column names and VARCHAR JSON columns, the way ingest leaves them.
- **Project.** 10 models laid out like acme_shop (5 staging views, 2 intermediate views, 3 mart tables), written with rules P1, P3, P4, S1 to S3, N4, N7, N8, D2 to D5, J1, F1, A1, A2, A6, A7 and the section 4 macro. There were 21 tests, including accepted_values, relationships and a singular test.

Results:

| check | result |
|---|---|
| `dbt parse --target molinia` | OK, with no API calls |
| `dbt build --target molinia`, fresh | PASS=31 (10 models, 21 tests). The models landed in `staging`, `intermediate` and `marts`, as P4 says. |
| HTTP requests: fresh build / rebuild / `-s stg_orders+` | 80 / 74 / 44. On the fresh build, 40 of the 80 were the per-connection `SELECT schema_name … LIMIT 1` check. |
| mart values (average order value, percent of month) vs Python `decimal` two-step | 0 mismatches |
| `dbt ls --resource-type test` on acme_shop (no connection) | 63 tests, 12 of them on sources |

**The acme_shop port by these rules.** A scratch copy of `acme_shop/` was ported
by this rulebook: P1 to P4, the macro, the ported `cents_to_euro`, and every
model and test. It was built with the dbt-molinia installed in the kit venv
(pacing active) against the same kind of stub, loaded with synthetic raw
tables shaped like the Snowflake unload (upper-case columns, `DECIMAL(38,0)`
ids, VARCHAR JSON):

| check | result |
|---|---|
| section 9 greps | only a comment line left (in `cents_to_euro.sql`), no `x:y` paths |
| `dbt build --target molinia` | **PASS=73** (10 models, 63 tests), ERROR=0 |
| HTTP requests / wall time | 164 requests (82 connection checks), 6 min 0 s at `requests_per_minute: 25` |

This proves the rewrites compile and run on DuckDB 1.5.5 through the adapter.
It does not prove parity: there was no Snowflake answer key locally. Parity is
proven on Molinia against `main.sf_*`.

These runs don't cover the real server's authentication, masking views, lake
routing or rate limiting. The live rehearsal covers those.

## Appendix D. Re-run the proof

Save the block below as `verify_money.py` in the kit root and run
`.venv/bin/python verify_money.py`. It reads the macro from the shipped file
`agent/snippets/molinia_round_div.sql`, renders it with jinja2, runs the grids
on local DuckDB and exits 1 on any mismatch. It makes no network calls.

```python
"""Money-math proof for agent/MIGRATION_RULES.md (local DuckDB only, no network).

Reads the molinia_round_div macro from agent/snippets/molinia_round_div.sql,
renders it with jinja2 and compares DuckDB results with Python decimal
ROUND_HALF_UP (ties away from zero = Snowflake NUMBER rounding). Exit 1 on any
idiom mismatch."""
import sys
from decimal import Decimal as D, ROUND_HALF_UP, getcontext
from pathlib import Path
import duckdb, jinja2

getcontext().prec = 60
SNIPPET = Path(__file__).resolve().parent / "agent" / "snippets" / "molinia_round_div.sql"
macro = SNIPPET.read_text(encoding="utf-8")
env = jinja2.Environment()
def M(call):  # render one macro call to SQL
    return env.from_string(macro + "{{ " + call + " }}").render().strip()
def hu(x, places):  # Python decimal, round half away from zero
    return x.quantize(D(1).scaleb(-places), rounding=ROUND_HALF_UP)

con = duckdb.connect()
bad_total = 0
def report(name, n, naive_bad, idiom_bad, example=""):
    global bad_total
    bad_total += idiom_bad
    print(f"| {name} | {n:,} | {naive_bad:,} | **{idiom_bad:,}** | {example} |")

print(f"DuckDB {duckdb.__version__}; macro read from {SNIPPET.name}\n")
print("| grid | cases | naive DOUBLE mismatches | idiom mismatches | example of a naive miss |")
print("|---|---|---|---|---|")

# G1: line amounts ROUND(q * p * (1 - d / 100), 2), p in 0.01..199.99, 6 discounts
con.execute("""create table g1 as select q::decimal(38,0) as q, (p / 100.0)::decimal(10,2) as p, d
  from range(1, 6) a(q), range(1, 20000) b(p),
       (select unnest([0, 5, 10, 12.5, 15, 33.33])::decimal(5,2) as d)""")
rows = con.execute("""select q, p, d, round(q * p * (1 - d / 100), 2), round(q * p * (1 - d * 0.01), 2) from g1""").fetchall()
nb = ib = ties = 0; ex = ""
for q, p, d, naive, idiom in rows:
    exact = D(q) * p * (1 - d / 100); want = hu(exact, 2)
    ties += (exact * 1000) % 10 == 5 and (exact * 100) % 1 != 0
    if D(repr(naive)) != want:
        nb += 1; ex = ex or f"{q} x {p} x (1 - {d}/100): {naive} vs {want}"
    ib += idiom != want
report(f"G1 line amount, `* 0.01` idiom (N4); {ties:,} exact half cents", len(rows), nb, ib, ex)

# G2: revenue / order_count, incl. negatives and n = 0 (DIV0)
con.execute("""create table g2 as select (c / 100.0)::decimal(38,2) as r, n::bigint as n
  from range(-50000, 50001, 7) a(c), range(0, 65) b(n)""")
rows = con.execute(f"""select r, n, round(r / nullif(n, 0), 2),
  {M("molinia_round_div('r', 'n', 2, div0=true)")},
  round({M("molinia_round_div('r', 'n', 8, div0=true)")}, 2) from g2""").fetchall()
nb = ib1 = ib2 = 0; ex = ""
for r, n, naive, one, two in rows:
    if n == 0:
        ib1 += one != 0; ib2 += two != 0; continue
    e = r / D(n)
    if naive is None or D(repr(naive)) != hu(e, 2):
        nb += 1; ex = ex or f"{r} / {n}: {naive} vs {hu(e, 2)}"
    ib1 += one != hu(e, 2)
    ib2 += two != hu(hu(e, 8), 2)
report("G2 `molinia_round_div(r, n, 2, div0=true)` vs exact", len(rows), nb, ib1, ex)
report("G2 `round(molinia_round_div(r, n, 8, div0=true), 2)` vs Snowflake two-step", len(rows), nb, ib2, "")

# G3: shares with exact ties: pairs summing to 200.00 (half of them tie at the 5th decimal)
con.execute("""create table g3 as select g, amt from (
  select i as g, (i / 100.0)::decimal(38,2) as amt from range(1, 20000) t(i) union all
  select i as g, ((20000 - i) / 100.0)::decimal(38,2) from range(1, 20000) t(i))""")
rows = con.execute(f"""select amt, sum(amt) over (partition by g),
  round(amt / sum(amt) over (partition by g), 4),
  {M("molinia_round_div('amt', 'sum(amt) over (partition by g)', 4)")},
  round(100 * {M("molinia_round_div('amt', 'sum(amt) over (partition by g)', 8)")}, 2) from g3""").fetchall()
nb = ib = ibp = 0; ex = ""
for amt, tot, naive, share4, pct2 in rows:
    e = amt / tot
    if D(repr(naive)) != hu(e, 4):
        nb += 1; ex = ex or f"{amt} / {tot}: {naive} vs {hu(e, 4)}"
    ib += share4 != hu(e, 4)
    ibp += pct2 != hu(100 * hu(e, 8), 2)
report("G3 share of total to 4 dp (A2), 20,000 exact ties", len(rows), nb, ib, ex)
report("G3 percent `round(100 * macro(.., 8), 2)`", len(rows), 0, ibp, "")

# G4: DECIMAL(38,6) / DECIMAL(10,2) to 3 dp (non-integer divisors)
con.execute("""create table g4 as select (a * 0.000137)::decimal(38,6) as x, (b / 4.0)::decimal(10,2) as y
  from range(-3000, 3001) s(a), range(1, 41) t(b)""")
rows = con.execute(f"select x, y, round(x / y, 3), {M(chr(39).join(['molinia_round_div(', 'x', ', ', 'y', ', 3)']))} from g4").fetchall()
nb = sum(D(repr(n)) != hu(x / y, 3) for x, y, n, m in rows)
ib = sum(m != hu(x / y, 3) for x, y, n, m in rows)
report("G4 DECIMAL(38,6) / DECIMAL(10,2) to 3 dp", len(rows), nb, ib, "")

print("\nPASS: every idiom equals Python decimal ROUND_HALF_UP" if bad_total == 0 else f"\nFAIL: {bad_total} idiom mismatches")
sys.exit(1 if bad_total else 0)
```
