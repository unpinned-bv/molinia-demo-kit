-- 01_raw_data.sql: generate the Acme Shop raw tables in MOLINIA_DEMO.RAW.
-- Run with: tools/sf.py run setup/snowflake/01_raw_data.sql   (after 00_setup.sql)
--
-- Fully deterministic: ids come from ROW_NUMBER() OVER (ORDER BY SEQ4()) over
-- TABLE(GENERATOR(ROWCOUNT => n)) (SEQ4 alone may have gaps), and every
-- attribute is derived from the id with arithmetic and HASH(...). No RANDOM,
-- UUID_STRING or CURRENT_*; running this twice produces identical tables.
-- HASH always gets ONE string, '<purpose>:<id>[:<n>]', so draws made for
-- different purposes are independent of each other (a multi-argument HASH is
-- free to combine its inputs linearly, which would correlate them).
--
-- Planted edge cases (what the dbt project has to cope with):
--   CUSTOMERS      every 50th customer is a second signup of customer id-25:
--                  same person, e-mail differing only in case/whitespace,
--                  later SIGNUP_TS; lowercase country codes; NULL opt-in;
--                  names and e-mails with surrounding spaces.
--   PRODUCTS       NULL UNIT_PRICE (every 37th); lowercase SKUs (every 11th);
--                  every 3rd price is a multiple of 8 cents plus 4 cents, so
--                  quantity x price x 0.875 lands exactly on a half cent.
--   ORDERS         STATUS in mixed case, some with a trailing space;
--                  ORDER_META with missing keys, JSON nulls, and an
--                  unparseable shipping cost ('n/a').
--   ORDER_ITEMS    DISCOUNT_PCT in (0, 5, 10, 12.5, 15, 33.33); every 499th
--                  order has no lines at all.
--   PAYMENTS       every 20th order (and every order without lines) is unpaid;
--                  about 10% of paid orders have a failed attempt first;
--                  returned orders carry a refund.
--   WEB_EVENTS     about 30% anonymous (NULL CUSTOMER_ID); page views have an
--                  empty items array or no items key; some utm objects are
--                  missing, some campaigns are JSON null; some SKUs lowercase
--                  or unknown to the catalogue.

USE ROLE SYSADMIN;
USE WAREHOUSE DEMO_WH;
USE DATABASE MOLINIA_DEMO;
USE SCHEMA RAW;

-------------------------------------------------------------------------------
-- CUSTOMERS (2,000)
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.CUSTOMERS AS
WITH ids AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS id
    FROM TABLE(GENERATOR(ROWCOUNT => 2000))
),
base AS (
    -- src_id: the person behind the row. Every 50th row is a duplicate signup
    -- of the person 25 ids earlier (whose own id is never a multiple of 50).
    SELECT
        id,
        IFF(MOD(id, 50) = 0, id - 25, id) AS src_id
    FROM ids
),
person AS (
    SELECT
        id,
        src_id,
        SPLIT_PART(
            'Anna,Bram,Chloe,Daan,Emma,Finn,Greta,Hugo,Iris,Jens,Katja,Lars,Mila,Noah,Olga,Pieter,Quinn,Rosa,Sven,Tess,Umar,Vera,Wout,Xenia,Yara,Zeno',
            ',', MOD(ABS(HASH('first_name:' || src_id)), 26) + 1)                 AS first_name,
        SPLIT_PART(
            'de Vries,Jansen,Bakker,Visser,Smit,Meyer,Schmidt,Dubois,Rossi,Novak,Nielsen,Kowalski,Garcia,Leroy,Weber,Fischer,Moreau,Horvat,Svensson,Martin',
            ',', MOD(ABS(HASH('last_name:' || src_id)), 20) + 1)                  AS last_name,
        SPLIT_PART('example.com,example.net,example.org',
            ',', MOD(ABS(HASH('domain:' || src_id)), 3) + 1)                      AS email_domain,
        SPLIT_PART('NL,NL,NL,NL,NL,DE,DE,DE,DE,BE,BE,BE,FR,FR,AT,DK,SE,IT,ES,PL',
            ',', MOD(ABS(HASH('country:' || src_id)), 20) + 1)                    AS country,
        DATEADD(second, MOD(ABS(HASH('signup:' || src_id)), 912 * 86400),
            '2023-01-01 00:00:00'::TIMESTAMP_NTZ)                                 AS first_signup_ts
    FROM base
),
shaped AS (
    SELECT
        id,
        src_id,
        first_name,
        last_name,
        LOWER(first_name) || '.' || REPLACE(LOWER(last_name), ' ', '') || src_id || '@' || email_domain AS email_clean,
        country,
        IFF(id = src_id,
            first_signup_ts,
            DATEADD(day, 30 + MOD(id, 200), first_signup_ts))                     AS signup_ts
    FROM person
)
SELECT
    id::NUMBER(38,0)                                                              AS CUSTOMER_ID,
    IFF(MOD(id, 23) = 0, ' ' || first_name || ' ', first_name)::VARCHAR           AS FIRST_NAME,
    IFF(MOD(id, 31) = 0, last_name || '  ', last_name)::VARCHAR                   AS LAST_NAME,
    (CASE
        WHEN id <> src_id AND MOD(id, 100) = 0 THEN UPPER(email_clean)
        WHEN id <> src_id                      THEN '  ' || email_clean || ' '
        WHEN MOD(id, 37) = 0                   THEN UPPER(LEFT(email_clean, 1)) || SUBSTR(email_clean, 2)
        ELSE email_clean
    END)::VARCHAR                                                                 AS EMAIL,
    IFF(MOD(id, 17) = 0, LOWER(country), country)::VARCHAR(2)                     AS COUNTRY_CODE,
    signup_ts::TIMESTAMP_NTZ                                                      AS SIGNUP_TS,
    IFF(MOD(id, 13) = 0, NULL,
        MOD(ABS(HASH('opt_in:' || id)), 3) = 0)::BOOLEAN                          AS MARKETING_OPT_IN
