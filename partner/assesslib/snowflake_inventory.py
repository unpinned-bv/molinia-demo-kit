"""Read-only Snowflake metadata probes.

Rules this module keeps:
  * it never writes: no CREATE, ALTER, DROP, INSERT, COPY, CALL, USE;
  * INFORMATION_SCHEMA and SHOW first, because they are metadata reads with no
    table scan; ACCOUNT_USAGE only when the caller passes --sample-queries;
  * every probe is independent. One that fails (a missing view on an older
    edition, a role without a grant) is recorded as a warning and the rest of
    the inventory still lands. A scope report that silently omits the tasks is
    worse than one that says "tasks: could not read".
  * a wall-clock budget, so an assessment on a large account cannot turn into a
    long-running session on the client's warehouse.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

SF_VARS = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE",
           "SNOWFLAKE_DATABASE", "SNOWFLAKE_PRIVATE_KEY_PATH")

_WRITE = re.compile(r"^\s*(create|alter|drop|insert|update|delete|merge|copy|call|use|grant|revoke"
                    r"|truncate|undrop|put|get|remove|execute)\b", re.IGNORECASE)


class SnowflakeError(RuntimeError):
    """Connection or configuration problem. Never contains a secret."""


# --------------------------------------------------------------------------- env


def parse_env_file(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines, `export ` prefixes, quotes and comments. Never logs."""
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def load_profile(profile: Optional[str], kit_root: Optional[Path] = None) -> List[str]:
    """Put a profile's settings in the environment. Returns the NAMES loaded.

    `profile` is either a path to an env file, or a name resolved against the
    kit's `.secrets/<name>.env`. Without one, the kit's `.secrets/*.env` and
    whatever is already exported are used, exactly like `tools/sf.py`.

    An explicitly named profile OVERRIDES whatever is already exported: asking
    for a client's profile and silently getting a stale SNOWFLAKE_ACCOUNT from
    the shell is how you inventory the wrong account. The implicit default keeps
    the kit's behaviour, where the environment wins so CI can override one
    setting without editing a file.
    """
    candidates: List[Path] = []
    if profile:
        p = Path(os.path.expanduser(profile))
        if p.is_file():
            candidates = [p]
        elif kit_root is not None:
            named = kit_root / ".secrets" / f"{profile}.env"
            if named.is_file():
                candidates = [named]
            else:
                raise SnowflakeError(
                    f"--snowflake-profile {profile!r} is neither a readable file nor "
                    f"{named} (expected SNOWFLAKE_ACCOUNT, _USER, _ROLE, _WAREHOUSE, "
                    f"_PRIVATE_KEY_PATH)")
        else:
            raise SnowflakeError(f"--snowflake-profile {profile!r} is not a readable file")
    elif kit_root is not None and (kit_root / ".secrets").is_dir():
        candidates = sorted((kit_root / ".secrets").glob("*.env"))
    override = bool(profile)
    loaded: List[str] = []
    for f in candidates:
        for key, value in parse_env_file(f).items():
            if key.startswith("SNOWFLAKE_") and (override or key not in os.environ):
                os.environ[key] = value
                loaded.append(key)
    return loaded


def missing_settings(*names: str) -> List[str]:
    return [n for n in names if not (os.environ.get(n) or "").strip()]


# --------------------------------------------------------------------------- connection


def _private_key_der(path: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise SnowflakeError(f"SNOWFLAKE_PRIVATE_KEY_PATH points at a file that does not exist ({p})")
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or None
    try:
        key = serialization.load_pem_private_key(
            p.read_bytes(), password=passphrase.encode() if passphrase else None)
    except TypeError:
        raise SnowflakeError("the private key is encrypted: set SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") \
            from None
    except ValueError:
        raise SnowflakeError("could not parse the private key as PEM PKCS#8") from None
    return key.private_bytes(encoding=serialization.Encoding.DER,
                             format=serialization.PrivateFormat.PKCS8,
                             encryption_algorithm=serialization.NoEncryption())


def connect(database: Optional[str] = None):
    """Key-pair connection, read-only by intent. Raises SnowflakeError with a
    message that names the missing setting, never its value."""
    missing = missing_settings("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE",
                               "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_PRIVATE_KEY_PATH")
    if missing:
        raise SnowflakeError("missing Snowflake setting(s): " + ", ".join(missing)
                             + " (pass --snowflake-profile, or export them)")
    try:
        import snowflake.connector
    except ImportError:
        raise SnowflakeError("snowflake-connector-python is not installed in this interpreter") from None
    try:
        return snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            role=os.environ["SNOWFLAKE_ROLE"],
            warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
            database=database or os.environ.get("SNOWFLAKE_DATABASE") or None,
            authenticator="SNOWFLAKE_JWT",
            private_key=_private_key_der(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]),
            session_parameters={"QUERY_TAG": "molinia_partner_assess"},
            login_timeout=60,
            network_timeout=120,
            client_session_keep_alive=False,
        )
    except SnowflakeError:
        raise
    except Exception as exc:
        raise SnowflakeError(f"could not connect to Snowflake: {_short(exc)}") from None


