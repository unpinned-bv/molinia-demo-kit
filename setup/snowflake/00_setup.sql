-- 00_setup.sql: one-time Snowflake objects for the Acme Shop demo.
-- Idempotent: safe to run again. Run with: tools/sf.py run setup/snowflake/00_setup.sql
--
-- Trial credits are real money, so the warehouse is the smallest size and
-- suspends after 60 seconds idle.

USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS DEMO_WH
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    COMMENT = 'Molinia partner demo (acme_shop); XSMALL, suspends after 60 s';

-- Re-assert the cost settings in case the warehouse already existed with others.
ALTER WAREHOUSE DEMO_WH SET
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE;

USE WAREHOUSE DEMO_WH;

CREATE DATABASE IF NOT EXISTS MOLINIA_DEMO
    COMMENT = 'Molinia partner demo: Acme Shop raw data and dbt output';

USE DATABASE MOLINIA_DEMO;

-- Raw, generated source data (01_raw_data.sql).
CREATE SCHEMA IF NOT EXISTS MOLINIA_DEMO.RAW;

-- dbt target schema; dbt derives ANALYTICS_STAGING, ANALYTICS_INTERMEDIATE and
-- ANALYTICS_MARTS from it and creates those itself.
CREATE SCHEMA IF NOT EXISTS MOLINIA_DEMO.ANALYTICS;

-- Internal stage for the Parquet unload (02_unload.sql). PUBLIC exists in
-- every new database.
CREATE STAGE IF NOT EXISTS MOLINIA_DEMO.PUBLIC.MOLINIA_EXPORT
    FILE_FORMAT = (TYPE = PARQUET)
    COMMENT = 'Parquet export of raw tables and dbt model outputs for the Molinia migration demo';

USE SCHEMA MOLINIA_DEMO.RAW;