FROM shaped
ORDER BY id;

-------------------------------------------------------------------------------
-- CUSTOMER_CONTACTS (2,000) - PII; masked in Molinia, never read by a model
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.CUSTOMER_CONTACTS AS
WITH ids AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS id
    FROM TABLE(GENERATOR(ROWCOUNT => 2000))
)
SELECT
    id::NUMBER(38,0)                                                              AS CUSTOMER_ID,
    IFF(MOD(id, 19) = 0, NULL,
        '+31 6 ' || LPAD(TO_VARCHAR(MOD(ABS(HASH('phone:' || id)), 100000000)), 8, '0'))::VARCHAR AS PHONE,
    ('NL' || LPAD(TO_VARCHAR(MOD(ABS(HASH('iban_check:' || id)), 90) + 10), 2, '0')
        || 'DEMO' || LPAD(TO_VARCHAR(MOD(ABS(HASH('iban:' || id)), 10000000000)), 10, '0'))::VARCHAR AS IBAN,
    DATEADD(day, MOD(ABS(HASH('dob:' || id)), 18250), '1950-01-01'::DATE)::DATE   AS DATE_OF_BIRTH
FROM ids
ORDER BY id;

-------------------------------------------------------------------------------
-- PRODUCTS (150)
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.PRODUCTS AS
WITH ids AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS id
    FROM TABLE(GENERATOR(ROWCOUNT => 150))
),
coded AS (
    SELECT
        id,
        SPLIT_PART('EL,HO,SP,BO,TO', ',', MOD(ABS(HASH('category:' || id)), 5) + 1) AS category_code,
        199 + MOD(ABS(HASH('price:' || id)), 25000)                               AS raw_cents
    FROM ids
),
priced AS (
    SELECT
        id,
        category_code,
        -- Every 3rd product costs 8k + 4 cents: at 12.5% discount an odd
        -- quantity lands on an exact half cent (the rounding trap).
        IFF(MOD(id, 3) = 0, raw_cents - MOD(raw_cents, 8) + 4, raw_cents) AS price_cents
    FROM coded
)
SELECT
    id::NUMBER(38,0)                                                              AS PRODUCT_ID,
    IFF(MOD(id, 11) = 0,
        LOWER(category_code) || '-' || LPAD(TO_VARCHAR(id), 5, '0'),
        category_code || '-' || LPAD(TO_VARCHAR(id), 5, '0'))::VARCHAR            AS SKU,
    (SPLIT_PART('Nordic,Classic,Compact,Deluxe,Eco,Urban,Coastal,Alpine',
            ',', MOD(ABS(HASH('adjective:' || id)), 8) + 1)
        || ' '
        || SPLIT_PART(
            DECODE(category_code,
                'EL', 'Wireless Earbuds,USB-C Charger,Smart Speaker,Desk Lamp,Power Bank,Bluetooth Keyboard',
                'HO', 'Linen Duvet,Ceramic Mug,Cast Iron Pan,Wool Throw,Glass Carafe,Bamboo Tray',
                'SP', 'Yoga Mat,Trail Socks,Water Bottle,Resistance Band,Running Cap,Foam Roller',
                'BO', 'Field Guide,Cookbook,Travel Journal,Poetry Collection,Atlas,Crime Novel',
                'TO', 'Wooden Train,Puzzle Cube,Plush Fox,Building Blocks,Kite,Board Game'),
            ',', MOD(ABS(HASH('noun:' || id)), 6) + 1)
        || IFF(MOD(id, 13) = 0, ' ', ''))::VARCHAR                                AS PRODUCT_NAME,
    category_code::VARCHAR(2)                                                     AS CATEGORY_CODE,
    IFF(MOD(id, 37) = 0, NULL, price_cents / 100)::NUMBER(10,2)                   AS UNIT_PRICE,
    (MOD(id, 10) <> 0)::BOOLEAN                                                   AS IS_ACTIVE