def _short(exc: Exception) -> str:
    msg = str(getattr(exc, "msg", None) or exc).strip().splitlines()
    text = " ".join((msg[0] if msg else type(exc).__name__).split())
    return text[:240]


# --------------------------------------------------------------------------- probes


@dataclass
class Probe:
    name: str
    sql: str
    fallback_sql: Optional[str] = None
    note: str = ""
    #: 'schema'  the rows belong to a schema, so --schemas applies to them;
    #: 'account' the object lives above the database (shares, warehouses), so a
    #:           schema filter cannot apply and the report has to say so.
    scope: str = "schema"


#: Column names a probe's rows may use for "the schema this object is in".
#: `SHOW ... IN DATABASE` answers with `schema_name`; INFORMATION_SCHEMA views
#: each use their own spelling.
SCHEMA_KEYS = ("schema_name", "table_schema", "procedure_schema", "function_schema",
               "sequence_schema", "stage_schema", "file_format_schema")


def filter_rows_to_schemas(rows: List[dict], schemas: Sequence[str]) -> List[dict]:
    """Drop rows that are not in one of `schemas`.

    Several probes are `SHOW ... IN DATABASE`, which has no schema clause, and
    three INFORMATION_SCHEMA views (SEQUENCES, STAGES, FILE_FORMATS) were left
    unfiltered. Without this, `--schemas RAW` still counted the tasks, streams,
    policies, stages and external tables of every other schema, so a scoped
    assessment silently quoted objects that were never in scope.

    A row with no recognisable schema column is KEPT, and the caller says in the
    report that it could not be scoped — dropping it would hide an object.
    """
    if not schemas:
        return rows
    wanted = {s.strip().upper() for s in schemas if s.strip()}
    out = []
    for r in rows:
        key = next((k for k in SCHEMA_KEYS if k in r), None)
        if key is None or str(r.get(key) or "").upper() in wanted:
            out.append(r)
    return out


@dataclass
class Inventory:
    database: str = ""
    schemas_requested: List[str] = field(default_factory=list)
    rows: Dict[str, List[dict]] = field(default_factory=dict)
    failed: Dict[str, str] = field(default_factory=dict)
    timings: Dict[str, float] = field(default_factory=dict)
    account: str = ""
    role: str = ""
    warehouse: str = ""
    elapsed: float = 0.0
    skipped: List[str] = field(default_factory=list)
    #: probes whose objects live above the database, so --schemas cannot scope them
    account_scoped: List[str] = field(default_factory=list)

    def get(self, name: str) -> List[dict]:
        return self.rows.get(name, [])

    def as_dict(self) -> dict:
        return {"database": self.database, "account": self.account, "role": self.role,
                "warehouse": self.warehouse, "schemas_requested": self.schemas_requested,
                "elapsed_seconds": round(self.elapsed, 1),
                "counts": {k: len(v) for k, v in sorted(self.rows.items())},
                "probes_failed": self.failed, "probes_skipped": self.skipped,
                "objects": self.rows}


