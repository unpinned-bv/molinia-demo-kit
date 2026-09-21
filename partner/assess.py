#!/usr/bin/env python3
"""Scope a Snowflake → Molinia migration: the first handrail, before you quote.

It reads a client's Snowflake account (read-only metadata) and their dbt project
(statically, from disk) and writes a scope report a consultant can price from:
every object placed in one of four classes, the blockers that stop a migration
dead, a data-movement plan with the commands to run, a parity plan, and an
effort summary calibrated against the one migration this kit has measured.

    partner/assess.py --database MOLINIA_DEMO --dbt-project acme_shop --out ./scope
    partner/assess.py --no-snowflake --dbt-project ~/client/dbt --out /tmp/client

What it never does: write to Snowflake, run dbt, or call Molinia. Every
Snowflake statement is INFORMATION_SCHEMA or SHOW, inside a wall-clock budget,
and ACCOUNT_USAGE is read only when --sample-queries asks for it.

Exit codes: 0 complete · 1 report written but the inventory is incomplete
· 2 nothing to assess (usage, or both halves unavailable).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path
from typing import Dict, List, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from assesslib import __version__, classify as C, plan, report, scan           # noqa: E402
from assesslib import snowflake_inventory as sfi                               # noqa: E402
from assesslib.facts import AS_OF, FACTS                                       # noqa: E402

KIT_ROOT = _HERE.parent
DEFAULT_STAGE = "<DATABASE>.PUBLIC.MOLINIA_EXPORT"


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="partner/assess.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    p.add_argument("--snowflake-profile", metavar="PATH|NAME",
                   help="env file with SNOWFLAKE_ACCOUNT, _USER, _ROLE, _WAREHOUSE, "
                        "_PRIVATE_KEY_PATH (and optionally _PRIVATE_KEY_PASSPHRASE), or a name "
                        "resolved to <kit>/.secrets/<name>.env. Omit to use the environment and "
                        "the kit's .secrets/*.env, like tools/sf.py.")
    p.add_argument("--database", metavar="DB", help="Snowflake database to inventory")
    p.add_argument("--schemas", metavar="A,B", default="",
                   help="comma-separated schemas to scope the inventory to (default: all)")
    p.add_argument("--dbt-project", metavar="PATH", help="a local dbt project directory")
    p.add_argument("--out", metavar="PATH", required=True,
                   help="output base path: writes <PATH>.md and <PATH>.json side by side "
                        "(a directory gets assessment.md / assessment.json)")
    p.add_argument("--no-snowflake", action="store_true",
                   help="dbt-only mode: no connection, for when they have the repo but not yet "
                        "an account")
    p.add_argument("--sample-queries", type=int, nargs="?", const=20, default=0, metavar="N",
                   help="also read SNOWFLAKE.ACCOUNT_USAGE (last 7 days, N rows per aggregate). "
                        "Off by default: ACCOUNT_USAGE costs a warehouse and lags up to 45 min.")
    p.add_argument("--parquet-ratio", type=float, default=plan.PARQUET_RATIO, metavar="R",
                   help=f"unload size = Snowflake bytes x R (default {plan.PARQUET_RATIO}, "
                        f"measured on this kit's dataset)")
    p.add_argument("--stage", default="", metavar="STAGE",
                   help="Snowflake stage the generated COPY INTO statements unload to "
                        f"(default {DEFAULT_STAGE})")
    p.add_argument("--org-id", default="<ORG_ID>", help="Molinia org id for the generated commands")
    p.add_argument("--datasource-id", default="<DATASOURCE_ID>",
                   help="Molinia data source id for the generated ingest calls")
    p.add_argument("--location-id", default="<LOCATION_ID>",
                   help="Molinia storage location id for the generated ingest calls")
    p.add_argument("--detail-limit", type=int, default=25, metavar="N",
                   help="hits shown per rule in the Markdown (the JSON always has all of them)")
    p.add_argument("--budget-seconds", type=float, default=120.0, metavar="S",
                   help="wall-clock budget for the Snowflake probes (default 120)")
    p.add_argument("--quiet", action="store_true", help="only print the written paths")
    return p


def _out_paths(raw: str) -> tuple:
    p = Path(raw).expanduser()
    if p.is_dir() or raw.endswith("/"):
        base = p / "assessment"
    elif p.suffix.lower() in (".md", ".json"):
        base = p.with_suffix("")
    else:
        base = p
    return base.with_suffix(".md"), base.with_suffix(".json")


# --------------------------------------------------------------------------- assembly

def _volume(tables: List[dict], items: List[C.Item], ratio: float) -> dict:
    by_schema: Dict[str, dict] = {}
    klass_of = {i.name: i.klass for i in items}
    largest = []
    total_rows = total_bytes = 0
    for t in tables:
        schema = str(t.get("table_schema"))
        ttype = str(t.get("table_type") or "").upper()
        row = by_schema.setdefault(schema, {"schema": schema, "tables": 0, "views": 0,
                                            "rows": 0, "bytes": 0, "est_parquet_bytes": 0})
        if ttype == "VIEW":
            row["views"] += 1
            continue
        row["tables"] += 1
        rows_n = int(t.get("row_count") or 0)
        bytes_n = int(t.get("bytes") or 0)
        row["rows"] += rows_n
        row["bytes"] += bytes_n
        row["est_parquet_bytes"] += int(bytes_n * ratio)
        total_rows += rows_n
        total_bytes += bytes_n
        name = f"{schema}.{t.get('table_name')}"
        largest.append({"name": name, "rows": rows_n, "bytes": bytes_n,
                        "est_parquet_bytes": int(bytes_n * ratio),
                        "class": klass_of.get(name, "-")})
    largest.sort(key=lambda r: -r["bytes"])
    return {"by_schema": sorted(by_schema.values(), key=lambda r: -r["bytes"]),
            "largest": largest[:15], "total_rows": total_rows, "total_bytes": total_bytes,
            "total_est_parquet": int(total_bytes * ratio)}


_BODY_FIELDS = ("view_definition", "procedure_definition", "function_definition", "text", "body")


def _strip_bodies(rows: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """The raw inventory for the JSON, with SQL bodies replaced by a flag.

    The scope report is an inventory, not a copy of the client's source code: a
    view definition is scanned in memory and then dropped, so the JSON can be
    mailed around without shipping their SQL with it."""
    out: Dict[str, List[dict]] = {}
    for name, rows_in in rows.items():
        cleaned = []
        for r in rows_in:
            row = {}
            for k, v in r.items():
                if k in _BODY_FIELDS:
                    row[k + "_read"] = bool(v)
                else:
                    row[k] = v
            cleaned.append(row)
        out[name] = cleaned
    return out


def _by_kind(items: List[C.Item]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for i in items:
        row = out.setdefault(i.kind, {"total": 0, "needs_human": 0})
        row["total"] += 1
        if i.klass in C.NAMED_CLASSES:
            row["needs_human"] += 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1]["total"], kv[0])))


def _dbt_items(proj: scan.Project) -> List[C.Item]:
    items: List[C.Item] = []
    for n in proj.nodes + proj.macros:
        items.append(C.Item(f"dbt {n.kind}", n.name, n.klass, n.reason,
                            C.VERIFIED if n.klass != C.AUTOMATIC else C.VERIFIED,
                            ",".join(sorted({h.rule.id for h in n.hits if h.rule.weight})),
                            {"file": n.path, "flags": n.flags} if n.flags else {"file": n.path}))
    # project-level configs that no single node owns
    for h in proj.project_config_hits:
        if h.rule.klass in C.NAMED_CLASSES:
            items.append(C.Item("dbt project config", f"{h.path}:{h.line} {h.rule.construct}",
                                h.rule.klass, h.rule.why, h.rule.basis, h.rule.id))
    return items


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda *_a, **_k: None) if args.quiet else (lambda *a, **k: print(*a, **k, flush=True))
    warnings: List[str] = []
    schemas = [s.strip() for s in args.schemas.split(",") if s.strip()]

    # ---- dbt project ------------------------------------------------------
    proj: Optional[scan.Project] = None
    if args.dbt_project:
        log(f"dbt project: {args.dbt_project}")
        proj = scan.load_project(Path(args.dbt_project).expanduser())
        if not proj.ok:
            warnings.append(f"dbt project not assessed: {proj.error}")
            log(f"  ! {proj.error}")
            proj = None
        else:
            log(f"  {len(proj.models)} models, {len(proj.tests)} tests, "
                f"{len(proj.macros)} macro files")
            for wmsg in proj.warnings:
                warnings.append(f"dbt project: {wmsg}")

    # ---- Snowflake --------------------------------------------------------
    inv = sfi.Inventory()
    sf_ok = False
    sf_reason = "not requested (--no-snowflake)"
    if not args.no_snowflake:
        if not args.database:
            sf_reason = "--database was not given"
            warnings.append("Snowflake not inventoried: --database was not given "
                            "(use --no-snowflake to say that is deliberate)")
        else:
            try:
                loaded = sfi.load_profile(args.snowflake_profile, KIT_ROOT)
                log(f"snowflake: {len(loaded)} setting(s) from the profile, connecting…")
                conn = sfi.connect(args.database)
                try:
                    probes = sfi.build_probes(args.database, schemas, args.sample_queries)
                    inv = sfi.run_probes(conn, probes, args.budget_seconds,
                                         log=None if args.quiet else log, schemas=schemas)
                finally:
                    conn.close()
                inv.database = args.database
                inv.schemas_requested = schemas
                sf_ok = True
                log(f"  {inv.elapsed:.1f}s total")
                for name, err in sorted(inv.failed.items()):
                    warnings.append(f"Snowflake probe `{name}` did not return: {err}")
                if inv.skipped:
                    warnings.append("Snowflake probes skipped for the time budget: "
                                    + ", ".join(inv.skipped))
                if inv.account_scoped:
                    warnings.append(
                        "these are account-level objects, so the --schemas filter does not apply to "
                        "them and they are reported for the whole account: "
                        + ", ".join(inv.account_scoped))
                if not inv.rows.get("tables"):
                    warnings.append(f"no tables or views found in {args.database}"
                                    + (f" schemas {', '.join(schemas)}" if schemas else "")
                                    + ": check the database name, the schema filter and the role's "
                                      "grants before reading anything else in this report")
            except sfi.SnowflakeError as exc:
                sf_reason = str(exc)
                warnings.append(f"Snowflake not inventoried: {exc}")
                log(f"  ! {exc}")

    if proj is None and not sf_ok:
        print("error: nothing to assess — no usable dbt project and no Snowflake inventory.\n"
              "       give --dbt-project and/or --database, and check the error(s) above.",
              file=sys.stderr)
        return 2

    # ---- classify ---------------------------------------------------------
    def scan_sql(text: str, label: str):
        return scan.scan_text(text, label, "sql")

    model_names = [m.name for m in proj.models] if proj else []
    sf_items, column_types = (plan.classify_inventory(inv.rows, scan_sql, model_names)
                              if sf_ok else ([], []))
    dbt_items = _dbt_items(proj) if proj else []
    all_items = sf_items + dbt_items

    stage = args.stage or (f"{args.database}.PUBLIC.MOLINIA_EXPORT" if args.database
                           else DEFAULT_STAGE)
    moves = plan.build_moves(args.database or "<DATABASE>", inv.get("tables"), inv.get("columns"),
                             proj, args.parquet_ratio, stage, args.org_id, args.datasource_id,
                             args.location_id) if sf_ok else []
    parity_entries, keyless = plan.build_parity(proj, args.database or "", stage)
    projection = plan.request_projection(len(proj.models) if proj else 0,
                                         len(proj.tests) if proj else 0)
    blockers = plan.build_blockers(inv.rows, proj, moves, all_items, sf_ok)
    effort = plan.effort_summary(all_items, proj, projection)
    volume = _volume(inv.get("tables"), sf_items, args.parquet_ratio) if sf_ok else {}

    # ---- assemble ---------------------------------------------------------
    generated = _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    data: dict = {
        "scope": {"generated_at": generated, "tool_version": __version__, "facts_as_of": AS_OF,
                  "database": args.database or "", "schemas": schemas,
                  "dbt_project": str(Path(args.dbt_project).expanduser()) if args.dbt_project
                  else "", "sample_queries": args.sample_queries,
                  "parquet_ratio": args.parquet_ratio},
        "warnings": warnings,
        "classes": {
            "total": C.counts(all_items),
            "snowflake": C.counts(sf_items),
            "dbt": C.counts(dbt_items),
        },
        "items": [i.as_dict() for i in sorted(
            all_items, key=lambda i: (-C.severity(i.klass), i.kind, i.name))],
        "blockers": [b.as_dict() for b in blockers],
        "snowflake": {"available": sf_ok, "reason": sf_reason},
        "dbt": {"available": proj is not None,
                "reason": "" if proj else "no dbt project given or it could not be read"},
        "data_movement": {
            "how": (
                "There is no Snowflake reader and no Iceberg read: every table moves as files. "
                "COPY INTO an internal stage as Parquet, GET the files, put them in an EU bucket "
                "the client controls, then one ingest call per file. Ingest always lands in schema "
                "`main` and its default mode is CREATE OR REPLACE TABLE, so re-running a table is "
                "safe and replaces it wholesale. Order below: tables the dbt project reads first "
                "(they unblock the build), smallest first inside each group so the path is proven "
                "on a cheap table before a large one."),
            "size_assumption": plan.parquet_assumption(args.parquet_ratio),
            "moves": [], "total_bytes": 0, "total_est_parquet": 0,
            "get_and_upload": (
                f"# 1. pull the unloaded files out of the stage\n"
                f"snowsql -q \"GET @{stage}/raw/ 'file:///tmp/export/raw/'\"\n\n"
                f"# 2. put them in the EU bucket the client's Molinia org reads\n"
                f"#    (their storage connection: data source {args.datasource_id}, "
                f"location {args.location_id})\n"
                f"aws s3 cp --recursive /tmp/export/raw/ s3://<bucket>/<prefix>/raw/ "
                f"--endpoint-url <eu-endpoint>\n\n"
                f"# 3. ingest, one call per file, paced under 60 requests/min per IP"),
        },
        "parity": {
            "entries": [e.as_dict() for e in parity_entries],
            "keyless": keyless,
            "parity_yml": plan.parity_yaml(parity_entries) if parity_entries else "",
            "target_schema": (proj.target_schema if proj and proj.target_schema
                              else "<TARGET_SCHEMA>"),
        },
        "effort": effort,
        "facts_cited": sorted(FACTS),
        "method": [
            "Snowflake was read with INFORMATION_SCHEMA and SHOW only — metadata views, no table "
            "scan. Row counts and bytes are Snowflake's own maintained metadata. ACCOUNT_USAGE was "
            + ("read (--sample-queries)." if args.sample_queries else "not read."),
            "The dbt project was read from disk: no `dbt run`, no `dbt parse`, no connection. "
            "Jinja is not rendered, so a construct hidden in a macro is counted where it is "
            "defined and attributed to the models that call it.",
            "The Snowflake-only construct list comes from `agent/MIGRATION_RULES.md` and keeps its "
            "rule ids. A unit test fails if this tool cites an id the rulebook does not have.",
            "Comments and string literals are masked before matching, so `-- listagg` is not "
            "counted. Rules marked `counts: no` are low-precision hints: they are printed but do "
            "not move a node into a more expensive class.",
            "What this cannot tell you: whether a model's OUTPUT matches Snowflake. Only parity "
            "against a real answer key proves that, and a class of AUTOMATIC means the port is "
            "mechanical, not that the numbers will agree. Every silent-risk rule in section 5.2 "
            "is a place where they might not.",
            "It also cannot see: anything the connecting role cannot read, anything outside the "
            "database in scope, orchestration or BI that lives outside Snowflake, and how much of "
            "the estate is actually used — run with --sample-queries to get a week of query "
            "history, and ask for the BI inventory separately.",
        ],
    }
    if sf_ok:
        data["snowflake"].update({
            "account": inv.account, "role": inv.role, "warehouse": inv.warehouse,
            "elapsed_seconds": round(inv.elapsed, 1),
            "probes_failed": inv.failed, "probes_skipped": inv.skipped,
            "object_counts": {k: len(v) for k, v in sorted(inv.rows.items())},
            "objects": _strip_bodies(inv.rows),
            "volume_by_schema": volume["by_schema"], "largest_tables": volume["largest"],
            "total_rows": volume["total_rows"], "total_bytes": volume["total_bytes"],
            "total_est_parquet": volume["total_est_parquet"],
            "column_types": column_types,
            "by_kind": _by_kind(sf_items),
            "warehouses": inv.get("warehouses"),
            "query_history": {k: inv.get(k) for k in
                              ("query_history_by_type", "query_history_clients")
                              if inv.get(k)} if args.sample_queries else {},
        })
    if proj:
        labels = {"test": "singular test (.sql file)"}
        node_counts: Dict[str, int] = {}
        for n in proj.nodes:
            key = labels.get(n.kind, n.kind)
            node_counts[key] = node_counts.get(key, 0) + 1
        node_counts["macro file"] = len(proj.macros)
        node_counts["data test (generic + singular)"] = len(proj.tests)
        data["dbt"].update({
            "name": proj.name, "profile": proj.profile, "dbt_version": proj.dbt_version,
            "target_name": proj.target_name, "target_schema": proj.target_schema,
            "has_molinia_target": proj.has_molinia_target,
            "node_counts": sorted(node_counts.items(), key=lambda kv: (-kv[1], kv[0])),
            "materializations": scan.materialization_frequency(proj),
            "tests": scan.test_frequency(proj),
            "sources": proj.sources, "packages": proj.packages,
            "packages_installed": proj.packages_installed,
            "macro_callers": proj.macro_callers,
            "constructs": scan.construct_frequency(proj),
            "nodes": [n.as_dict() for n in proj.nodes + proj.macros],
            "alias_candidates": proj.alias_candidates,
            "regex_literals": proj.regex_literals,
            "detail_limit": args.detail_limit,
        })
    if moves:
        for idx, m in enumerate(moves, 1):
            d = m.as_dict()
            d["order_index"] = idx
            data["data_movement"]["moves"].append(d)
        data["data_movement"]["total_bytes"] = sum(m.bytes or 0 for m in moves if m.order < 3)
        data["data_movement"]["total_est_parquet"] = sum(m.est_parquet_bytes or 0
                                                         for m in moves if m.order < 3)
        data["data_movement"]["answer_key_bytes"] = sum(m.bytes or 0 for m in moves
                                                        if m.order == 3)
        data["data_movement"]["answer_key_est_parquet"] = sum(m.est_parquet_bytes or 0
                                                              for m in moves if m.order == 3)

    # ---- write ------------------------------------------------------------
    md_path, json_path = _out_paths(args.out)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report.render_markdown(data), encoding="utf-8")
    json_path.write_text(report.render_json(data), encoding="utf-8")
    log("")
    print(md_path)
    print(json_path)
    if not args.quiet:
        counts = data["classes"]["total"]
        log("")
        log("  " + "   ".join(f"{k}: {v}" for k, v in counts.items()))
        if warnings:
            log(f"  {len(warnings)} warning(s) — see section 0 of the report")
    return 1 if warnings else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