FROM priced
ORDER BY id;

-------------------------------------------------------------------------------
-- ORDERS (20,000)
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.ORDERS AS
WITH ids AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS id
    FROM TABLE(GENERATOR(ROWCOUNT => 20000))
),
buckets AS (
    SELECT
        id,
        -- Customers 1851-2000 never order (prospects).
        MOD(ABS(HASH('customer:' || id)), 1850) + 1                               AS customer_id,
        -- 2025-01-01 00:00:00 .. 2026-06-30 23:59:59 (546 days).
        DATEADD(second, MOD(ABS(HASH('order_ts:' || id)), 546 * 86400),
            '2025-01-01 00:00:00'::TIMESTAMP_NTZ)                                 AS order_ts,
        MOD(ABS(HASH('status:' || id)), 20)                                       AS status_bucket,
        MOD(ABS(HASH('channel:' || id)), 20)                                      AS channel_bucket,
        MOD(ABS(HASH('coupon:' || id)), 10)                                       AS coupon_bucket,
        MOD(ABS(HASH('device:' || id)), 3)                                        AS device_bucket,
        MOD(ABS(HASH('shipping:' || id)), 10)                                     AS shipping_bucket,
        MOD(ABS(HASH('gift:' || id)), 10)                                         AS gift_bucket
    FROM ids
),
coded AS (
    SELECT
        *,
        CASE
            WHEN status_bucket < 2  THEN 'placed'
            WHEN status_bucket < 5  THEN 'shipped'
            WHEN status_bucket < 17 THEN 'delivered'
            WHEN status_bucket < 19 THEN 'cancelled'
            ELSE 'returned'
        END                                                                       AS status_clean,
        CASE
            WHEN channel_bucket < 11 THEN 'web'
            WHEN channel_bucket < 17 THEN 'app'
            ELSE 'marketplace'
        END                                                                       AS channel,
        IFF(shipping_bucket < 8, 'standard', 'express')                           AS shipping_method,
        CASE
            WHEN MOD(id, 97) = 0     THEN 'n/a'      -- unparseable on purpose
            WHEN shipping_bucket < 7 THEN '4.95'
            WHEN shipping_bucket < 8 THEN '0.00'     -- free standard shipping
            ELSE '9.95'
        END                                                                       AS shipping_cost
    FROM buckets
)
SELECT
    id::NUMBER(38,0)                                                              AS ORDER_ID,
    customer_id::NUMBER(38,0)                                                     AS CUSTOMER_ID,
    order_ts::TIMESTAMP_NTZ                                                       AS ORDER_TS,
    (CASE
        WHEN MOD(id, 7) = 0  THEN UPPER(status_clean)
        WHEN MOD(id, 11) = 0 THEN UPPER(LEFT(status_clean, 1)) || SUBSTR(status_clean, 2)
        WHEN MOD(id, 29) = 0 THEN status_clean || ' '
        ELSE status_clean
    END)::VARCHAR                                                                 AS STATUS,
    channel::VARCHAR                                                              AS CHANNEL,
    -- OBJECT_CONSTRUCT omits a key whose value is SQL NULL (a missing key);
    -- PARSE_JSON('null') keeps the key with a JSON null.
    OBJECT_CONSTRUCT(
        'coupon', CASE
                      WHEN coupon_bucket < 6 THEN NULL
                      WHEN coupon_bucket < 8 THEN PARSE_JSON('null')
                      ELSE TO_VARIANT(SPLIT_PART('WELCOME10,SUMMER25,VIP5,FREESHIP', ',', MOD(id, 4) + 1))
                  END,
        'device', IFF(MOD(id, 25) = 0, NULL,
                      SPLIT_PART('ios,android,desktop', ',', device_bucket + 1)),
        'shipping', IFF(MOD(id, 40) = 0, NULL,
                        OBJECT_CONSTRUCT('method', shipping_method, 'cost', shipping_cost)),
        'gift', IFF(MOD(id, 9) = 0, NULL, gift_bucket = 0)
    )::VARIANT                                                                    AS ORDER_META
