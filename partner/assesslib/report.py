"""Render the assessment as Markdown and as JSON.

The Markdown is what a consultant reads and sends to the client. The JSON is the
same data for a pricing sheet or a pipeline. Neither adds a fact the rest of the
library did not establish: every limitation printed here carries the id of the
fact or the rulebook rule behind it.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import classify as C
from .facts import AS_OF, FACTS

MAX_CELL = 110


def human_bytes(n: Optional[float]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def num(n: Optional[float]) -> str:
    return "-" if n is None else f"{int(n):,}"


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= MAX_CELL else s[: MAX_CELL - 1] + "…"


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    rows = [list(r) for r in rows]
    if not rows:
        return "_none_\n"
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return "\n".join(out) + "\n"


def code(text: str, lang: str = "sql") -> str:
    return f"```{lang}\n{text.rstrip()}\n```\n"


def _class_table(counts: Dict[str, int]) -> str:
    meaning = {
        C.AUTOMATIC: "the agent ports it with no judgement",
        C.AGENT_PLUS_REVIEW: "the agent ports it, a senior checks it",
        C.REDESIGN: "a human reworks it before it can land",
        C.NOT_SUPPORTED_TODAY: "it does not land on Molinia today",
    }
    total = sum(counts.values()) or 1
    return table(["class", "count", "share", "what it means"],
                 [[k, counts.get(k, 0), f"{100 * counts.get(k, 0) / total:.0f}%", meaning[k]]
                  for k in C.CLASSES])


def render_markdown(data: dict) -> str:
    """`data` is the dict that render_json serialises: one shape, two outputs."""
    scope = data["scope"]
    out: List[str] = []
    w = out.append

    title = scope.get("database") or "(no Snowflake)"
    project = scope.get("dbt_project") or "(no dbt project)"
    w("# Snowflake to Molinia: migration assessment\n")
    w(f"**Scope:** Snowflake database `{title}`"
      + (f", schemas {', '.join(scope['schemas'])}" if scope.get("schemas") else " (all schemas)")
      + f" · dbt project `{project}`\n")
    w(f"**Generated:** {scope['generated_at']} by `partner/assess.py` "
      f"v{scope['tool_version']} · Molinia facts as of {AS_OF}\n")
    w("This report says what a Snowflake estate costs to move to Molinia, in counts and named "
      "objects. It does not invent an hours-per-object rate: section 9 gives the one measured "
      "anchor there is, and you price off that plus your own review rate.\n")

    if data.get("warnings"):
        w("## 0. Read this first: the inventory is incomplete\n")
        for x in data["warnings"]:
            w(f"- {x}")
        w("")

    # ---- 1. headline ------------------------------------------------------
    w("## 1. The headline\n")
    w("**Everything in scope, by class**\n")
    w(_class_table(data["classes"]["total"]))
    w("**Snowflake objects**\n")
    w(_class_table(data["classes"]["snowflake"]))
    built = len([i for i in data["items"] if "dbt-built" in i["kind"]])
    if built:
        w(f"_{built} of those are relations the dbt project itself builds (kind `table "
          f"(dbt-built)` / `view (dbt-built)`). They are counted AUTOMATIC because they are zero "
          f"migration work — dbt rebuilds them on Molinia — so do not read the AUTOMATIC share as "
          f"\"{built} objects the agent ports\". Section 4.4 separates them._\n")
    w("**dbt nodes** (models, tests, seeds, snapshots, macros)\n")
    w(_class_table(data["classes"]["dbt"]))

    # ---- 2. blockers ------------------------------------------------------
    w("## 2. Blockers\n")
    w("Each one is a thing that stops a migration dead, the fact behind it, and what to do "
      "instead. `prerequisite` means fix it before the first build; `watch` means tell the client "
      "before their security review does.\n")
    for b in data["blockers"]:
        w(f"### {b['title']}  ·  _{b['severity']}_\n")
        w(f"- **Triggered by:** {b['triggered_by']}")
        w(f"- **The fact:** {b['evidence']}")
        w(f"- **Do this instead:** {b['do_instead']}\n")

    # ---- 3. named objects -------------------------------------------------
    w("## 3. The objects that need a human\n")
    for klass in C.NAMED_CLASSES:
        named = [i for i in data["items"] if i["class"] == klass]
        w(f"### {klass} — {len(named)} object(s)\n")
        if not named:
            w("_none_\n")
            continue
        w(table(["kind", "object", "why", "basis"],
                [[i["kind"], i["name"], i["reason"], i["basis"]] for i in named]))
    w("`basis: verified` means a fact established by a code read or a live run. "
      "`basis: inference` means an engineering judgement this kit has not proven — challenge those "
      "before they go in a fixed price.\n")

    # ---- 4. Snowflake -----------------------------------------------------
    sf = data.get("snowflake") or {}
    w("## 4. Snowflake inventory\n")
    if not sf.get("available"):
        w(f"_Not inventoried: {sf.get('reason', 'no connection')}._\n")
    else:
        w(f"Account `{sf['account']}`, role `{sf['role']}`, warehouse `{sf['warehouse']}`. "
          f"{sf['elapsed_seconds']}s of read-only metadata queries "
          f"(INFORMATION_SCHEMA and SHOW; ACCOUNT_USAGE only when asked).\n")
        if sf.get("probes_failed"):
            w("**Metadata this role could not read** — the corresponding objects are missing from "
              "every count in this report:\n")
            w(table(["probe", "error"], sorted(sf["probes_failed"].items())))
        w("### 4.1 Volume by schema\n")
        w(table(["schema", "tables", "views", "rows", "Snowflake bytes", "est. Parquet"],
                [[r["schema"], r["tables"], r["views"], num(r["rows"]),
                  human_bytes(r["bytes"]), human_bytes(r["est_parquet_bytes"])]
                 for r in sf["volume_by_schema"]]))
        w(f"Totals: {num(sf['total_rows'])} rows, {human_bytes(sf['total_bytes'])} in Snowflake, "
          f"about {human_bytes(sf['total_est_parquet'])} unloaded.\n")
        w(f"> **Assumption.** {data['data_movement']['size_assumption']}\n")
        w("### 4.2 Largest tables\n")
        w(table(["table", "rows", "Snowflake bytes", "est. Parquet", "class"],
                [[r["name"], num(r["rows"]), human_bytes(r["bytes"]),
                  human_bytes(r["est_parquet_bytes"]), r["class"]]
                 for r in sf["largest_tables"]]))
        w("### 4.3 Column types\n")
        w("Every type in scope and what it becomes. A type with no Molinia equivalent makes its "
          "whole table NOT_SUPPORTED_TODAY.\n")
        w(table(["Snowflake type", "columns", "tables", "becomes", "class", "rule", "note"],
                [[r["snowflake_type"], r["columns"], r["tables"], r["molinia_type"], r["class"],
                  r["rule"] or "-", r["note"] or ""] for r in sf["column_types"]]))
        w("### 4.4 Objects by kind\n")
        w(table(["kind", "count", "of which REDESIGN or NOT_SUPPORTED"],
                [[k, v["total"], v["needs_human"]] for k, v in sf["by_kind"].items()]))
        if sf.get("warehouses"):
            w("### 4.5 Warehouses (sizing input, not migrated objects)\n")
            w(table(["name", "size", "auto suspend", "state"],
                    [[r.get("name"), r.get("size"), r.get("auto_suspend"), r.get("state")]
                     for r in sf["warehouses"]]))
        if sf.get("query_history"):
            w("### 4.6 ACCOUNT_USAGE sample\n")
            w("Last 7 days for this database. ACCOUNT_USAGE lags by up to 45 minutes, and it only "
              "shows what ran against Snowflake — a BI tool that hits an extract or a cache does "
              "not appear here. Read the client list as a floor, not a census.\n")
            for name, rows in sf["query_history"].items():
                w(f"**{name}**\n")
                if rows:
                    w(table(list(rows[0].keys()), [list(r.values()) for r in rows]))
                else:
                    w("_none_\n")

    # ---- 5. dbt -----------------------------------------------------------
    d = data.get("dbt") or {}
    w("## 5. The dbt project\n")
    if not d.get("available"):
        w(f"_Not assessed: {d.get('reason', 'no project given')}._\n")
    else:
        w(f"`{d['name']}` · profile `{d['profile']}` · {d['dbt_version'] or 'no version pin'}"
          + (f" · target `{d['target_name']}` (schema `{d['target_schema']}`)"
             if d.get("target_name") else "") + "\n")
        w("### 5.1 What is in it\n")
        w(table(["node kind", "count"], d["node_counts"]))
        w("**Models by materialization**\n")
        w(table(["materialization", "count"], d["materializations"]))
        w("**Tests by type**\n")
        w(table(["test", "count"], d["tests"]))
        if d["sources"]:
            w("**Sources**\n")
            w(table(["source", "database", "schema", "tables", "declared in"],
                    [[s["name"], s.get("database") or "-", s.get("schema") or "-",
                      len(s.get("tables") or []), s["file"]] for s in d["sources"]]))
        if d["packages"]:
            w("**Packages** — none of this has been run against dbt-molinia.\n")
            w(table(["package", "version", "from", "checked out"],
                    [[p.get("package") or p.get("git") or p.get("local"),
                      p.get("version") or p.get("revision") or "-", p["source"],
                      "yes" if d["packages_installed"] else "no"] for p in d["packages"]]))
        if d["macro_callers"]:
            w("**Macros and who calls them** — a rewrite inside a macro changes every caller, so "
              "it is one port and many reviews.\n")
            w(table(["macro", "called by"],
                    [[k, ", ".join(v)] for k, v in sorted(d["macro_callers"].items())]))

        w("### 5.2 Snowflake-only constructs\n")
        w("Frequency first, then every hit as `file:line`. `counts: no` marks a low-precision "
          "hint that is printed but does not move a node into a more expensive class.\n")
        w(table(["rule", "construct", "n", "class", "becomes", "counts", "why it matters"],
                [[r["rule"], r["construct"], r["n"], r["class"], r["molinia"],
                  "yes" if r["counts"] else "no", r["why"]] for r in d["constructs"]]))
        if d["constructs"]:
            w("<details>\n<summary>Every hit, file:line</summary>\n")
            for r in d["constructs"]:
                w(f"\n**{r['rule']} · {r['construct']}** ({r['n']})\n")
                shown = r["hits"][: d["detail_limit"]]
                w(table(["file:line", "match"],
                        [[f"{h['file']}:{h['line']}", h["match"]] for h in shown]))
                if len(r["hits"]) > len(shown):
                    w(f"_…and {len(r['hits']) - len(shown)} more (all of them are in the JSON)._\n")
            w("\n</details>\n")

        w("### 5.3 Per-node classification\n")
        w(table(["node", "kind", "materialization", "class", "why"],
                [[n["name"], n["kind"], n.get("materialized", "-"), n["class"], n["reason"]]
                 for n in d["nodes"]]))

        if d["alias_candidates"] or d["regex_literals"]:
            w("### 5.4 Review hints (candidates, not defects)\n")
            if d["alias_candidates"]:
                w("**Rule Y4 — a select-list alias reused in the same select list.** DuckDB "
                  "resolves a lateral column alias and Snowflake does not, so a port can bind to "
                  "something else with no error. Read each one.\n")
                w(table(["file:line", "alias"],
                        [[f"{a['file']}:{a['line']}", a["alias"]]
                         for a in d["alias_candidates"][: d["detail_limit"]]]))
                if len(d["alias_candidates"]) > d["detail_limit"]:
                    w(f"_…and {len(d['alias_candidates']) - d['detail_limit']} more._\n")
            if d["regex_literals"]:
                w("**Rule X1 — regex literals with a backslash.** Snowflake processes backslash "
                  "escapes in string literals and DuckDB does not, so a straight port matches "
                  "nothing and raises no error.\n")
                w(table(["file:line", "literal"],
                        [[f"{r['file']}:{r['line']}", r["literal"]] for r in d["regex_literals"]]))

    # ---- 6. data movement -------------------------------------------------
    dm = data["data_movement"]
    w("## 6. Data-movement plan\n")
    w(f"{dm['how']}\n")
    w(f"> **Assumption.** {dm['size_assumption']}\n")
    if dm["moves"]:
        w(table(["#", "table", "rows", "Snowflake bytes", "est. Parquet", "lands as", "why here"],
                [[m["order_index"], f"{m['schema']}.{m['table']}", num(m["rows"]),
                  human_bytes(m["snowflake_bytes"]), human_bytes(m["est_parquet_bytes"]),
                  f"main.{m['molinia_table']}", m["why_order"]] for m in dm["moves"]]))
        w(f"Source data to move: {human_bytes(dm['total_bytes'])} in Snowflake, about "
          f"{human_bytes(dm['total_est_parquet'])} of Parquet. Answer keys on top of that "
          f"(relations the dbt project builds, unloaded for parity, not migrated): "
          f"{human_bytes(dm.get('answer_key_bytes'))} / about "
          f"{human_bytes(dm.get('answer_key_est_parquet'))}.\n")
        w("### The commands, per table\n")
        w("<details>\n<summary>COPY INTO and ingest for every table</summary>\n")
        for m in dm["moves"]:
            w(f"\n**{m['schema']}.{m['table']} → `main.{m['molinia_table']}`**\n")
            for note in m["notes"]:
                w(f"- {note}")
            w(code(m["copy_into"]))
            w(code(m["ingest_body"], "http"))
        w("\n</details>\n")
    else:
        w("_No base tables in scope._\n")
    w("Then, once per batch:\n")
    w(code(dm["get_and_upload"], "bash"))

    # ---- 7. parity --------------------------------------------------------
    p = data["parity"]
    w("## 7. Parity plan\n")
    w("Parity is what makes a fixed price defensible: every model is compared against the "
      "Snowflake output of the same model, unloaded as an answer key, joined on the model's key. "
      "It returns counts only, never rows.\n")
    if p["entries"]:
        w(table(["model", "Molinia relation", "answer key", "key", "where the key came from"],
                [[e["model"], e["relation"], e["answer_key"],
                  ", ".join(e["key"]) or "**none**", e["key_source"]] for e in p["entries"]]))
        if p["keyless"]:
            w(f"**{len(p['keyless'])} model(s) have no unique test, so parity has no key for them: "
              f"{', '.join(p['keyless'])}.** Someone has to state the grain for each one before "
              "parity means anything. That is analyst time, not agent time, and it is the most "
              "commonly missed line in a migration quote.\n")
        else:
            w("Every model has a key derived from its own `unique` test.\n")
        w("**Generated `parity.yml`** — check every key before you trust a green run.\n")
        w(code(p["parity_yml"], "yaml"))
        w("**Answer-key unloads** (run after the client's dbt build on Snowflake):\n")
        w("<details>\n<summary>COPY INTO per model</summary>\n")
        for e in p["entries"]:
            w(f"\n**{e['model']}**\n")
            w(code(e["unload"]))
        w("\n</details>\n")
        w(f"> The Snowflake relation names above assume dbt's default `generate_schema_name` "
          f"(`{p['target_schema']}` plus the custom schema). A project with its own "
          f"`generate_schema_name` override needs these names checked by hand.\n")
    else:
        w("_No dbt models, so no parity plan. Without a dbt project, parity has to be defined "
          "table by table before the migration can be priced._\n")

    # ---- 8. effort --------------------------------------------------------
    e = data["effort"]
    w("## 8. Effort summary — what you price from\n")
    w(_class_table(e["counts_by_class"]))
    w(f"- dbt models: **{e['dbt_models']}**, dbt tests: **{e['dbt_tests']}**")
    bp = e["build_projection"]
    w(f"- One full `dbt build` against Molinia: about **{bp['requests']} API requests** "
      f"({bp['nodes']} nodes x {bp['requests_per_node']}), roughly "
      f"**{bp['minutes_at_25_per_min']} minutes** paced at 25 requests a minute. "
      f"Basis: {bp['basis']}.")
    w(f"- Rate ceiling: {bp['http_requests_per_min_per_ip']} HTTP requests/min per IP; "
      f"{bp['free_plan_engine_queries_per_min']} engine queries/min on free, "
      f"{bp['paid_plan_engine_queries_per_min']} on paid. One command at a time.\n")
    w("**The calibration anchor**\n")
    w(f"> {e['calibration_anchor']}\n")
    w(f"> {e['calibration_cost']}\n")
    w(f"{e['pricing_note']}\n")
    w("**Positioning, so nobody has to walk it back in the second meeting**\n")
    w(f"> {FACTS['F-POSITION'].text}\n")

    # ---- 9. method --------------------------------------------------------
    w("## 9. Method, and what this report cannot tell you\n")
    for line in data["method"]:
        w(f"- {line}")
    w("")
    w("### Facts cited\n")
    w("Everything this report asserts about Molinia comes from this list, established by code "
      f"reads and live runs as of {AS_OF}. Anything not on it is marked `inference`.\n")
    for f in (data.get("facts_cited") or sorted(FACTS)):
        if f in FACTS:
            w(f"- **{f}** — {FACTS[f].text}")
    w("")
    return "\n".join(out) + "\n"


def render_json(data: dict) -> str:
    return json.dumps(data, indent=2, default=str, ensure_ascii=False) + "\n"
