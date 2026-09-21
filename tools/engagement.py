#!/usr/bin/env python3
"""Inspect the engagement, and check the kit actually agrees with it.

  tools/engagement.py show [--json]
      Print the resolved engagement and where each value came from.
  tools/engagement.py check [--quiet]
      Four checks, exit 1 on any finding:
        1. the dbt project exists and is a dbt project;
        2. agent/parity.yml agrees with the engagement (relations live in the
           configured dbt schemas, answer keys are <source>.<prefix><model>);
        3. no model, test or source in the project reads a forbidden table
           (rule H2, checked mechanically instead of trusted);
        4. prose still naming a value the engagement has moved away from.

Check 4 is the honest one. engagement.yml re-points every tool, but CLAUDE.md
and agent/MIGRATION_RULES.md name schemas, tables and the project directory in
sentences, and no config can rewrite a sentence. This prints the work list.

Local only: reads files, makes no API calls.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _engagement  # noqa: E402
from _engagement import DEFAULTS, Engagement, EngagementError  # noqa: E402

# Prose that names engagement values. Everything else is generated or data.
DOC_GLOBS = ("*.md", "Makefile", "agent/*.md", "partner/*.md", "setup/**/*.sql")
SKIP_PARTS = {".venv", "exports", "target", "__pycache__", ".git", ".secrets", "scratch"}

# Prose splits three ways, and only the first two block a re-point.
#   agent    the agent reads these; a stale name here sends it at the wrong table
#   partner  a delivery lead reads these to run and price a migration
#   demo     describes THIS demo (its run-sheet, its Snowflake setup, its status
#            log). A client engagement does not carry them, so a stale name is
#            expected, reported as a count, and does not fail the check.
AGENT_DOCS = ("CLAUDE.md", "agent/MIGRATION_RULES.md", "agent/PROMPT.md")
PARTNER_DOCS = ("partner/PARTNER-PLAYBOOK.md", "partner/MIGRATION-STEPS.md")


def _tier(rel: str) -> str:
    if rel in AGENT_DOCS:
        return "agent"
    return "partner" if rel in PARTNER_DOCS else "demo"


def _rel(path: Path, root: Path) -> str:
    """`acme_shop/models/x.sql` inside the kit, an absolute path outside it.
    A client's dbt project often lives outside the kit, so this cannot assume
    the path is relative to the kit root."""
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(path)


def _iter_docs(kit: Path) -> Iterable[Path]:
    for pattern in DOC_GLOBS:
        for p in sorted(kit.glob(pattern)):
            if p.is_file() and not (set(p.relative_to(kit).parts) & SKIP_PARTS):
                yield p


def _slots(eng: Engagement) -> List[Tuple[str, str, str]]:
    """(label, value the built-in demo defaults use, value in force now)."""
    d, m = DEFAULTS, DEFAULTS["molinia"]
    return [
        ("project_dir", d["project_dir"], eng.project_dir.name),
        ("export_prefix", d["export_prefix"], eng.export_prefix),
        ("snowflake.stage", d["snowflake"]["stage"], eng.snowflake_stage),
        ("molinia.source_schema", m["source_schema"], eng.source_schema),
        ("molinia.raw_prefix", m["raw_prefix"], eng.raw_prefix),
        ("molinia.answer_key_prefix", m["answer_key_prefix"], eng.answer_key_prefix),
        ("service_accounts.build", d["service_accounts"]["build"], eng.service_account_build),
        ("service_accounts.readonly", d["service_accounts"]["readonly"], eng.service_account_readonly),
        *[(f"molinia.dbt_schemas[{i}]", old, new)
          for i, (old, new) in enumerate(zip(m["dbt_schemas"], eng.dbt_schemas))],
        *[(f"forbidden_tables[{i}]", old.split(".", 1)[-1], new.split(".", 1)[-1])
          for i, (old, new) in enumerate(zip(d["forbidden_tables"], eng.forbidden_tables))],
    ]


# --------------------------------------------------------------------------- checks

def check_project(eng: Engagement) -> List[str]:
    if not eng.project_dir.is_dir():
        return [f"project_dir: {eng.project_dir} does not exist"]
    if not (eng.project_dir / "dbt_project.yml").is_file():
        return [f"project_dir: {eng.project_dir} has no dbt_project.yml"]
    return []


def check_parity_config(eng: Engagement, parity_yml: Path) -> List[str]:
    if not parity_yml.is_file():
        return [f"{parity_yml.name}: not found "
                "(partner/assess.py generates one from the client's dbt project)"]
    try:
        import yaml
        doc = yaml.safe_load(parity_yml.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - report, don't crash the check
        return [f"{parity_yml.name}: unreadable: {' '.join(str(exc).split())[:120]}"]

    out: List[str] = []
    models = doc.get("models") or []
    if not models:
        out.append(f"{parity_yml.name}: lists no models")
    for m in models:
        name = str(m.get("name") or "?")
        relation = str(m.get("relation") or "")
        schema = relation.split(".", 1)[0].lower() if "." in relation else ""
        if schema and schema not in eng.dbt_schemas:
            out.append(f"{parity_yml.name}: {name}.relation is in schema {schema!r}, "
                       f"which is not one of {eng.schema_list(', ')}")
        expected = eng.answer_key(name)
        actual = str(m.get("answer_key") or "").lower()
        if actual and actual != expected:
            out.append(f"{parity_yml.name}: {name}.answer_key is {actual!r}, "
                       f"the engagement says {expected!r}")
        if not m.get("key"):
            out.append(f"{parity_yml.name}: {name} has no key columns "
                       "(assess.py writes CHOOSE_A_KEY when dbt declares no unique test)")
    return out


def _project_files(eng: Engagement, suffixes: Tuple[str, ...]) -> Iterable[Path]:
    for path in sorted(eng.project_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        if "target" in path.relative_to(eng.project_dir).parts:
            continue
        yield path


def _forbidden_sources(eng: Engagement) -> List[dict]:
    """Source entries that resolve to a forbidden table.

    A model does not name the table: it writes `source('raw', 'customer_contacts')`
    and dbt resolves that through `identifier:`. Matching the raw table name in
    SQL would therefore miss every real read, so resolve the identifiers first.
    """
    import yaml

    out: List[dict] = []
    for path in _project_files(eng, (".yml", ".yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - a broken yml is dbt's error to report
            continue
        if not isinstance(doc, dict):
            continue
        for src in doc.get("sources") or []:
            if not isinstance(src, dict):
                continue
            schema = str(src.get("schema") or eng.source_schema).lower()
            for tbl in src.get("tables") or []:
                if not isinstance(tbl, dict):
                    continue
                name = str(tbl.get("name") or "")
                identifier = str(tbl.get("identifier") or name)
                if not (eng.is_forbidden(f"{schema}.{identifier}") or eng.is_forbidden(identifier)):
                    continue
                columns = tbl.get("columns") or []
                tests = bool(tbl.get("data_tests") or tbl.get("tests")) or any(
                    isinstance(c, dict) and (c.get("data_tests") or c.get("tests"))
                    for c in columns)
                out.append({"source": str(src.get("name") or ""), "table": name,
                            "identifier": identifier, "tests": tests,
                            "path": _rel(path, eng.kit_root)})
    return out


def check_forbidden_tables(eng: Engagement) -> List[str]:
    """Rule H2, checked rather than trusted.

    A declaration in sources.yml is fine and is how a client documents "this is
    PII and we do not model it". A *read* is not, and neither is a test on the
    declaration: a service account can never be unmasked, so dbt would compare
    masked values and the model would persist '****'.
    """
    if not eng.project_dir.is_dir() or not eng.forbidden_tables:
        return []

    out: List[str] = []
    declared = _forbidden_sources(eng)
    for d in declared:
        if d["tests"]:
            out.append(f"{d['path']}: source {d['source']}.{d['table']} is forbidden "
                       f"({d['identifier']}) and carries tests; dbt would query it (rule H2)")

    # Every way SQL can reach the table: the resolved identifier written out, or
    # a source() call that dbt resolves to it.
    needles = {re.compile(rf"\b{re.escape(t.split('.', 1)[-1])}\b", re.I): "names"
               for t in eng.forbidden_tables}
    for d in declared:
        rx = re.compile(rf"source\s*\(\s*['\"]{re.escape(d['source'])}['\"]\s*,\s*"
                        rf"['\"]{re.escape(d['table'])}['\"]\s*\)", re.I)
        needles[rx] = f"reads source {d['source']}.{d['table']}"

    for path in _project_files(eng, (".sql",)):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("--", 1)[0]
            for rx, how in needles.items():
                if rx.search(code):
                    out.append(f"{_rel(path, eng.kit_root)}:{lineno}: {how} a "
                               "forbidden table (rule H2)")
    return out


def check_docs(eng: Engagement) -> List[Tuple[str, str]]:
    """[(tier, finding)] for prose still naming a value the engagement moved."""
    moved = [(label, old, new) for label, old, new in _slots(eng) if old != new]
    if not moved:
        return []
    out: List[Tuple[str, str]] = []
    for path in _iter_docs(eng.kit_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel(path, eng.kit_root)
        tier = _tier(rel)
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, old, new in moved:
                if re.search(rf"\b{re.escape(old)}\b", line):
                    out.append((tier, f"{rel}:{lineno}: says {old!r}, "
                                      f"engagement says {new!r} ({label})"))
    return out


# --------------------------------------------------------------------------- commands

def cmd_show(args, eng: Engagement) -> int:
    if args.json:
        print(json.dumps({"source": str(eng.path) if eng.path else "built-in defaults",
                          **eng.as_dict()}, indent=2))
        return 0
    source = str(eng.path) if eng.path else "built-in defaults (engagement.yml not found)"
    rows = [
        ("engagement", eng.name),
        ("source", source),
        ("dbt project", str(eng.project_dir)),
        ("exports", str(eng.export_root)),
        ("Snowflake stage", eng.snowflake_stage),
        ("source schema", eng.source_schema),
        ("dbt schemas", eng.schema_list(", ")),
        ("ingested source", eng.raw_table("<table>")),
        ("answer key", eng.answer_key("<model>")),
        ("storage", f"data source {eng.data_source_id}, location {eng.location_id}"),
        ("forbidden", ", ".join(eng.forbidden_tables) or "(none)"),
        ("build SA", f"{eng.service_account_build} (MOLINIA_API_KEY)"),
        ("read-only SA", f"{eng.service_account_readonly} (MOLINIA_READONLY_KEY)"),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.rjust(width)}  {value}")
    return 0


def cmd_check(args, eng: Engagement) -> int:
    docs = check_docs(eng)
    by_tier = {t: [f for tier, f in docs if tier == t] for t in ("agent", "partner", "demo")}
    groups = [
        ("dbt project", check_project(eng), True),
        ("parity config", check_parity_config(eng, eng.kit_root / "agent" / "parity.yml"), True),
        ("forbidden tables (H2)", check_forbidden_tables(eng), True),
        ("prose the agent reads", by_tier["agent"], True),
        ("partner docs", by_tier["partner"], True),
    ]
    total = sum(len(f) for _, f, blocking in groups if blocking)
    for label, findings, _ in groups:
        if findings:
            print(f"\n{label}: {len(findings)} finding(s)")
            for f in findings:
                print(f"  {f}")
        elif not args.quiet:
            print(f"ok  {label}")

    narrative = by_tier["demo"]
    if narrative:
        files = sorted({f.split(":", 1)[0] for f in narrative})
        print(f"\nnote: {len(narrative)} line(s) in {len(files)} demo document(s) still name the "
              "old values.\n      These describe this demo rather than the engagement, so they do "
              "not fail this check:\n      " + ", ".join(files))
    if total:
        print(f"\n{total} blocking finding(s). engagement.yml re-points every tool; the lines "
              "above are prose and data that a config cannot rewrite.")
    return 1 if total else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="tools/engagement.py",
        description="Inspect engagement.yml and check the kit agrees with it.")
    p.add_argument("--file", type=Path, default=None,
                   help="engagement file to use (default: engagement.yml, "
                        "or $MOLINIA_KIT_ENGAGEMENT)")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{show,check}")
    s = sub.add_parser("show", help="print the resolved engagement")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_show)
    c = sub.add_parser("check", help="check the kit against the engagement")
    c.add_argument("--quiet", action="store_true", help="print findings only")
    c.set_defaults(func=cmd_check)
    args = p.parse_args(argv)

    try:
        eng = _engagement.load(args.file) if args.file else _engagement.ENGAGEMENT
    except EngagementError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return args.func(args, eng)


if __name__ == "__main__":
    sys.exit(main())