FROM coded
ORDER BY id;

-------------------------------------------------------------------------------
-- ORDER_ITEMS (~55,000)
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.ORDER_ITEMS AS
WITH slots AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS line_no
    FROM TABLE(GENERATOR(ROWCOUNT => 5))
),
sizes AS (
    SELECT
        ORDER_ID AS order_id,
        -- 1-5 lines (average 2.75); every 499th order has none.
        IFF(MOD(ORDER_ID, 499) = 0, 0,
            CASE
                WHEN MOD(ABS(HASH('lines:' || ORDER_ID)), 20) < 4  THEN 1
                WHEN MOD(ABS(HASH('lines:' || ORDER_ID)), 20) < 9  THEN 2
                WHEN MOD(ABS(HASH('lines:' || ORDER_ID)), 20) < 14 THEN 3
                WHEN MOD(ABS(HASH('lines:' || ORDER_ID)), 20) < 18 THEN 4
                ELSE 5
            END)                                                                  AS n_lines
    FROM MOLINIA_DEMO.RAW.ORDERS
),
lines AS (
    SELECT
        s.order_id,
        l.line_no,
        MOD(ABS(HASH('product:' || s.order_id || ':' || l.line_no)), 150) + 1     AS product_id,
        MOD(ABS(HASH('quantity:' || s.order_id || ':' || l.line_no)), 20)         AS quantity_bucket,
        MOD(ABS(HASH('discount:' || s.order_id || ':' || l.line_no)), 20)         AS discount_bucket
    FROM sizes s
    JOIN slots l
      ON l.line_no <= s.n_lines
)
SELECT
    ROW_NUMBER() OVER (ORDER BY li.order_id, li.line_no)::NUMBER(38,0)            AS ORDER_ITEM_ID,
    li.order_id::NUMBER(38,0)                                                     AS ORDER_ID,
    li.product_id::NUMBER(38,0)                                                   AS PRODUCT_ID,
    (CASE
        WHEN li.quantity_bucket < 12 THEN 1
        WHEN li.quantity_bucket < 16 THEN 2
        WHEN li.quantity_bucket < 19 THEN 3
        ELSE 5
    END)::NUMBER(38,0)                                                            AS QUANTITY,
    -- Price at order time; products without a list price had a legacy price.
    NVL(p.UNIT_PRICE,
        (500 + MOD(ABS(HASH('legacy_price:' || li.product_id)), 5000)) / 100)::NUMBER(10,2) AS UNIT_PRICE,
    (CASE
        WHEN li.discount_bucket < 10 THEN 0
        WHEN li.discount_bucket < 13 THEN 5
        WHEN li.discount_bucket < 15 THEN 10
        WHEN li.discount_bucket < 17 THEN 12.5
        WHEN li.discount_bucket < 19 THEN 15
        ELSE 33.33
    END)::NUMBER(5,2)                                                             AS DISCOUNT_PCT
FROM lines li
JOIN MOLINIA_DEMO.RAW.PRODUCTS p
  ON p.PRODUCT_ID = li.product_id
