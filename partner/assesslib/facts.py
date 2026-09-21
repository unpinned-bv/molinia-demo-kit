"""The verified facts this assessment is allowed to assert.

Every limitation the report prints cites one of these ids. They were established
by code reads and live runs on 2026-09-19/20 against Molinia dev and prod. If a
statement about Molinia is not in this table, the report must not make it, or it
must mark itself `inference`.

Keep this file and the report honest together: when a fact changes, change it
here and the wording changes everywhere it is cited.
"""
from __future__ import annotations

from typing import Dict, NamedTuple

AS_OF = "2026-09-20"


class Fact(NamedTuple):
    id: str
    text: str


_FACTS = [
    # --- the migration path -------------------------------------------------
    Fact("F-API", "The migration path is the REST API: POST /api/orgs/{org}/query/execute "
                  "(one statement per request, service-account key) and "
                  "POST /api/orgs/{org}/datasources/{id}/ingest."),
    Fact("F-MCP", "MCP is a separate read-only surface (7 tools), enabled on prod, disabled on dev."),
    Fact("F-PGWIRE", "pgwire exists in the code but is disabled everywhere (PGWIRE_ENABLED is set in no "
                     "deploy file), so BI tools cannot connect over the Postgres wire protocol today."),
    Fact("F-NOREADER", "There is no Snowflake reader and no Iceberg read support. Data moves by COPY INTO "
                       "to object storage, then ingest."),
    Fact("F-INGEST", "Ingest always lands in schema `main`, and its default mode is "
                     "CREATE OR REPLACE TABLE."),
    Fact("F-UNLOAD", "Snowflake unloads TIMESTAMP_NTZ as UTC-adjusted Parquet timestamps, and VARIANT via "
                     "TO_JSON as text."),

    # --- dbt ----------------------------------------------------------------
    Fact("F-DBT-OK", "dbt-molinia works for table and view models and for tests (63 passing live)."),
    Fact("F-DBT-NO", "dbt-molinia does NOT support seeds (bind parameters refused), snapshots (no session "
                     "state between statements), or the dbt docs catalog."),
    Fact("F-DBT-INC", "Incremental is implemented as delete+insert through a _dbt_internal staging schema, "
                      "but it has never been proven on a live server."),
    Fact("F-DBT-NAMES", "Sources render two-part; custom schemas are used as-is, with no target prefix."),
    Fact("F-ADAPTER", "The adapter with rate-limit pacing/backoff and correct result typing exists only on "
                      "the local branch feat/dbt-molinia-429-backoff. The released @molinia/cli and the dbt "
                      "adapter on main lack it. dbt-molinia is not on PyPI: install from the repo."),

    # --- limits -------------------------------------------------------------
    Fact("F-RATE", "Rate limits: 60 HTTP requests/min per client IP for the whole API; 30 org-engine "
                   "queries/min on a free-plan org, 300 on a paid plan with a valid mandate; 3600 "
                   "engine-seconds per UTC day on free."),

    # --- not portable -------------------------------------------------------
    Fact("F-NOTPORT", "Not portable today: Snowflake Scripting control flow (procedures are statement "
                      "batches), Python models/UDFs, TASK -> CALL procedure orchestration (CALL resolves "
                      "only on the single-statement query path, not in scheduled tasks or pipeline steps), "
                      "UNDROP, CLONE, SQL GRANT, and direct reads of Snowflake or Iceberg."),

    # --- governance ---------------------------------------------------------
    Fact("F-MASK-MAIN", "Masking and RLS views only resolve tables in schema `main`."),
    Fact("F-MASK-SA", "A service account can never hold column:unmask, so a build key reading a masked "
                      "column writes masked values."),
    Fact("F-RLS-BREAK", "An RLS policy naming a table that does not exist breaks every engine query in the "
                        "org."),
    Fact("F-AUDIT", "Audit logs every service-account request with a hash chain. Query History shows no "
                    "principal."),

    # --- positioning --------------------------------------------------------
    Fact("F-POSITION", "Positioning: EU alternative, NOT cheaper (per vCPU-hour Molinia is pricier than "
                       "Snowflake). App and control plane run on IONOS in Frankfurt; warehouse compute and "
                       "lake storage on Leafcloud in Amsterdam: EU-owned infrastructure in NL and DE."),

    # --- the calibration anchor --------------------------------------------
    Fact("F-ANCHOR", "Proven on dev on 2026-09-19/20: an agent ported a 10-model, 63-test Snowflake dbt "
                     "project cold and reached 10/10 parity, twice — 35 minutes, then 13 minutes after kit "
                     "improvements. Parity compares every model against Snowflake output unloaded as an "
                     "answer key, returning counts only."),
    Fact("F-BUILD-COST", "Measured request cost of that project: a full `dbt build` (10 models, 63 tests) "
                         "took 164 HTTP requests — 82 of them connection checks — and 6 minutes at 25 "
                         "requests/minute pacing."),
]

FACTS: Dict[str, Fact] = {f.id: f for f in _FACTS}


def fact(fact_id: str) -> str:
    """The text of a verified fact. KeyError if the id is unknown, on purpose:
    a citation that does not resolve is a bug, not a footnote."""
    return FACTS[fact_id].text


def cite(fact_id: str) -> str:
    """`[F-RATE]` — the marker the Markdown report prints next to a claim."""
    if fact_id not in FACTS:
        raise KeyError(fact_id)
    return f"[{fact_id}]"