def quote_lit(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def build_probes(database: str, schemas: Sequence[str], sample_queries: int = 0) -> List[Probe]:
    db = quote_ident(database)
    dbl = quote_lit(database)
    scoped = ""
    if schemas:
        scoped = " AND TABLE_SCHEMA IN (" + ", ".join(quote_lit(s.upper()) for s in schemas) + ")"
    base = " WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'" + scoped

    probes = [
        Probe("schemata",
              f"SELECT SCHEMA_NAME, SCHEMA_OWNER, IS_TRANSIENT, RETENTION_TIME, COMMENT "
              f"FROM {db}.INFORMATION_SCHEMA.SCHEMATA "
              f"WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA'"
              + (" AND SCHEMA_NAME IN (" + ", ".join(quote_lit(s.upper()) for s in schemas) + ")"
                 if schemas else "")
              + " ORDER BY SCHEMA_NAME",
              fallback_sql=f"SELECT SCHEMA_NAME FROM {db}.INFORMATION_SCHEMA.SCHEMATA"),
        Probe("tables",
              f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, IS_TRANSIENT, "
              f"CLUSTERING_KEY, COMMENT FROM {db}.INFORMATION_SCHEMA.TABLES{base} "
              f"ORDER BY BYTES DESC NULLS LAST, TABLE_SCHEMA, TABLE_NAME",
              fallback_sql=f"SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES "
                           f"FROM {db}.INFORMATION_SCHEMA.TABLES{base}",
              note="row counts and bytes are Snowflake's maintained metadata: no table is scanned"),
        Probe("columns",
              f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, "
              f"NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE, IS_IDENTITY "
              f"FROM {db}.INFORMATION_SCHEMA.COLUMNS{base} "
              f"ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION",
              fallback_sql=f"SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, ORDINAL_POSITION, "
                           f"DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE "
                           f"FROM {db}.INFORMATION_SCHEMA.COLUMNS{base}"),
        Probe("views",
              f"SELECT TABLE_SCHEMA, TABLE_NAME, IS_SECURE, VIEW_DEFINITION "
              f"FROM {db}.INFORMATION_SCHEMA.VIEWS{base}",
              fallback_sql=f"SELECT TABLE_SCHEMA, TABLE_NAME, VIEW_DEFINITION "
                           f"FROM {db}.INFORMATION_SCHEMA.VIEWS{base}",
              note="the definitions are scanned with the same rulebook as the dbt project"),
        Probe("procedures",
              f"SELECT PROCEDURE_SCHEMA, PROCEDURE_NAME, ARGUMENT_SIGNATURE, PROCEDURE_LANGUAGE, "
              f"PROCEDURE_DEFINITION FROM {db}.INFORMATION_SCHEMA.PROCEDURES "
              f"WHERE PROCEDURE_SCHEMA <> 'INFORMATION_SCHEMA'"
              + (" AND PROCEDURE_SCHEMA IN (" + ", ".join(quote_lit(s.upper()) for s in schemas)
                 + ")" if schemas else ""),
              fallback_sql=f"SELECT PROCEDURE_SCHEMA, PROCEDURE_NAME, ARGUMENT_SIGNATURE, "
                           f"PROCEDURE_LANGUAGE FROM {db}.INFORMATION_SCHEMA.PROCEDURES"),
        Probe("functions",
              f"SELECT FUNCTION_SCHEMA, FUNCTION_NAME, ARGUMENT_SIGNATURE, FUNCTION_LANGUAGE, "
              f"IS_EXTERNAL FROM {db}.INFORMATION_SCHEMA.FUNCTIONS "
              f"WHERE FUNCTION_SCHEMA <> 'INFORMATION_SCHEMA'"
              + (" AND FUNCTION_SCHEMA IN (" + ", ".join(quote_lit(s.upper()) for s in schemas)
                 + ")" if schemas else ""),
              fallback_sql=f"SELECT FUNCTION_SCHEMA, FUNCTION_NAME, FUNCTION_LANGUAGE "
                           f"FROM {db}.INFORMATION_SCHEMA.FUNCTIONS"),
        Probe("sequences",
              f"SELECT SEQUENCE_SCHEMA, SEQUENCE_NAME, NEXT_VALUE, INCREMENT "
              f"FROM {db}.INFORMATION_SCHEMA.SEQUENCES",
              fallback_sql=f"SELECT SEQUENCE_SCHEMA, SEQUENCE_NAME "
                           f"FROM {db}.INFORMATION_SCHEMA.SEQUENCES"),
        Probe("stages",
              f"SELECT STAGE_SCHEMA, STAGE_NAME, STAGE_TYPE, STAGE_URL, STAGE_REGION "
              f"FROM {db}.INFORMATION_SCHEMA.STAGES",
              fallback_sql=f"SELECT STAGE_SCHEMA, STAGE_NAME, STAGE_TYPE "
                           f"FROM {db}.INFORMATION_SCHEMA.STAGES"),
        Probe("file_formats",
              f"SELECT FILE_FORMAT_SCHEMA, FILE_FORMAT_NAME, FILE_FORMAT_TYPE "
              f"FROM {db}.INFORMATION_SCHEMA.FILE_FORMATS"),
        Probe("external_tables", f"SHOW EXTERNAL TABLES IN DATABASE {db}"),
        Probe("iceberg_tables", f"SHOW ICEBERG TABLES IN DATABASE {db}",
              note="Iceberg tables cannot be read by Molinia today"),
        Probe("materialized_views", f"SHOW MATERIALIZED VIEWS IN DATABASE {db}"),
        Probe("dynamic_tables", f"SHOW DYNAMIC TABLES IN DATABASE {db}"),
        Probe("tasks", f"SHOW TASKS IN DATABASE {db}",
              note="a task whose definition CALLs a procedure has no Molinia equivalent"),
        Probe("streams", f"SHOW STREAMS IN DATABASE {db}"),
        Probe("masking_policies", f"SHOW MASKING POLICIES IN DATABASE {db}"),
        Probe("row_access_policies", f"SHOW ROW ACCESS POLICIES IN DATABASE {db}"),
        Probe("shares", "SHOW SHARES",
              note="account level: needs a role with the privilege, otherwise it is skipped",
              scope="account"),
        Probe("warehouses", "SHOW WAREHOUSES",
              note="sizing input only; warehouses are not migrated objects", scope="account"),
    ]
    if sample_queries > 0:
        n = max(1, min(int(sample_queries), 200))
        probes += [
            Probe("query_history_by_type", scope="account", sql=
                  "SELECT QUERY_TYPE, COUNT(*) AS QUERIES, "
                  "ROUND(SUM(TOTAL_ELAPSED_TIME)/1000.0, 1) AS ELAPSED_SECONDS, "
                  "COUNT(DISTINCT USER_NAME) AS USERS "
                  "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY "
                  "WHERE START_TIME > DATEADD('day', -7, CURRENT_TIMESTAMP()) "
                  f"AND DATABASE_NAME = {dbl} "
                  f"GROUP BY 1 ORDER BY QUERIES DESC LIMIT {n}",
                  note="ACCOUNT_USAGE, last 7 days; latency up to 45 minutes"),
            Probe("query_history_clients", scope="account", sql=
                  "SELECT COALESCE(S.CLIENT_APPLICATION_ID, 'unknown') AS CLIENT, "
                  "COUNT(*) AS QUERIES "
                  "FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY Q "
                  "JOIN SNOWFLAKE.ACCOUNT_USAGE.SESSIONS S ON S.SESSION_ID = Q.SESSION_ID "
                  "WHERE Q.START_TIME > DATEADD('day', -7, CURRENT_TIMESTAMP()) "
                  f"AND Q.DATABASE_NAME = {dbl} "
                  f"GROUP BY 1 ORDER BY QUERIES DESC LIMIT {n}",
                  note="which client applications query this database — the pgwire question"),
        ]
    return probes


def _rows(cur) -> List[dict]:
    names = [d[0].lower() for d in (cur.description or [])]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def run_probes(conn, probes: Sequence[Probe], budget_seconds: float = 120.0,
               max_rows: int = 100000, log=None,
               schemas: Optional[Sequence[str]] = None) -> Inventory:
    """Run every probe, independently, inside a wall-clock budget.

    `schemas` is the --schemas filter. Probes that can carry it in SQL already
    do; the rest (every `SHOW ... IN DATABASE`, plus SEQUENCES/STAGES/
    FILE_FORMATS) are filtered here, on the rows, so a scoped assessment counts
    only what it says it is scoped to.
    """
    inv = Inventory()
    schemas = list(schemas or [])
    inv.schemas_requested = list(schemas)
    started = time.monotonic()
    cur = conn.cursor()
    try:
        try:
            cur.execute("SELECT CURRENT_ACCOUNT(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
            row = cur.fetchone() or ("", "", "")
            inv.account, inv.role, inv.warehouse = (str(v or "") for v in row[:3])
        except Exception as exc:
            inv.failed["session"] = _short(exc)
        for probe in probes:
            if time.monotonic() - started > budget_seconds:
                inv.skipped.append(probe.name)
                continue
            for sql in (probe.sql, probe.fallback_sql):
                if not sql:
                    break
                if _WRITE.match(sql):
                    inv.failed[probe.name] = "refused: not a read-only statement"
                    break
                t0 = time.monotonic()
                try:
                    cur.execute(sql)
                    rows = _rows(cur)
                    if probe.scope == "schema":
                        rows = filter_rows_to_schemas(rows, schemas)
                    elif schemas:
                        inv.account_scoped.append(probe.name)
                    note = ""
                    if len(rows) > max_rows:
                        rows = rows[:max_rows]
                        note = f"truncated at {max_rows} rows"
                    inv.rows[probe.name] = rows
                    inv.timings[probe.name] = time.monotonic() - t0
                    if note:
                        inv.failed[probe.name] = note
                    else:
                        inv.failed.pop(probe.name, None)
                    if log:
                        log(f"  {probe.name:<22} {len(rows):>6} rows  "
                            f"{time.monotonic() - t0:5.1f}s")
                    break
                except Exception as exc:
                    inv.failed[probe.name] = _short(exc)
                    if log and sql is (probe.fallback_sql or probe.sql):
                        log(f"  {probe.name:<22} failed: {inv.failed[probe.name]}")
    finally:
        cur.close()
    inv.elapsed = time.monotonic() - started
    return inv