ORDER BY ORDER_ITEM_ID;

-------------------------------------------------------------------------------
-- PAYMENTS (~22,000)
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.PAYMENTS AS
WITH item_totals AS (
    SELECT
        ORDER_ID AS order_id,
        SUM(ROUND(QUANTITY * UNIT_PRICE * (1 - DISCOUNT_PCT / 100), 2)) AS items_amount
    FROM MOLINIA_DEMO.RAW.ORDER_ITEMS
    GROUP BY ORDER_ID
),
paid_orders AS (
    SELECT
        o.ORDER_ID                                                                AS order_id,
        o.ORDER_TS                                                                AS order_ts,
        LOWER(TRIM(o.STATUS))                                                     AS status,
        t.items_amount
            + NVL(TRY_TO_NUMBER(o.ORDER_META:shipping.cost::STRING, 10, 2), 0)    AS total,
        MOD(ABS(HASH('retry:' || o.ORDER_ID)), 10) = 0                            AS has_failed_attempt
    FROM MOLINIA_DEMO.RAW.ORDERS o
    JOIN item_totals t
      ON t.order_id = o.ORDER_ID
    -- Every 20th order is unpaid; orders without lines are never paid.
    WHERE MOD(o.ORDER_ID, 20) <> 0
),
attempts AS (
    SELECT order_id, 1 AS attempt_no, 'failed' AS status, total,
           DATEADD(minute, 2, order_ts) AS paid_ts
    FROM paid_orders
    WHERE has_failed_attempt
    UNION ALL
    SELECT order_id, 2 AS attempt_no, 'success' AS status, total,
           DATEADD(minute, IFF(has_failed_attempt, 9, 3), order_ts) AS paid_ts
    FROM paid_orders
    UNION ALL
    SELECT order_id, 3 AS attempt_no, 'refunded' AS status, total,
           DATEADD(day, 14, order_ts) AS paid_ts
    FROM paid_orders
    WHERE status = 'returned'
)
SELECT
    ROW_NUMBER() OVER (ORDER BY order_id, attempt_no)::NUMBER(38,0)               AS PAYMENT_ID,
    order_id::NUMBER(38,0)                                                        AS ORDER_ID,
    SPLIT_PART('ideal,card,paypal,klarna,bancontact',
        ',', MOD(ABS(HASH('method:' || order_id || ':' || attempt_no)), 5) + 1)::VARCHAR  AS PAYMENT_METHOD,
    (total * 100)::NUMBER(38,0)                                                   AS AMOUNT_CENTS,
    status::VARCHAR                                                               AS STATUS,
    paid_ts::TIMESTAMP_NTZ                                                        AS PAID_TS
FROM attempts
ORDER BY PAYMENT_ID;

