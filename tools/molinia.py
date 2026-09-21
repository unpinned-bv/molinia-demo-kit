#!/usr/bin/env python3
"""Molinia side of the partner demo kit (org engine, dev environment).

  tools/molinia.py ingest [--only raw|expected] [--table NAME ...] [--from bucket|local] [--dry-run]
      Ingest every exported Parquet file through the engagement's data source
      and storage location: raw/<t>.parquet -> <source_schema>.<raw_prefix><t>,
      expected/<m>.parquet -> <source_schema>.<answer_key_prefix><m>.
      Server-side this is CREATE OR REPLACE TABLE, so re-running is safe. The
      file list comes from the bucket, i.e. the objects the server actually
      reads, so the unmasked Parquet never has to sit inside the kit (rule
      H11). --from local plans from exports/ instead. Each Parquet's own row
      count is checked against the server's.
  tools/molinia.py empty [--yes]
      Drop the ingested sources and the answer keys (<raw_prefix>* and
      <answer_key_prefix>* in the source schema), so the audience sees an
      empty org before the live ingest. A table under a masking policy is
      kept (its masking view shadows it for service accounts). Lists only,
      unless --yes. Presenter only: rule H6 forbids the agent. Never touches
      the dbt schemas.
  tools/molinia.py reset
      DROP SCHEMA IF EXISTS <dbt_schemas> CASCADE (the agent's dbt output).
      Never touches the source schema.
  tools/molinia.py status [--json]
      Ingested sources and answer keys in the source schema, and every relation
      in the dbt schemas, with row counts (two queries).
  tools/molinia.py query "<sql>" [--readonly] [--json]
      One statement; prints a table. --readonly uses MOLINIA_READONLY_KEY
      (the engagement's read-only service account) instead of MOLINIA_API_KEY.

Schemas, prefixes and ids come from engagement.yml (tools/_engagement.py).

Loads .secrets/*.env. Paces itself (<= 25 org-engine queries/min, <= 40 HTTP
requests/min, shared across invocations) and waits out HTTP 429 (Retry-After).
Never prints API keys.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _env  # noqa: E402
from _engagement import ENGAGEMENT, PROTECTED_SCHEMAS  # noqa: E402
from _molinia_client import (MoliniaClient, MoliniaError, format_table,  # noqa: E402
                             to_int)
from _sql import split_statements  # noqa: E402

# Everything client-specific below comes from engagement.yml.
EXPORT_ROOT = _env.EXPORT_ROOT
EXPORT_PREFIX = ENGAGEMENT.export_prefix
DATA_SOURCE_ID = ENGAGEMENT.data_source_id
LOCATION_ID = ENGAGEMENT.location_id
TARGET_PREFIX = ENGAGEMENT.target_prefix
DBT_SCHEMAS = ENGAGEMENT.dbt_schemas
SOURCE_SCHEMA = ENGAGEMENT.source_schema
TABLE_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")   # server IngestDto
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
SKIP_CATALOGS = frozenset({"temp", "system"})


def qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def qlit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------- catalogs

def parse_search_path(sp: Optional[str]) -> List[str]:
    """`__rd_lake,"org-12"` -> ['__rd_lake', 'org-12'] (catalog parts, lower-case)."""
    out: List[str] = []
    if not sp:
        return out
    parts: List[str] = []
    cur, inq = "", False
    for ch in sp:
        if ch == '"':
            inq = not inq
            cur += ch
        elif ch == "," and not inq:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if p.startswith('"'):
            end = p.find('"', 1)
            cat = p[1:end] if end > 0 else p[1:]
        else:
            cat = p.split(".", 1)[0]
        out.append(cat.lower())
    return out


def choose_catalogs(rows: Sequence[Tuple[str, str, str]], search_path: Optional[str],
                    current_db: Optional[str]) -> Dict[Tuple[str, str], Tuple[str, List[str]]]:
    """rows: (catalog, schema, name). For each (schema, name) (lower-cased) pick
    the catalog an unqualified two-part name most likely binds to: earliest in
    search_path, else current_database(), else alphabetical. Temp/system
    catalogs (masking views) are ignored. Returns {(schema, name): (catalog, others)}."""
    sp = parse_search_path(search_path)
    cdb = (current_db or "").lower()

    def rank(cat: str) -> Tuple[int, int, str]:
        c = cat.lower()
        if c in sp:
            return (0, sp.index(c), c)
        if c == cdb:
            return (1, 0, c)
        return (2, 0, c)

    grouped: Dict[Tuple[str, str], List[str]] = {}
    for cat, schema, name in rows:
        if (cat or "").lower() in SKIP_CATALOGS:
            continue
        key = (schema.lower(), name.lower())
        cats = grouped.setdefault(key, [])
        if cat not in cats:
            cats.append(cat)
    out: Dict[Tuple[str, str], Tuple[str, List[str]]] = {}
    for key, cats in grouped.items():
        ordered = sorted(cats, key=rank)
        out[key] = (ordered[0], ordered[1:])
    return out


# --------------------------------------------------------------------------- ingest

def plan_ingest(root: Path = EXPORT_ROOT, only: Optional[str] = None,
                tables: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    kinds = [only] if only else ["raw", "expected"]
    wanted = {t.lower() for t in tables} if tables else None
    plan: List[Dict[str, Any]] = []
    for kind in kinds:
        d = root / kind
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.parquet")):
            name = f.stem.lower()
            target = TARGET_PREFIX[kind] + name
            if wanted is not None and name not in wanted and target not in wanted:
                continue
            if not TABLE_NAME_RE.match(target):
                raise ValueError(f"{f.name}: target table {target!r} is not a valid identifier")
            plan.append({
                "kind": kind,
                "file": f,
                "target": target,
                "file_path": f"{EXPORT_PREFIX}/{kind}/{f.name}",
                "local_rows": _local_rows(f),
                "source": "local",
            })
    return plan


def plan_ingest_bucket(s3, bucket: str, only: Optional[str] = None,
                       tables: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    """The same plan as plan_ingest, built from the bucket listing instead of
    exports/. The bucket holds exactly the objects the server ingests, so this
    is the ground truth; a local tree can lag behind it or be absent (it is
    moved out of the kit after upload, rule H11)."""
    kinds = [only] if only else ["raw", "expected"]
    wanted = {t.lower() for t in tables} if tables else None
    plan: List[Dict[str, Any]] = []
    for kind in kinds:
        prefix = f"{EXPORT_PREFIX}/{kind}/"
        keys: List[str] = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []) or []:
                rest = o["Key"][len(prefix):]
                # direct children only: a nested key is not part of the export layout
                if rest and "/" not in rest and rest.lower().endswith(".parquet"):
                    keys.append(o["Key"])
        for key in sorted(keys):
            fname = key.rsplit("/", 1)[-1]
            name = fname[: -len(".parquet")].lower()
            target = TARGET_PREFIX[kind] + name
            if wanted is not None and name not in wanted and target not in wanted:
                continue
            if not TABLE_NAME_RE.match(target):
                raise ValueError(f"{fname}: target table {target!r} is not a valid identifier")
            plan.append({
                "kind": kind,
                "file": None,
                "target": target,
                "file_path": key,
                "local_rows": _bucket_rows(s3, bucket, key),
                "source": "bucket",
            })
    return plan


def _bucket_rows(s3, bucket: str, key: str) -> Optional[int]:
    """The object's own Parquet row count, read in memory (the export is a few MB)."""
    try:
        import io
        import pyarrow.parquet as pq
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return int(pq.read_metadata(io.BytesIO(body)).num_rows)
    except Exception:
        return None


