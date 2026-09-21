"""Turn the inventory into the three things a consultant has to quote against:
a data-movement plan, a parity plan, and the blockers that stop a migration dead.

Nothing here talks to Molinia. The commands are printed for the partner to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import classify as C
from .facts import fact
from .scan import Project

# --------------------------------------------------------------------------- unload size
#
# Measured on this kit's own dataset on 2026-09-20: the seven MOLINIA_DEMO.RAW
# tables' INFORMATION_SCHEMA.TABLES.BYTES against the Parquet files the same
# tables produced through `COPY INTO ... FILE_FORMAT = (TYPE = PARQUET)`.
#
# The result is the opposite of the folklore: the Parquet unload came out
# LARGER than Snowflake's own storage, 1.52x in total, and between 0.99x and
# 2.17x per table. Snowflake's micro-partitions are aggressively compressed and
# a plain Parquet unload is not. Sizing a transfer window off "Parquet is
# smaller" would have under-provisioned by a third.
#
#   web_events        451,072 -> 608,018   1.35   order_items 336,384 -> 728,724  2.17
#   orders            399,872 -> 394,741   0.99   customers    47,104 ->  58,601  1.24
#   payments          346,112 -> 619,522   1.79   contacts     47,104 ->  71,375  1.52
#   products            3,584 ->   5,462   1.52
PARQUET_CALIBRATION = {
    "snowflake_bytes": 1_631_232,
    "parquet_bytes": 2_486_443,
    "tables": 7,
    "per_table_min": 0.99,
    "per_table_max": 2.17,
    "measured_on": "2026-09-20",
    "dataset": "MOLINIA_DEMO.RAW (the kit's acme_shop source data)",
}
PARQUET_RATIO = round(PARQUET_CALIBRATION["parquet_bytes"]
                      / PARQUET_CALIBRATION["snowflake_bytes"], 2)

PARQUET_ASSUMPTION = (
    "Unload size = Snowflake BYTES x {ratio}. That multiplier is measured, not assumed, and it is "
    "the opposite of the usual claim: on this kit's dataset the Parquet unload came out LARGER "
    "than Snowflake's own storage ({sf:,} B -> {pq:,} B over {n} tables on {when}), because "
    "Snowflake's micro-partitions are aggressively compressed and a plain Parquet unload is not. "
    "Per table it ranged {lo}x to {hi}x — wide numeric tables worst. Use this to size a transfer "
    "window and a bucket, never as a quote input: unload the client's largest table first and "
    "re-scale from that one real number (--parquet-ratio)."
)

SINGLE_FILE_LIMIT = 5 * 1024 ** 3        # Snowflake's MAX_FILE_SIZE ceiling for SINGLE = TRUE
DEFAULT_MAX_FILE_SIZE = 268_435_456      # what the kit's proven 02_unload.sql uses

_SEMI = {"VARIANT", "OBJECT", "ARRAY"}


def parquet_assumption(ratio: float) -> str:
    return PARQUET_ASSUMPTION.format(
        ratio=ratio, sf=PARQUET_CALIBRATION["snowflake_bytes"],
        pq=PARQUET_CALIBRATION["parquet_bytes"], n=PARQUET_CALIBRATION["tables"],
        lo=PARQUET_CALIBRATION["per_table_min"], hi=PARQUET_CALIBRATION["per_table_max"],
        when=PARQUET_CALIBRATION["measured_on"])


# --------------------------------------------------------------------------- data movement


@dataclass
class Move:
    schema: str
    table: str
    rows: Optional[int]
    bytes: Optional[int]
    est_parquet_bytes: Optional[int]
    target_table: str
    order: int
    why_order: str
    copy_sql: str
    ingest_body: str
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"schema": self.schema, "table": self.table, "rows": self.rows,
                "snowflake_bytes": self.bytes, "est_parquet_bytes": self.est_parquet_bytes,
                "molinia_table": self.target_table, "order": self.order,
                "why_order": self.why_order, "copy_into": self.copy_sql,
                "ingest_body": self.ingest_body, "notes": self.notes}


def _source_identifiers(proj: Optional[Project]) -> Dict[str, str]:
    """{TABLE_NAME upper: the dbt source name} for tables the project reads."""
    out: Dict[str, str] = {}
    if not proj or not proj.ok:
        return out
    for src in proj.sources:
        for t in src.get("tables") or []:
            name = str(t.get("name") or "")
            ident = str(t.get("identifier") or name)
            if name:
                out[name.upper()] = f"{src.get('name')}.{name}"
                out[ident.upper()] = f"{src.get('name')}.{name}"
    return out


def _pii_source_names(proj: Optional[Project]) -> List[str]:
    """Source tables the project itself marks as PII (meta.contains_pii), once each."""
    out: List[str] = []
    if not proj or not proj.ok:
        return out
    for src in proj.sources:
        for t in src.get("tables") or []:
            meta = t.get("meta") or {}
            if any(str(k).lower() in ("contains_pii", "pii", "is_pii") and bool(v)
                   for k, v in meta.items()):
                out.append(f"{src.get('name')}.{t.get('name')}")
    return sorted(out)


def _pii_sources(proj: Optional[Project]) -> set:
    """Every spelling (source name and identifier) of a PII source table, for lookups."""
    out = set()
    if not proj or not proj.ok:
        return out
    for src in proj.sources:
        for t in src.get("tables") or []:
            meta = t.get("meta") or {}
            if any(str(k).lower() in ("contains_pii", "pii", "is_pii") and bool(v)
                   for k, v in meta.items()):
                out.add(str(t.get("name") or "").upper())
                out.add(str(t.get("identifier") or t.get("name") or "").upper())
    return out


def build_moves(database: str, tables: Sequence[dict], columns: Sequence[dict],
                proj: Optional[Project], ratio: float, stage: str,
                org_id: str, datasource_id: str, location_id: str,
                prefix: str = "raw") -> List[Move]:
    """One COPY INTO + one ingest call per base table, in the order to run them.

    A table the dbt project itself builds is kept in the list but ordered last
    and labelled: its data does not need to move, because dbt rebuilds it on
    Molinia. It is unloaded as a parity answer key instead (section 7)."""
    built = {m.name.upper() for m in proj.models} if proj and proj.ok else set()
    by_table: Dict[Tuple[str, str], List[dict]] = {}
    for c in columns:
        by_table.setdefault((c["table_schema"], c["table_name"]), []).append(c)

    wanted = [t for t in tables if str(t.get("table_type", "")).upper() in
              ("BASE TABLE", "TABLE", "TEMPORARY TABLE", "TRANSIENT TABLE")]
    # ingest always lands in schema `main`, so two tables of the same name in two
    # Snowflake schemas would collide there.
    name_counts: Dict[str, int] = {}
    for t in wanted:
        n = str(t["table_name"]).upper()
        name_counts[n] = name_counts.get(n, 0) + 1

    sources = _source_identifiers(proj)
    pii = _pii_sources(proj)
    moves: List[Move] = []
    for t in wanted:
        schema, table = str(t["table_schema"]), str(t["table_name"])
        cols = sorted(by_table.get((schema, table), []), key=lambda c: c.get("ordinal_position") or 0)
        notes: List[str] = []
        select_list = []
        for c in cols:
            name = str(c["column_name"])
            dtype = str(c.get("data_type") or "").upper()
            if dtype in _SEMI:
                select_list.append(f'    TO_JSON("{name}") AS "{name}"')
                notes.append(f"{name} is {dtype}: unloaded with TO_JSON, arrives as VARCHAR (J0)")
            else:
                select_list.append(f'    "{name}"')
        if not select_list:
            select_list = ["    *"]
            notes.append("column list not available: the COPY uses SELECT *, check the order")

        is_built = table.upper() in built
        kind_prefix = "expected" if is_built else prefix
        suffix = f"{schema.lower()}_{table.lower()}" if name_counts[table.upper()] > 1 \
            else table.lower()
        target = ("sf_" if is_built else f"{prefix}_") + suffix
        if name_counts[table.upper()] > 1 and not is_built:
            notes.append(f"another schema also has a table called {table}: ingest lands everything "
                         f"in schema `main`, so the name is prefixed with the schema")

        est = int((t.get("bytes") or 0) * ratio) if t.get("bytes") is not None else None
        single = est is None or est < SINGLE_FILE_LIMIT
        file_target = (f"@{stage}/{kind_prefix}/{suffix}.parquet" if single
                       else f"@{stage}/{kind_prefix}/{suffix}/")
        copy_sql = (
            f"COPY INTO {file_target}\n"
            f"FROM (\n  SELECT\n" + ",\n".join(select_list) + "\n"
            f'  FROM "{database}"."{schema}"."{table}"\n)\n'
            f"FILE_FORMAT = (TYPE = PARQUET)\nHEADER = TRUE\n"
            + ("SINGLE = TRUE\n" if single else "")
            + f"OVERWRITE = TRUE\nMAX_FILE_SIZE = {DEFAULT_MAX_FILE_SIZE};"
        )
        if not single:
            notes.append("estimated over 5 GB, so SINGLE = TRUE is not available: the unload writes "
                         "several files and each one needs its own ingest call. The kit has never "
                         "exercised a multi-file ingest — prove it before quoting this table.")
        ingest = ('POST /api/orgs/%s/datasources/%s/ingest\n{"targetTable": "%s", '
                  '"filePath": "%s/%s.parquet", "locationId": %s, "fileFormat": "parquet"}'
                  % (org_id, datasource_id, target, kind_prefix, suffix, location_id))
        if table.upper() in pii:
            notes.append("the dbt project marks this source as PII (meta.contains_pii). It lands "
                         "in schema `main`, which is the only place masking resolves, and the "
                         "policy is created by an admin in the console AFTER ingest. No model may "
                         "read a masked column with a build key: a service account can never hold "
                         "column:unmask, so it would write masked values.")

        src = sources.get(table.upper())
        if is_built:
            order = 3
            why = (f"built by dbt model `{table.lower()}` — do NOT migrate this data; unload it as "
                   f"the parity answer key instead (section 7)")
            notes.append("dbt rebuilds this relation on Molinia: moving it would overwrite the "
                         "build with a copy of the old output")
        elif src:
            order, why = 1, f"the dbt project reads it as source('{src}')"
        else:
            order, why = 2, "not referenced by the dbt project: move it only if something else reads it"
        moves.append(Move(schema, table, t.get("row_count"), t.get("bytes"), est, target,
                          order, why, copy_sql, ingest, notes))

    moves.sort(key=lambda m: (m.order, m.bytes if m.bytes is not None else 0, m.schema, m.table))
    return moves


# --------------------------------------------------------------------------- parity plan


@dataclass
class ParityEntry:
    model: str
    relation: str
    answer_key: str
    key: List[str]
    key_source: str
    snowflake_relation: str
    unload_sql: str

    def as_dict(self) -> dict:
        return {"model": self.model, "relation": self.relation, "answer_key": self.answer_key,
                "key": self.key, "key_source": self.key_source or "NONE — choose one by hand",
                "snowflake_relation": self.snowflake_relation, "unload": self.unload_sql}


def build_parity(proj: Optional[Project], database: str, stage: str,
                 target_schema: str = "") -> Tuple[List[ParityEntry], List[str]]:
    """A parity entry per model, plus the models with no usable key."""
    entries: List[ParityEntry] = []
    keyless: List[str] = []
    if not proj or not proj.ok:
        return entries, keyless
    tschema = (target_schema or proj.target_schema or "<TARGET_SCHEMA>").upper()
    db = (database or proj.target_database or "<DATABASE>").upper()
    for node in proj.models:
        if node.language != "sql":
            continue
        molinia_schema = node.schema or "<target schema>"
        custom = node.schema
        sf_schema = f"{tschema}_{custom.upper()}" if custom else tschema
        sf_rel = f'"{db}"."{sf_schema}"."{node.name.upper()}"'
        unload = (f"COPY INTO @{stage}/expected/{node.name}.parquet\n"
                  f"FROM (SELECT * FROM {sf_rel})\n"
                  f"FILE_FORMAT = (TYPE = PARQUET)\nHEADER = TRUE\nSINGLE = TRUE\n"
                  f"OVERWRITE = TRUE\nMAX_FILE_SIZE = {DEFAULT_MAX_FILE_SIZE};")
        if not node.key:
            keyless.append(node.name)
        entries.append(ParityEntry(node.name, f"{molinia_schema}.{node.name}",
                                   f"main.sf_{node.name}", node.key, node.key_source,
                                   sf_rel, unload))
    return entries, keyless


def parity_yaml(entries: Sequence[ParityEntry]) -> str:
    """A `parity.yml` in the shape `agent/parity.py` already reads."""
    out = ["# generated by partner/assess.py — check every key before you trust a green run",
           "tolerance: 1.0e-9", "", "models:"]
    for e in entries:
        key = ", ".join(e.key) if e.key else "CHOOSE_A_KEY"
        out += [f"  - name: {e.model}",
                f"    relation: {e.relation}",
                f"    answer_key: {e.answer_key}",
                f"    key: [{key}]" + ("" if e.key else "   # NO unique test found: pick the grain")]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- build cost


def request_projection(models: int, tests: int) -> dict:
    """Projected API requests and wall-clock for one full dbt build.

    Calibrated on the kit's own measurement: 164 requests and 6 minutes for 10
    models and 63 tests, half of them connection checks, paced at 25 requests a
    minute. That is 2.25 requests per node, which is one statement plus roughly
    one connection check.
    """
    nodes = models + tests
    per_node = 164 / 73
    requests = int(round(nodes * per_node))
    return {"nodes": nodes, "requests_per_node": round(per_node, 2), "requests": requests,
            "minutes_at_25_per_min": round(requests / 25.0, 1),
            "free_plan_engine_queries_per_min": 30, "paid_plan_engine_queries_per_min": 300,
            "http_requests_per_min_per_ip": 60,
            "basis": "measured: 164 requests / 6 minutes for 10 models + 63 tests"}


# --------------------------------------------------------------------------- blockers


_BI_MARKERS = ("tableau", "power bi", "powerbi", "looker", "qlik", "jdbc", "odbc", "excel",
               "sigma", "thoughtspot", "metabase", "superset", "mstr", "microstrategy", "spotfire",
               "dbeaver", "datagrip", "alteryx", "sisense", "domo", "hex", "mode")


def _bi_clients(rows: Sequence[dict]) -> List[str]:
    """Client applications in ACCOUNT_USAGE that look like a BI or SQL tool.

    They are the pgwire question made concrete: each one is a live connection
    with no route to Molinia today."""
    out = []
    for r in rows:
        name = str(r.get("client") or r.get("client_application_id") or "")
        if any(m in name.lower() for m in _BI_MARKERS):
            out.append(f"{name} ({r.get('queries', '?')} queries)")
    return out


@dataclass
class Blocker:
    id: str
    title: str
    evidence: str          # the fact behind it
    instead: str           # what to do instead
    triggered_by: str = "always"
    severity: str = "blocker"   # blocker | prerequisite | watch

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "evidence": self.evidence,
                "do_instead": self.instead, "triggered_by": self.triggered_by,
                "severity": self.severity}


def build_blockers(inv_rows: Dict[str, List[dict]], proj: Optional[Project],
                   moves: Sequence[Move], items: Sequence[C.Item],
                   snowflake_available: bool) -> List[Blocker]:
    out: List[Blocker] = []

    def n(name: str) -> int:
        return len(inv_rows.get(name, []))

    bi_clients = _bi_clients(inv_rows.get("query_history_clients", []))
    out.append(Blocker(
        "BI-PGWIRE", "BI tools cannot connect the way they connect to Snowflake",
        fact("F-PGWIRE"),
        "Every Snowflake BI connection has to be re-planned before the migration is quoted: today "
        "the surfaces are the REST query API (one statement per request, service-account key) and "
        "MCP, which is read-only and prod-only. If the client's dashboards are the deliverable, "
        "scope the BI layer as its own workstream and get pgwire's status in writing first."
        + (" The query history for this database already shows " + ", ".join(bi_clients)
           + " connecting: those are live dashboards, and none of them has a route to Molinia "
             "today." if bi_clients else ""),
        ("BI clients seen in the query history: " + ", ".join(bi_clients)) if bi_clients
        else "always"))

    if not snowflake_available:
        out.append(Blocker(
            "NO-SF", "The Snowflake side was not inventoried",
            "This run had no Snowflake connection (--no-snowflake, or the connection failed).",
            "The dbt half of this report stands on its own, but object counts, data volume and the "
            "movement plan do not exist. Do not quote a fixed price from a dbt-only assessment.",
            "no Snowflake connection", "prerequisite"))

    masking = n("masking_policies")
    rls = n("row_access_policies")
    pii = _pii_source_names(proj)
    if snowflake_available:
        policy_scope = f"{masking} masking policies, {rls} row access policies in scope"
    else:
        policy_scope = "Snowflake was not inventoried, so the policies in scope are unknown"
    if pii:
        policy_scope += ("; the dbt project marks these sources as PII: " + ", ".join(pii))
    # This one is always printed. It is a design constraint on where data may
    # land and on what a build key may read, so it binds even in an account
    # that has no policy today.
    out.append(Blocker(
        "MASK-MAIN", "Masking and RLS only resolve tables in schema `main`",
        fact("F-MASK-MAIN") + " " + fact("F-MASK-SA"),
        "Land every table that carries a policy in schema `main` (which is where ingest puts "
        "them anyway) and keep masked columns out of every model: a build service account can "
        "never hold column:unmask, so a model that reads a masked column writes masked values "
        "into its output — silently, and parity will not catch it, because both sides would be "
        "the agent's. Re-create the policies in the console after the tables exist.",
        policy_scope, "blocker" if (masking or rls or pii or not snowflake_available) else "watch"))
    out.append(Blocker(
        "RLS-ORDER", "An RLS policy on a table that does not exist breaks the whole org",
        fact("F-RLS-BREAK"),
        "Create policies only after the tables they name are ingested, and never leave a policy "
        "pointing at a table you are about to replace. One wrong policy takes down every engine "
        "query in the org, not just the query that touches that table.",
        policy_scope, "blocker" if rls else "watch"))

    tasks = inv_rows.get("tasks", [])
    calling = [t for t in tasks if "call" in str(t.get("definition", "")).lower()]
    if tasks:
        out.append(Blocker(
            "TASK-CALL", "Task orchestration does not port",
            fact("F-NOTPORT"),
            "Rebuild the schedule outside the warehouse — the agent can port each task's SQL, but "
            "the DAG, the predecessors and any CALL have to become pipeline steps or an external "
            "scheduler. Price the orchestration rebuild separately from the SQL port.",
            f"{len(tasks)} tasks, {len(calling)} of them CALL a procedure"))

    if proj and proj.ok:
        # Always printed for a dbt project. `dbt docs generate` is in most dbt
        # CI pipelines and is often a named deliverable, and it fails at the
        # catalog step on every project, not only on one with seeds — so it
        # cannot ride along in the seeds-and-snapshots blocker.
        out.append(Blocker(
            "DBT-DOCS", "`dbt docs generate` does not complete against Molinia",
            fact("F-DBT-NO"),
            "The catalog step has no `get_catalog`, so the docs site cannot be built from a Molinia "
            "run. If the client's statement of work names a documentation site or a data "
            "dictionary, either produce it from their Snowflake run before cutover, or take it out "
            "of scope in writing. Do not discover this in the last sprint.",
            "always, for any dbt project", "watch"))
        seeds = [x for x in proj.nodes if x.kind == "seed"]
        snaps = [x for x in proj.nodes if x.kind == "snapshot"]
        if seeds or snaps:
            out.append(Blocker(
                "DBT-SEED-SNAP", "dbt seeds and snapshots do not run on dbt-molinia",
                fact("F-DBT-NO"),
                "Load seed CSVs through ingest and delete the seed nodes. Snapshots are a redesign: "
                "there is no session state between statements, so SCD2 history has to be produced "
                "by a model against a full history table, or kept in the source system.",
                f"{len(seeds)} seeds, {len(snaps)} snapshots in the project"))
        inc = [x for x in proj.models if x.materialized == "incremental"]
        if inc:
            out.append(Blocker(
                "DBT-INCREMENTAL", "Incremental models have never been proven on a live server",
                fact("F-DBT-INC"),
                "Budget a spike before the quote: port one incremental model, run it full-refresh, "
                "run it again, and prove parity after both. If it fails, those models become table "
                "rebuilds, which changes the compute profile of the whole project.",
                f"{len(inc)} incremental models", "prerequisite"))
        if proj.packages:
            out.append(Blocker(
                "DBT-PACKAGES", "Package macros have never been run against dbt-molinia",
                "Inference, not a verified fact: dbt-molinia ships no `molinia__` macro "
                "implementations, so a dispatched package macro falls through to `default__`. "
                "Whether that default is valid DuckDB has not been tested by this kit.",
                "List the package macros the project actually calls and test them first. Treat the "
                f"{len(proj.packages)} package dependencies as an unknown, not as portable code.",
                ", ".join(str(p.get("package") or p.get("git") or p.get("local"))
                          for p in proj.packages), "prerequisite"))

    out.append(Blocker(
        "ADAPTER", "The dbt adapter you need is not the one that is published",
        fact("F-ADAPTER"),
        "Pin the install to the branch commit in the delivery runbook and check for the pacing "
        "keys before the first build. On the released adapter every dbt test reports "
        "\"ERROR: '0' is not of type 'integer'\" — a red build on green data.",
        "always", "prerequisite"))

    out.append(Blocker(
        "RATE", "The rate limits shape the delivery plan, not just the build",
        fact("F-RATE"),
        "One API-using command at a time, threads: 1, and a paid-plan org for delivery: 30 engine "
        "queries a minute and 3600 engine-seconds a day are a free-tier sandbox, not a migration "
        "environment. Two agents on one org exceed the limit between them.",
        "always", "prerequisite"))

    out.append(Blocker(
        "NO-READER", "There is no reader: data moves as files",
        fact("F-NOREADER") + " " + fact("F-INGEST"),
        "Plan an unload window on the client's Snowflake warehouse, a bucket in the EU they control, "
        "and a re-run policy: ingest is CREATE OR REPLACE TABLE, so re-running a table is safe but "
        "replaces it wholesale. Nothing incremental exists on the ingest path.",
        "always"))

    if any(i.klass == C.NOT_SUPPORTED_TODAY and i.kind in ("iceberg table", "external table")
           for i in items):
        out.append(Blocker(
            "ICEBERG", "Iceberg and external tables cannot be read",
            fact("F-NOREADER"),
            "Those tables stay where they are, or their underlying files are ingested as Parquet "
            "and the table becomes a normal Molinia table. Either way the semantics change: no "
            "schema evolution, no time travel, no shared catalog.",
            "external or Iceberg tables in scope"))

    out.append(Blocker(
        "AUDIT-PRINCIPAL", "Query History does not show which principal ran a query",
        fact("F-AUDIT"),
        "If the client needs per-user attribution of queries during or after the migration, the "
        "audit log is the source, not Query History. Say so before the security review does.",
        "always", "watch"))

    return out


# --------------------------------------------------------------------------- effort


def effort_summary(items: Sequence[C.Item], proj: Optional[Project],
                   projection: dict) -> dict:
    by_class = C.counts(list(items))
    models = len([x for x in (proj.models if proj and proj.ok else [])])
    tests = len(proj.tests) if proj and proj.ok else 0
    return {
        "counts_by_class": by_class,
        "dbt_models": models,
        "dbt_tests": tests,
        "build_projection": projection,
        "calibration_anchor": fact("F-ANCHOR"),
        "calibration_cost": fact("F-BUILD-COST"),
        "pricing_note": (
            "These are counts and one measured anchor. This tool deliberately gives no "
            "hours-per-object rate: the only honest data points are the two runs above, both on "
            "one 10-model project with a rulebook already written for it. Price the first client "
            "off the anchor plus your own review rate, then re-calibrate with your own numbers."),
    }


# --------------------------------------------------------------------------- Snowflake objects


def _q(schema: object, name: object) -> str:
    return f"{schema}.{name}"


def classify_inventory(rows: Dict[str, List[dict]], scan_sql,
                       dbt_models: Optional[Sequence[str]] = None) -> Tuple[List[C.Item],
                                                                            List[dict]]:
    """Place every inventoried Snowflake object in a class.

    `scan_sql(text, label)` returns rule hits, so a view definition and a
    procedure body are held to the same rulebook as the dbt project.
    `dbt_models` is the project's model names: a Snowflake relation the project
    itself builds is not separate migration work, and counting it as an object
    to port would double-count the whole dbt project.
    Returns (items, column type summary).
    """
    items: List[C.Item] = []
    built = {str(m).upper() for m in (dbt_models or [])}

    # ---- columns, rolled up into their table -----------------------------
    per_table: Dict[Tuple[str, str], List[tuple]] = {}
    type_summary: Dict[str, dict] = {}
    for c in rows.get("columns", []):
        dtype = str(c.get("data_type") or "").upper()
        molinia, klass, basis, rule, note = C.map_type(dtype)
        row = type_summary.setdefault(dtype, {"snowflake_type": dtype, "molinia_type": molinia,
                                              "class": klass, "basis": basis, "rule": rule,
                                              "note": note, "columns": 0, "tables": set()})
        row["columns"] += 1
        row["tables"].add(_q(c.get("table_schema"), c.get("table_name")))
        if klass != C.AUTOMATIC:
            per_table.setdefault((str(c.get("table_schema")), str(c.get("table_name"))), []).append(
                (klass, f"{c.get('column_name')} {dtype} -> {molinia}"
                        + (f" ({note})" if note else "")))
        if str(c.get("is_identity") or "").upper() in ("YES", "TRUE"):
            items.append(C.Item(
                "identity column", _q(c.get("table_schema"), c.get("table_name"))
                + "." + str(c.get("column_name")),
                C.NOT_SUPPORTED_TODAY,
                "an IDENTITY/AUTOINCREMENT column has no generator on the target: existing values "
                "move with the data, new rows need a key strategy.",
                C.INFERENCE, ""))
    summary = []
    for dtype, row in sorted(type_summary.items(), key=lambda kv: -kv[1]["columns"]):
        row = dict(row)
        row["tables"] = len(row["tables"])
        summary.append(row)

    # ---- tables and views -------------------------------------------------
    external_names = {str(r.get("name", "")).upper() for r in rows.get("external_tables", [])}
    iceberg_names = {str(r.get("name", "")).upper() for r in rows.get("iceberg_tables", [])}
    mv_names = {str(r.get("name", "")).upper() for r in rows.get("materialized_views", [])}
    view_defs = {(str(v.get("table_schema")), str(v.get("table_name"))): v
                 for v in rows.get("views", [])}

    for t in rows.get("tables", []):
        schema, name = str(t.get("table_schema")), str(t.get("table_name"))
        qname = _q(schema, name)
        ttype = str(t.get("table_type") or "").upper()
        upper = name.upper()
        if upper in built:
            kind = "view (dbt-built)" if ttype == "VIEW" else "table (dbt-built)"
            items.append(C.Item(kind, qname, C.AUTOMATIC,
                                f"the dbt project builds this relation (model `{name.lower()}`): "
                                "dbt rebuilds it on Molinia, so it is not a separate object to "
                                "port. Its Snowflake copy is the parity answer key instead.",
                                C.VERIFIED, ""))
            continue
        if upper in iceberg_names:
            items.append(C.Item("iceberg table", qname, C.NOT_SUPPORTED_TODAY,
                                "Molinia has no Iceberg read support and no Snowflake reader.",
                                C.VERIFIED, "F-NOREADER"))
            continue
        if upper in external_names or "EXTERNAL" in ttype:
            items.append(C.Item("external table", qname, C.NOT_SUPPORTED_TODAY,
                                "an external table reads files through Snowflake's catalog; there "
                                "is no equivalent read path.",
                                C.VERIFIED, "F-NOREADER"))
            continue
        if upper in mv_names or "MATERIALIZED" in ttype:
            items.append(C.Item("materialized view", qname, C.REDESIGN,
                                "no materialized view object: it becomes a table model plus a "
                                "schedule, which changes its refresh semantics.",
                                C.INFERENCE, ""))
            continue
        if ttype == "VIEW":
            v = view_defs.get((schema, name)) or {}
            definition = str(v.get("view_definition") or "")
            hits = scan_sql(definition, qname) if definition else []
            klass = C.worst(C.AGENT_PLUS_REVIEW, *[h.rule.klass for h in hits if h.rule.weight])
            ids = sorted({h.rule.id for h in hits if h.rule.weight})
            secure = str(v.get("is_secure") or "").upper() in ("YES", "TRUE")
            reason = ("the SQL is ported like a model" +
                      (f"; rulebook hits: {', '.join(ids)}" if ids else
                       "; no Snowflake-only construct found in the definition"))
            if not definition:
                reason = ("the definition could not be read with this role, so the SQL was not "
                          "scanned: assume it needs a port and a review")
                klass = C.worst(klass, C.AGENT_PLUS_REVIEW)
            if secure:
                reason += ". SECURE views have no equivalent: the protection has to be redone as " \
                          "masking or RLS in the console"
                klass = C.worst(klass, C.REDESIGN)
            items.append(C.Item("view", qname, klass, reason, C.VERIFIED, ",".join(ids)))
            continue
        flags = per_table.get((schema, name), [])
        worst_col = C.worst(*[k for k, _ in flags]) if flags else C.AUTOMATIC
        items.append(C.classify_table("table", qname, worst_col, [n for _, n in flags]))

    # ---- routines ---------------------------------------------------------
    for p in rows.get("procedures", []):
        qname = _q(p.get("procedure_schema"), p.get("procedure_name")) + \
            str(p.get("argument_signature") or "")
        item = C.classify_routine("procedure", qname, str(p.get("procedure_language") or ""),
                                  p.get("procedure_definition"))
        items.append(item)
    for f in rows.get("functions", []):
        qname = _q(f.get("function_schema"), f.get("function_name")) + \
            str(f.get("argument_signature") or "")
        items.append(C.classify_routine("function", qname, str(f.get("function_language") or ""),
                                        None))

    # ---- orchestration ----------------------------------------------------
    for t in rows.get("tasks", []):
        qname = _q(t.get("schema_name"), t.get("name"))
        definition = str(t.get("definition") or "")
        if "call" in definition.lower():
            items.append(C.Item("task", qname, C.NOT_SUPPORTED_TODAY,
                                "the task CALLs a procedure: CALL resolves only on the "
                                "single-statement query path, not in a scheduled task or a "
                                "pipeline step.", C.VERIFIED, "F-NOTPORT"))
        else:
            items.append(C.Item("task", qname, C.AGENT_PLUS_REVIEW,
                                "a single-statement task becomes a pipeline step or an external "
                                "schedule; the DAG and the predecessors are rebuilt by hand.",
                                C.VERIFIED, "F-NOTPORT",
                                {"schedule": str(t.get("schedule") or ""),
                                 "predecessors": str(t.get("predecessors") or "")}))
    for s in rows.get("streams", []):
        items.append(C.Item("stream", _q(s.get("schema_name"), s.get("name")), C.REDESIGN,
                            "no stream object: change capture has to be rebuilt from the source "
                            "system or from full snapshots.", C.INFERENCE, ""))
    for d in rows.get("dynamic_tables", []):
        items.append(C.Item("dynamic table", _q(d.get("schema_name"), d.get("name")), C.REDESIGN,
                            "no dynamic table and no TARGET_LAG: it becomes a table model plus a "
                            "schedule.", C.INFERENCE, ""))

    # ---- loading plumbing -------------------------------------------------
    for s in rows.get("stages", []):
        url = str(s.get("stage_url") or "")
        qname = _q(s.get("stage_schema"), s.get("stage_name"))
        if str(s.get("stage_type") or "").upper().startswith("INTERNAL") and not url:
            items.append(C.Item("stage", qname, C.AUTOMATIC,
                                "an internal stage is the unload target of the migration itself; "
                                "nothing to port.", C.VERIFIED, "F-NOREADER"))
        else:
            items.append(C.Item("stage", qname, C.AGENT_PLUS_REVIEW,
                                "an external stage becomes a Molinia data source plus a storage "
                                "location: new credentials, and the bucket has to be in the EU.",
                                C.INFERENCE, "", {"url": url}))
    for f in rows.get("file_formats", []):
        items.append(C.Item("file format", _q(f.get("file_format_schema"),
                                              f.get("file_format_name")), C.AUTOMATIC,
                            "replaced by the ingest call's fileFormat; no object to create.",
                            C.VERIFIED, "F-INGEST"))

    # ---- things with no target-side concept -------------------------------
    for s in rows.get("sequences", []):
        items.append(C.Item("sequence", _q(s.get("sequence_schema"), s.get("sequence_name")),
                            C.NOT_SUPPORTED_TODAY,
                            "the rulebook lists SEQUENCE among the values that cannot be "
                            "reproduced: any key it generates has to be re-designed.",
                            C.VERIFIED, "Y2"))
    for s in rows.get("shares", []):
        name = str(s.get("name") or "")
        kind = str(s.get("kind") or "").upper()
        owner = str(s.get("owner_account") or "").upper()
        if owner == "SNOWFLAKE" or "SFC_SAMPLES" in owner or "SFSALESSHARED" in owner:
            items.append(C.Item("share (Snowflake-provided)", name, C.AUTOMATIC,
                                "a share Snowflake itself provides (ACCOUNT_USAGE, sample data): "
                                "not client data, nothing to migrate.", C.VERIFIED, ""))
        elif kind == "INBOUND":
            provider = owner or "another account"
            items.append(C.Item("share (inbound)", name, C.NOT_SUPPORTED_TODAY,
                                f"the client consumes third-party data from {provider} through "
                                "Snowflake sharing (SHOW SHARES is account-level, so this is not "
                                "scoped to the database or schemas in the header). There is no "
                                "equivalent: that data has to "
                                "arrive as files under a new agreement with the provider, or the "
                                "workloads that use it stay on Snowflake.",
                                C.INFERENCE, ""))
        else:
            items.append(C.Item("share (outbound)", name, C.NOT_SUPPORTED_TODAY,
                                "the client publishes data to other accounts through Snowflake "
                                "sharing (SHOW SHARES is account-level, so this is not scoped to "
                                "the database or schemas in the header). Every consumer needs "
                                "another route, and that is its own project, not part of this "
                                "migration.", C.INFERENCE, ""))

    # ---- governance objects ----------------------------------------------
    for m in rows.get("masking_policies", []):
        items.append(C.Item("masking policy", _q(m.get("schema_name"), m.get("name")),
                            C.AGENT_PLUS_REVIEW,
                            "re-created by an admin in the Molinia console, never from SQL, and "
                            "only over tables in schema `main`.", C.VERIFIED, "F-MASK-MAIN"))
    for m in rows.get("row_access_policies", []):
        items.append(C.Item("row access policy", _q(m.get("schema_name"), m.get("name")),
                            C.REDESIGN,
                            "Molinia RLS is a different policy model; the predicate has to be "
                            "re-expressed and re-tested, and a policy naming a missing table "
                            "breaks every engine query in the org.", C.VERIFIED, "F-RLS-BREAK"))

    return items, summary