-------------------------------------------------------------------------------
-- WEB_EVENTS (15,000)
-------------------------------------------------------------------------------
CREATE OR REPLACE TABLE MOLINIA_DEMO.RAW.WEB_EVENTS AS
WITH ids AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS id
    FROM TABLE(GENERATOR(ROWCOUNT => 15000))
),
events AS (
    SELECT
        id                                                                        AS event_id,
        IFF(MOD(ABS(HASH('anonymous:' || id)), 10) < 3, NULL,
            MOD(ABS(HASH('customer:' || id)), 2000) + 1)                          AS customer_id,
        DATEADD(second, MOD(ABS(HASH('event_ts:' || id)), 546 * 86400),
            '2025-01-01 00:00:00'::TIMESTAMP_NTZ)                                 AS event_ts,
        CASE
            WHEN MOD(ABS(HASH('event_type:' || id)), 20) < 8  THEN 'page_view'
            WHEN MOD(ABS(HASH('event_type:' || id)), 20) < 14 THEN 'product_view'
            WHEN MOD(ABS(HASH('event_type:' || id)), 20) < 18 THEN 'add_to_cart'
            ELSE 'checkout'
        END                                                                       AS event_type,
        MOD(ABS(HASH('item_count:' || id)), 4)                                    AS item_bucket,
        MOD(ABS(HASH('utm:' || id)), 10)                                          AS utm_bucket
    FROM ids
),
sized AS (
    SELECT
        *,
        CASE event_type
            WHEN 'page_view'    THEN 0
            WHEN 'product_view' THEN 1
            WHEN 'add_to_cart'  THEN 1 + MOD(item_bucket, 2)
            ELSE 1 + item_bucket
        END                                                                       AS n_items
    FROM events
),
slots AS (
    SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) AS item_no
    FROM TABLE(GENERATOR(ROWCOUNT => 4))
),
items AS (
    SELECT
        e.event_id,
        k.item_no,
        OBJECT_CONSTRUCT(
            'sku', CASE
                       WHEN MOD(ABS(HASH('ghost:' || e.event_id || ':' || k.item_no)), 60) = 0
                           THEN 'XX-' || LPAD(TO_VARCHAR(MOD(ABS(HASH('ghost_sku:' || e.event_id || ':' || k.item_no)), 100000)), 5, '0')
                       WHEN MOD(ABS(HASH('case:' || e.event_id || ':' || k.item_no)), 7) = 0
                           THEN LOWER(p.SKU)
                       ELSE p.SKU
                   END,
            'qty', 1 + MOD(ABS(HASH('qty:' || e.event_id || ':' || k.item_no)), 3)
        )                                                                         AS item
    FROM sized e
    JOIN slots k
      ON k.item_no <= e.n_items
    JOIN MOLINIA_DEMO.RAW.PRODUCTS p
      ON p.PRODUCT_ID = MOD(ABS(HASH('product:' || e.event_id || ':' || k.item_no)), 150) + 1
),
item_arrays AS (
    SELECT event_id, ARRAY_AGG(item) WITHIN GROUP (ORDER BY item_no) AS items
    FROM items
    GROUP BY event_id
)
SELECT
    e.event_id::NUMBER(38,0)                                                      AS EVENT_ID,
    e.customer_id::NUMBER(38,0)                                                   AS CUSTOMER_ID,
    e.event_ts::TIMESTAMP_NTZ                                                     AS EVENT_TS,
    e.event_type::VARCHAR                                                         AS EVENT_TYPE,
    OBJECT_CONSTRUCT(
        'session_id', 's_' || TO_VARCHAR(100000 + MOD(ABS(HASH('session:' || e.event_id)), 900000)),
        -- Half of the page views have no items key at all, the other half [].
        'items', IFF(e.event_type = 'page_view' AND MOD(e.event_id, 2) = 0, NULL,
                     NVL(a.items, ARRAY_CONSTRUCT())),
        'utm', IFF(e.utm_bucket < 2, NULL,
                   OBJECT_CONSTRUCT(
                       'source', SPLIT_PART('google,newsletter,instagram,partner,google,newsletter,google,partner',
                                     ',', e.utm_bucket - 1),
                       'campaign', IFF(MOD(ABS(HASH('campaign:' || e.event_id)), 4) = 0,
                                       PARSE_JSON('null'),
                                       TO_VARIANT(SPLIT_PART('spring_sale,summer_sale,black_friday,brand',
                                                      ',', MOD(ABS(HASH('campaign_name:' || e.event_id)), 4) + 1)))))
    )::VARIANT                                                                    AS PAYLOAD
FROM sized e
LEFT JOIN item_arrays a
  ON a.event_id = e.event_id
ORDER BY EVENT_ID;

-- Quick look at what was generated.
SELECT 'CUSTOMERS' AS table_name, COUNT(*) AS row_count FROM MOLINIA_DEMO.RAW.CUSTOMERS
UNION ALL SELECT 'CUSTOMER_CONTACTS', COUNT(*) FROM MOLINIA_DEMO.RAW.CUSTOMER_CONTACTS
UNION ALL SELECT 'PRODUCTS', COUNT(*) FROM MOLINIA_DEMO.RAW.PRODUCTS
UNION ALL SELECT 'ORDERS', COUNT(*) FROM MOLINIA_DEMO.RAW.ORDERS
UNION ALL SELECT 'ORDER_ITEMS', COUNT(*) FROM MOLINIA_DEMO.RAW.ORDER_ITEMS
UNION ALL SELECT 'PAYMENTS', COUNT(*) FROM MOLINIA_DEMO.RAW.PAYMENTS
UNION ALL SELECT 'WEB_EVENTS', COUNT(*) FROM MOLINIA_DEMO.RAW.WEB_EVENTS
ORDER BY table_name;