def _bucket_client(s3=None):
    """(bucket name, S3 client) from .secrets/minio.env. Raises EnvError, naming
    the missing variable, when the bucket is not configured."""
    _env.load_env()
    v = _env.require("MINIO_ENDPOINT", "MINIO_BUCKET", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
    if s3 is None:
        import minio_upload
        s3 = minio_upload.make_client(v)
    return v["MINIO_BUCKET"], s3


def _local_rows(f: Path) -> Optional[int]:
    try:
        import pyarrow.parquet as pq
        return int(pq.read_metadata(f).num_rows)
    except Exception:
        return None


def cmd_ingest(args, client: Optional[MoliniaClient] = None, s3=None) -> int:
    # The ingest API takes a bare table name (IngestDto) and lands it in the org's
    # default schema. A different source_schema in engagement.yml would make every
    # line below lie about where the table went, so refuse instead.
    if SOURCE_SCHEMA != "main":
        raise ValueError(f"engagement source_schema is {SOURCE_SCHEMA!r}, but the ingest API has no "
                         "schema parameter: tables land in the org's default schema, main")
    source = getattr(args, "source", "bucket")
    if source == "local":
        plan = plan_ingest(EXPORT_ROOT, args.only, args.table)
        where = str(EXPORT_ROOT)
        hint = ("move ~/molinia-demo-exports back to exports/, or drop --from local to plan from "
                "the bucket")
    else:
        bucket, s3 = _bucket_client(s3)
        plan = plan_ingest_bucket(s3, bucket, args.only, args.table)
        where = f"s3://{bucket}/{EXPORT_PREFIX}/"
        hint = "run `make sf-unload` then `make minio-upload` first"
    if not plan:
        print(f"error: no Parquet files to ingest under {where}"
              + (f" (--only {args.only})" if args.only else "")
              + (f" matching {args.table}" if args.table else "")
              + f"; {hint}.", file=sys.stderr)
        return 1
    if args.dry_run:
        rows = [[p["file_path"], f"{SOURCE_SCHEMA}.{p['target']}",
                 "" if p["local_rows"] is None else f"{p['local_rows']:,}"] for p in plan]
        print(f"planned from {where}")
        print(format_table(["bucket key (filePath)", "target", "parquet rows"], rows, width=80,
                           align_right=[False, False, True]))
        print(f"dry run: {len(plan)} ingests via data source {args.data_source}, location {args.location}; "
              "nothing sent")
        return 0

    client = client or MoliniaClient.from_env()
    failures = 0
    results = []
    for p in plan:
        try:
            res = client.ingest(args.data_source, p["target"], p["file_path"],
                                location_id=args.location, file_format="parquet")
            n = to_int(res.get("rowCount"))
            ok = p["local_rows"] is None or n == p["local_rows"]
            status = "ok  " if ok else "ROWS"
            if not ok:
                failures += 1
            secs = (to_int(res.get("durationMs")) or 0) / 1000
            local = "" if p["local_rows"] is None else f" ({p.get('source', 'local')} {p['local_rows']:,})"
            print(f"{status} {SOURCE_SCHEMA}.{p['target']:<28} {n if n is not None else '?':>8} rows{local}"
                  f"  {secs:5.1f}s", flush=True)
            results.append((p["target"], n))
        except MoliniaError as exc:
            failures += 1
            print(f"FAIL {SOURCE_SCHEMA}.{p['target']:<28} {exc}", flush=True)
    print(f"{len(plan) - failures}/{len(plan)} ingested")
    return 1 if failures else 0


# --------------------------------------------------------------------------- empty

def empty_list_sql(eng=ENGAGEMENT) -> str:
    """Every relation `empty` may drop: engagement-prefixed names in the source
    schema, across catalogs (the lake and the org file can both hold one)."""
    prefixes = " OR ".join(f"starts_with(lower(table_name), {qlit(p)})"
                           for p in (eng.raw_prefix, eng.answer_key_prefix))
    return ("SELECT table_catalog, table_schema, table_name, table_type "
            "FROM information_schema.tables "
            f"WHERE lower(table_schema) = {qlit(eng.source_schema)} AND ({prefixes}) "
            "ORDER BY 3, 1")


def masked_names(rows: Sequence[Sequence[Any]], eng=ENGAGEMENT) -> List[str]:
    """Names in the source schema that a temp/system view shadows: the server's
    masking views. A service account's name for such a table binds to its
    masking view, so a DROP cannot reach the table (measured 2026-09-21:
    "Existing object raw_customer_contacts is of type View, trying to drop type
    Table"), and `empty` keeps it. The consequence is the useful one: a masking
    policy is never left pointing at a missing table."""
    def names(in_skip: bool) -> set:
        return {str(name).lower() for cat, schema, name, _ in rows
                if str(schema).lower() == eng.source_schema
                and ((cat or "").lower() in SKIP_CATALOGS) == in_skip}
    return sorted(names(True) & names(False))


def plan_empty(rows: Sequence[Sequence[Any]], eng=ENGAGEMENT) -> List[Tuple[str, str]]:
    """[(name, 'TABLE'|'VIEW')] to drop, one per name. Masking views live in the
    temp/system catalogs and belong to the server: never dropped, and neither is
    the table they mask (masked_names). Anything not carrying an engagement
    prefix is skipped even if the query returned it."""
    masked = set(masked_names(rows, eng))
    picked: Dict[str, Tuple[str, str]] = {}
    for cat, schema, name, ttype in rows:
        if (cat or "").lower() in SKIP_CATALOGS or str(schema).lower() != eng.source_schema:
            continue
        if str(name).lower() in masked:
            continue
        if not str(name).lower().startswith((eng.raw_prefix, eng.answer_key_prefix)):
            continue
        kind = "VIEW" if str(ttype).upper() == "VIEW" else "TABLE"
        picked.setdefault(str(name).lower(), (str(name), kind))
    return [picked[k] for k in sorted(picked)]


def drop_input_sql(name: str, kind: str = "TABLE", eng=ENGAGEMENT) -> str:
    """DROP for one ingested input. Refuses anything outside the source schema's
    two prefixes, so no config or listing mistake can reach a client table."""
    n = name.strip()
    if not n.lower().startswith((eng.raw_prefix, eng.answer_key_prefix)):
        raise ValueError(f"refusing to drop {n!r}: it carries neither engagement prefix "
                         f"({eng.raw_prefix!r}, {eng.answer_key_prefix!r})")
    if not TABLE_NAME_RE.match(n):
        raise ValueError(f"refusing to drop table with unexpected name {n!r}")
    if kind not in ("TABLE", "VIEW"):
        raise ValueError(f"unexpected relation kind {kind!r}")
    return f"DROP {kind} IF EXISTS {qident(eng.source_schema)}.{qident(n)}"


def cmd_empty(args, client: Optional[MoliniaClient] = None) -> int:
    raw, key = ENGAGEMENT.raw_prefix, ENGAGEMENT.answer_key_prefix
    # A prefix without a separator ('r', 'sf') would match unrelated tables.
    for label, prefix in (("raw_prefix", raw), ("answer_key_prefix", key)):
        if not prefix.endswith("_"):
            raise ValueError(f"engagement {label} {prefix!r} does not end in '_'; `empty` refuses a "
                             "prefix that could match tables it was not meant to drop")
    client = client or MoliniaClient.from_env()
    rows = client.execute(empty_list_sql(), idempotent=True).get("rows", [])
    masked = masked_names(rows)
    targets = plan_empty(rows)
    for name in masked:
        print(f"kept: {SOURCE_SCHEMA}.{name} (masked: the masking view shadows it for every service "
              "account, so no DROP can reach it, and its policy keeps its table)")
    if not targets:
        print(f"nothing {'else ' if masked else ''}to drop: no other {raw}* / {key}* tables in "
              f"{SOURCE_SCHEMA}")
        return 0
    print(f"{len(targets)} relation(s) to drop in {SOURCE_SCHEMA}: "
          + ", ".join(n for n, _ in targets))
    if not args.yes:
        print("dry run: nothing dropped. Re-run with --yes (or `make molinia-empty`) to drop them.")
        return 0

    failures = 0
    for name, kind in targets:
        sql = drop_input_sql(name, kind)
        try:
            client.execute(sql, idempotent=True)
            print(f"ok   {sql}", flush=True)
        except MoliniaError as exc:
            failures += 1
            print(f"FAIL {sql}: {exc}", flush=True)
    left = plan_empty(client.execute(empty_list_sql(), idempotent=True).get("rows", []))
    if left:
        failures += 1
        for name, _ in left:
            print(f"STILL THERE: {SOURCE_SCHEMA}.{name} (not reachable by an unqualified DROP)")
    else:
        kept = f"; kept {len(masked)} masked" if masked else ""
        print(f"verified: no other {raw}* / {key}* tables left in {SOURCE_SCHEMA}{kept}; "
              f"{ENGAGEMENT.schema_list()} untouched")
    print("note: an RLS policy on a dropped table would break every engine query until the ingest "
          "recreates it (rule H7). Masked tables are kept, so masking never loses its table.")
    return 1 if failures else 0


# --------------------------------------------------------------------------- reset

def drop_schema_sql(schema: str, cascade: bool = True) -> str:
    s = schema.strip()
    if s.lower() in PROTECTED_SCHEMAS:
        raise ValueError(f"refusing to drop protected schema {s!r}")
    if not IDENT_RE.match(s.lower()):
        raise ValueError(f"refusing to drop schema with unexpected name {s!r}")
    return f"DROP SCHEMA IF EXISTS {qident(s)}" + (" CASCADE" if cascade else "")


def _relations_in_schema(client: MoliniaClient, schema: str) -> List[Tuple[str, str, str]]:
    res = client.execute(
        "SELECT table_catalog, table_name, table_type FROM information_schema.tables "
        f"WHERE lower(table_schema) = {qlit(schema.lower())}", idempotent=True)
    return [(r[0], r[1], r[2]) for r in res.get("rows", [])]


def cmd_reset(args, client: Optional[MoliniaClient] = None) -> int:
    schemas = list(DBT_SCHEMAS)
    for s in schemas:  # validate everything before sending anything
        drop_schema_sql(s)
    client = client or MoliniaClient.from_env()
    failures = 0
    for s in schemas:
        sql = drop_schema_sql(s)
        try:
            client.execute(sql, idempotent=True)
            print(f"ok   {sql}", flush=True)
            continue
        except MoliniaError as exc:
            print(f"FAIL {sql}: {exc}", flush=True)
        # Fallback: drop the relations one by one, then the empty schema.
        try:
            rels = _relations_in_schema(client, s)
            for _cat, name, ttype in sorted(rels, key=lambda r: (r[2] != "VIEW", r[1])):
                kind = "VIEW" if ttype == "VIEW" else "TABLE"
                client.execute(f"DROP {kind} IF EXISTS {qident(s)}.{qident(name)}", idempotent=True)
                print(f"ok   DROP {kind} IF EXISTS {s}.{name}", flush=True)
            client.execute(drop_schema_sql(s, cascade=False), idempotent=True)
            print(f"ok   {drop_schema_sql(s, cascade=False)}", flush=True)
        except MoliniaError as exc:
            failures += 1
            print(f"FAIL fallback for schema {s}: {exc}", flush=True)
    # Verify: nothing named like the dbt schemas may remain in any real catalog.
    try:
        in_list = ", ".join(qlit(s) for s in schemas)
        res = client.execute(
            "SELECT catalog_name, schema_name FROM information_schema.schemata "
            f"WHERE lower(schema_name) IN ({in_list})", idempotent=True)
        left = [(r[0], r[1]) for r in res.get("rows", []) if str(r[0]).lower() not in SKIP_CATALOGS]
        if left:
            failures += 1
            for cat, sch in left:
                print(f"STILL THERE: schema {sch} in catalog {cat} (not reachable by an unqualified DROP)")
        else:
            print(f"verified: no {ENGAGEMENT.schema_list()} schema left; {SOURCE_SCHEMA} untouched")
    except MoliniaError as exc:
        failures += 1
        print(f"FAIL verification query: {exc}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- status

def status_list_sql(eng=ENGAGEMENT) -> str:
    """Relations worth listing: everything in the dbt schemas, plus the ingested
    sources and the answer keys in the source schema."""
    schemas = ", ".join(qlit(s) for s in eng.dbt_schemas)
    prefixes = " OR ".join(f"starts_with(lower(table_name), {qlit(p)})"
                           for p in (eng.raw_prefix, eng.answer_key_prefix))
    return (
        "SELECT table_catalog, table_schema, table_name, table_type, "
        "current_database() AS current_db, current_setting('search_path') AS search_path "
        "FROM information_schema.tables "
        f"WHERE lower(table_schema) IN ({schemas}) "
        f"OR (lower(table_schema) = {qlit(eng.source_schema)} AND ({prefixes})) "
        "ORDER BY 2, 3, 1"
    )


_SCHEMA_ORDER = ENGAGEMENT.schema_order


def count_sql(relations: Sequence[Tuple[str, str]]) -> str:
    parts = [
        f"SELECT {qlit(s)} AS table_schema, {qlit(t)} AS table_name, count(*) AS row_count "
        f"FROM {qident(s)}.{qident(t)}"
        for s, t in relations
    ]
    return " UNION ALL ".join(parts)


def gather_status(client) -> List[Dict[str, Any]]:
    res = client.execute(status_list_sql(), idempotent=True)
    rows = res.get("rows", [])
    if not rows:
        return []
    current_db, search_path = rows[0][4], rows[0][5]
    types: Dict[Tuple[str, str], str] = {}
    display: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for r in rows:
        k = (str(r[1]).lower(), str(r[2]).lower())
        if str(r[0]).lower() in SKIP_CATALOGS:
            continue
        types.setdefault(k, r[3])
        display.setdefault(k, (r[1], r[2]))
    chosen = choose_catalogs([(r[0], r[1], r[2]) for r in rows], search_path, current_db)
    rels = sorted(chosen, key=lambda k: (_SCHEMA_ORDER.get(k[0], 9), k[1]))
    out: List[Dict[str, Any]] = []
    for k in rels:
        cat, others = chosen[k]
        out.append({"schema": display[k][0], "name": display[k][1],
                    "type": "view" if types.get(k) == "VIEW" else "table",
                    "catalog": cat, "also_in": others, "rows": None, "error": None})
    try:
        cres = client.execute(count_sql([(o["schema"], o["name"]) for o in out]), idempotent=True)
        counts = {(str(r[0]).lower(), str(r[1]).lower()): to_int(r[2]) for r in cres.get("rows", [])}
        for o in out:
            o["rows"] = counts.get((o["schema"].lower(), o["name"].lower()))
    except MoliniaError:
        # One broken relation (e.g. a view over a dropped table) fails the union;
        # count one by one so the others still report.
        for o in out:
            try:
                r = client.execute(count_sql([(o["schema"], o["name"])]), idempotent=True)
                o["rows"] = to_int(r["rows"][0][2])
            except MoliniaError as exc:
                o["error"] = str(exc)
    return out


def cmd_status(args, client: Optional[MoliniaClient] = None) -> int:
    client = client or MoliniaClient.from_env()
    rels = gather_status(client)
    if args.json:
        print(json.dumps(rels, indent=2, default=str))
        return 0
    if not rels:
        print(f"no {ENGAGEMENT.raw_prefix}* / {ENGAGEMENT.answer_key_prefix}* tables in "
              f"{SOURCE_SCHEMA} and no relations in {ENGAGEMENT.schema_list()}")
        return 0
    table = []
    for o in rels:
        rows = o["error"] and "ERROR" or ("?" if o["rows"] is None else f"{o['rows']:,}")
        note = ("also in " + ", ".join(o["also_in"])) if o["also_in"] else ""
        if o["error"]:
            note = (note + "; " if note else "") + o["error"][:80]
        table.append([f"{o['schema']}.{o['name']}", o["type"], rows, note])
    print(format_table(["relation", "type", "rows", "note"], table, width=90,
                       align_right=[False, False, True, False]))
    by_schema: Dict[str, int] = {}
    for o in rels:
        by_schema[o["schema"].lower()] = by_schema.get(o["schema"].lower(), 0) + 1
    print("  ".join(f"{s}: {by_schema.get(s, 0)}" for s in (SOURCE_SCHEMA,) + tuple(DBT_SCHEMAS)))
    return 1 if any(o["error"] for o in rels) else 0


# --------------------------------------------------------------------------- query

def cmd_query(args, client: Optional[MoliniaClient] = None) -> int:
    stmts = split_statements(args.sql, dialect="duckdb")
    if len(stmts) != 1:
        print(f"error: expected exactly one statement, got {len(stmts)} (one statement per request)",
              file=sys.stderr)
        return 2
    if args.readonly:
        print(f"[using MOLINIA_READONLY_KEY: service account "
              f"{ENGAGEMENT.service_account_readonly}]", file=sys.stderr)
    client = client or MoliniaClient.from_env(readonly=args.readonly)
    try:
        res = client.execute(stmts[0])
    except MoliniaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    cols = res.get("columns") or []
    rows = res.get("rows") or []
    if args.json:
        print(json.dumps({k: res.get(k) for k in ("columns", "rows", "rowCount", "durationMs", "warnings")
                          if k in res}, indent=2, default=str))
        return 0
    if cols:
        shown = rows[: args.max_rows]
        print(format_table(cols, shown, width=args.width))
        extra = f", showing {len(shown)}" if len(rows) > len(shown) else ""
        print(f"({len(rows):,} rows{extra}; {res.get('durationMs', '?')} ms)")
    else:
        print(f"ok ({res.get('rowCount', 0)} rows affected; {res.get('durationMs', '?')} ms)")
    for w in res.get("warnings") or []:
        print(f"warning: {w}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools/molinia.py",
        description="Molinia side of the demo kit (loads .secrets/*.env; paced under the API rate limits; "
                    "never prints API keys).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{ingest,empty,reset,status,query}")

    i = sub.add_parser("ingest",
                       help=f"ingest exported Parquet into {SOURCE_SCHEMA}.{ENGAGEMENT.raw_prefix}* / "
                            f"{SOURCE_SCHEMA}.{ENGAGEMENT.answer_key_prefix}*",
                       description=f"Ingest {EXPORT_PREFIX}/{{raw,expected}}/*.parquet from the bucket into "
                                   f"{SOURCE_SCHEMA}.{ENGAGEMENT.raw_prefix}<table> / "
                                   f"{SOURCE_SCHEMA}.{ENGAGEMENT.answer_key_prefix}<model> through data source "
                                   f"{DATA_SOURCE_ID}. The file list is the bucket listing, so the Parquet "
                                   "never has to be inside the kit. CREATE OR REPLACE on the server: safe to "
                                   "re-run.")
    i.add_argument("--only", choices=["raw", "expected"], help="ingest only one half")
    i.add_argument("--table", action="append", metavar="NAME",
                   help="only this file/table (repeatable), e.g. --table orders or --table sf_fct_orders")
    i.add_argument("--from", dest="source", choices=["bucket", "local"], default="bucket",
                   help="where the file list comes from: the bucket (default) or exports/")
    i.add_argument("--dry-run", action="store_true",
                   help="print the plan; nothing is ingested (the bucket is still listed)")
    i.add_argument("--data-source", type=int, default=DATA_SOURCE_ID,
                   help=f"data source id (default {DATA_SOURCE_ID})")
    i.add_argument("--location", type=int, default=LOCATION_ID,
                   help=f"storage location id (default {LOCATION_ID})")
    i.set_defaults(func=cmd_ingest)

    e = sub.add_parser("empty",
                       help=f"drop the ingested sources and answer keys ({ENGAGEMENT.raw_prefix}* / "
                            f"{ENGAGEMENT.answer_key_prefix}* in {SOURCE_SCHEMA}); presenter only",
                       description=f"Drop {SOURCE_SCHEMA}.{ENGAGEMENT.raw_prefix}* and "
                                   f"{SOURCE_SCHEMA}.{ENGAGEMENT.answer_key_prefix}*, so the org is empty "
                                   "before the live ingest. Tables under a masking policy are kept: the "
                                   "masking view shadows them for every service account. Lists what it "
                                   f"would drop unless --yes. Never touches {ENGAGEMENT.schema_list()}. "
                                   "The agent must never run it (rule H6).")
    e.add_argument("--yes", action="store_true", help="drop them (default: list only)")
    e.set_defaults(func=cmd_empty)

    r = sub.add_parser("reset",
                       help=f"drop schemas {', '.join(DBT_SCHEMAS)} (never {SOURCE_SCHEMA})",
                       description=f"DROP SCHEMA IF EXISTS {ENGAGEMENT.schema_list()} CASCADE, so the demo "
                                   f"can run again. Refuses to touch {SOURCE_SCHEMA}.")
    r.set_defaults(func=cmd_reset)

    s = sub.add_parser("status", help="tables and views with row counts",
                       description=f"{ENGAGEMENT.raw_prefix}* / {ENGAGEMENT.answer_key_prefix}* tables in "
                                   f"{SOURCE_SCHEMA} and all relations in {ENGAGEMENT.schema_list()}, "
                                   f"with row counts (two queries).")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_status)

    q = sub.add_parser("query", help="run one SQL statement and print the result",
                       description="Run ONE statement on the org engine and print a table.")
    q.add_argument("sql", help="the statement (quote it)")
    q.add_argument("--readonly", action="store_true",
                   help=f"use MOLINIA_READONLY_KEY ({ENGAGEMENT.service_account_readonly}) "
                        "instead of MOLINIA_API_KEY")
    q.add_argument("--json", action="store_true", help="print columns/rows as JSON")
    q.add_argument("--max-rows", type=int, default=200, help="rows to print (default 200)")
    q.add_argument("--width", type=int, default=60, help="max characters per cell (default 60)")
    q.set_defaults(func=cmd_query)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _env.EnvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except MoliniaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
