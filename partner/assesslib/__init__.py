"""Library behind partner/assess.py — the Snowflake → Molinia scoping assessment.

Modules:
  facts       the verified Molinia facts the report is allowed to assert (2026-09-20)
  rules       the Snowflake-only construct table, derived from agent/MIGRATION_RULES.md
  classify    the four migration classes and how an object lands in one
  scan        static scan of a dbt project (no dbt run, no connection)
  snowflake_inventory  read-only Snowflake metadata probes
  plan        data-movement plan, parity plan, API-request projection
  report      Markdown and JSON rendering

Nothing in here calls Molinia. Nothing in here writes to Snowflake.
"""

__version__ = "1.0.0"
