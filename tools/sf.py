#!/usr/bin/env python3
"""Snowflake side of the Molinia partner demo kit.

  tools/sf.py run <file.sql> [--dry-run] [--keep-going]
      Execute a multi-statement SQL file on Snowflake, one statement at a time,
      printing a one-line result per statement.
  tools/sf.py unload [--no-copy]
      REMOVE @<stage>/raw/ and /expected/, run setup/snowflake/02_unload.sql
      (raw tables + every model to the stage as Parquet), GET both prefixes
      into a staging dir, normalise file names to <name>.parquet, then replace
      exports/<export_prefix>/{raw,expected}/ (a failure leaves the previous
      exports untouched), warn about files the SQL file does not write, and
      print a size / row-count table read locally.
  tools/sf.py dbt -- <dbt args>
      Run .venv/bin/dbt in the engagement's dbt project with --profiles-dir .
      --target snowflake (unless you pass your own --target / --profiles-dir)
      and the env loaded.

The stage, the export prefix and the dbt project come from engagement.yml
(tools/_engagement.py).

Connection settings come from .secrets/snowflake.env (SNOWFLAKE_ACCOUNT, _USER,
_ROLE, _WAREHOUSE, _DATABASE, _PRIVATE_KEY_PATH); key-pair auth with the
unencrypted PKCS#8 key at SNOWFLAKE_PRIVATE_KEY_PATH. Never prints secrets.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _env  # noqa: E402
from _engagement import ENGAGEMENT  # noqa: E402
from _sql import one_line, split_statements, strip_leading_comments  # noqa: E402,F401  (split_statements re-exported)

KIT = _env.KIT_ROOT
STAGE = ENGAGEMENT.snowflake_stage
EXPORT_KINDS = ("raw", "expected")
UNLOAD_SQL = KIT / "setup" / "snowflake" / "02_unload.sql"
EXPORT_ROOT = _env.EXPORT_ROOT
DBT_PROJECT = ENGAGEMENT.project_dir
DBT_BIN = KIT / ".venv" / "bin" / "dbt"
PARQUET_BUDGET_BYTES = 10 * 1024 * 1024

SF_VARS = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE", "SNOWFLAKE_WAREHOUSE",
           "SNOWFLAKE_DATABASE", "SNOWFLAKE_PRIVATE_KEY_PATH")


# --------------------------------------------------------------------------- connection

def _load_private_key_der(path: str) -> bytes:
    from cryptography.hazmat.primitives import serialization

    p = Path(os.path.expanduser(path))
    if not p.is_file():
        raise _env.EnvError(f"SNOWFLAKE_PRIVATE_KEY_PATH points to a file that does not exist ({p})")
    try:
        key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    except TypeError:
        raise _env.EnvError("the private key is encrypted; the kit expects an unencrypted PKCS#8 key") from None
    except ValueError:
        raise _env.EnvError("could not parse the private key as PEM PKCS#8") from None
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def connect():
    _env.load_env()
    v = _env.require(*SF_VARS)
    import snowflake.connector

    return snowflake.connector.connect(
        account=v["SNOWFLAKE_ACCOUNT"],
        user=v["SNOWFLAKE_USER"],
        role=v["SNOWFLAKE_ROLE"],
        warehouse=v["SNOWFLAKE_WAREHOUSE"],
        database=v["SNOWFLAKE_DATABASE"],
        authenticator="SNOWFLAKE_JWT",
        private_key=_load_private_key_der(v["SNOWFLAKE_PRIVATE_KEY_PATH"]),
        session_parameters={"QUERY_TAG": "molinia_demo_kit"},
        login_timeout=60,
        network_timeout=600,
        client_session_keep_alive=False,
    )


# --------------------------------------------------------------------------- run

def _summarise_result(cur) -> str:
    desc = cur.description or []
    if not desc:
        rc = cur.rowcount
        return f"{rc} rows affected" if rc is not None and rc >= 0 else "ok"
    first = cur.fetchmany(2)
    total = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(first)
    if total == 1 and first:
        row = first[0]
        if len(row) == 1:
            return str(row[0])
        names = [d[0] for d in desc]
        return ", ".join(f"{n}={v}" for n, v in zip(names, row))
    return f"{total} rows"


def run_statements(conn, statements: Sequence[str], label: str = "", keep_going: bool = False,
                   out=None) -> int:
    """Execute statements in order on one session. Returns the number of failures."""
    out = out or sys.stdout
    failures = 0
    n = len(statements)
    width = len(str(n))
    cur = conn.cursor()
    try:
        for i, stmt in enumerate(statements, 1):
            t0 = time.monotonic()
            try:
                cur.execute(stmt)
                summary = _summarise_result(cur)
                status = "ok  "
            except Exception as exc:  # snowflake.connector.errors.Error and friends
                failures += 1
                msg = str(getattr(exc, "msg", None) or exc).strip().splitlines()
                summary = msg[0] if msg else type(exc).__name__
                status = "FAIL"
            dt = time.monotonic() - t0
            summary = " ".join(str(summary).split())
            if len(summary) > 120:
                summary = summary[:117] + "..."
            print(f"{label}[{i:>{width}}/{n}] {status} {dt:6.1f}s  {one_line(stmt, 70)}  -> {summary}",
                  file=out, flush=True)
            if status == "FAIL" and not keep_going:
                print(f"stopped at statement {i}/{n} (use --keep-going to continue past failures)",
                      file=out)
                break
    finally:
        cur.close()
    return failures


def cmd_run(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: {path} not found", file=sys.stderr)
        return 2
    stmts = split_statements(path.read_text(encoding="utf-8"), dialect="snowflake")
    if not stmts:
        print(f"error: no statements in {path}", file=sys.stderr)
        return 2
    if args.dry_run:
        for i, s in enumerate(stmts, 1):
            print(f"[{i}/{len(stmts)}] {one_line(s, 100)}")
        print(f"{len(stmts)} statements (dry run, nothing executed)")
        return 0
    print(f"{path.name}: {len(stmts)} statements")
    conn = connect()
    try:
        failures = run_statements(conn, stmts, keep_going=args.keep_going)
    finally:
        conn.close()
    return 1 if failures else 0


# --------------------------------------------------------------------------- unload

_SNOWFLAKE_PART = re.compile(r"_\d+_\d+_\d+$")
_STRIP_EXT = (".parquet", ".snappy", ".gz", ".zst", ".lzo", ".brotli")


def normalise_export_name(filename: str, kind: str) -> str:
    """Canonical local/bucket name for an unloaded file.

    `CUSTOMERS.parquet`, `customers_0_0_0.snappy.parquet`,
    `customers.parquet_0_0_0.snappy.parquet` (a multi-file unload to a path
    that already ends in .parquet) and `customers` (SINGLE = TRUE without an
    extension) all become `customers.parquet`. Extension and `_N_N_N` part
    suffixes are stripped together until nothing changes. A redundant `raw_`
    prefix in raw/ or `sf_` prefix in expected/ is dropped, because the ingest
    step adds it (raw/orders.parquet -> main.raw_orders).
    """
    name = filename.lower()
    changed = True
    while changed:
        changed = False
        for ext in _STRIP_EXT:
            if name.endswith(ext) and len(name) > len(ext):
                name = name[: -len(ext)]
                changed = True
        stripped = _SNOWFLAKE_PART.sub("", name)
        if stripped and stripped != name:
            name = stripped
            changed = True
    prefix = {"raw": "raw_", "expected": "sf_"}.get(kind)
    if prefix and name.startswith(prefix) and len(name) > len(prefix):
        name = name[len(prefix):]
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"cannot derive a table name from exported file {filename!r}")
    return f"{name}.parquet"


def normalise_dir(d: Path, kind: str) -> List[Tuple[str, str]]:
    """Rename every file in d to its canonical name. Returns [(old, new)]."""
    renames: List[Tuple[str, str]] = []
    targets = {}
    for f in sorted(p for p in d.iterdir() if p.is_file() and not p.name.startswith(".")):
        new = normalise_export_name(f.name, kind)
        if new in targets:
            raise ValueError(f"{f.name} and {targets[new]} both normalise to {new}")
        targets[new] = f.name
    for new, old in targets.items():
        if old != new:
            (d / old).rename(d / new)
            renames.append((old, new))
    return renames


def export_table(root: Path = EXPORT_ROOT) -> Tuple[List[List[Any]], int]:
    import pyarrow.parquet as pq

    rows: List[List[Any]] = []
    total = 0
    for kind in ("raw", "expected"):
        d = root / kind
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.parquet")):
            size = f.stat().st_size
            total += size
            md = pq.read_metadata(f)
            rows.append([f"{kind}/{f.name}", f"{size / 1024:,.1f}", f"{md.num_rows:,}", md.num_columns])
    return rows, total


def print_export_table(root: Path = EXPORT_ROOT) -> int:
    from _molinia_client import format_table

    rows, total = export_table(root)
    if not rows:
        print(f"no Parquet files under {root}")
        return 0
    print(format_table(["file", "KB", "rows", "cols"], rows, align_right=[False, True, True, True]))
    print(f"{len(rows)} files, {total / 1024 / 1024:.2f} MB total"
          + ("" if total <= PARQUET_BUDGET_BYTES else "  WARNING: over the 10 MB budget in DESIGN.md"))
    return total


def _file_url(d: Path) -> str:
    return "file://" + str(d.resolve()).replace("\\", "/") + "/"


def clear_stage_statements() -> List[str]:
    """REMOVE every file under the stage's raw/ and expected/ prefixes, so a
    leftover from an earlier unload (an old model, a multi-file part) can never
    be downloaded next to the fresh files. REMOVE on an empty prefix is a no-op."""
    return [f"REMOVE @{STAGE}/{kind}/" for kind in EXPORT_KINDS]


def with_stage_clear(stmts: Sequence[str]) -> List[str]:
    """02_unload.sql with the stage REMOVEs inserted after its leading USE
    statements, so they run under the same role/database as the COPYs."""
    i = 0
    while i < len(stmts) and re.match(r"(?i)use\s", strip_leading_comments(stmts[i]).lstrip()):
        i += 1
    return list(stmts[:i]) + clear_stage_statements() + list(stmts[i:])


_COPY_TARGET = re.compile(r"COPY\s+INTO\s+@" + re.escape(STAGE) + r"/(raw|expected)/([^\s'\"()]+)",
                          re.IGNORECASE)


def planned_exports(sql_text: str) -> dict:
    """{kind: {canonical file name}} that 02_unload.sql writes."""
    out: dict = {k: set() for k in EXPORT_KINDS}
    for kind, target in _COPY_TARGET.findall(sql_text):
        out[kind.lower()].add(normalise_export_name(target, kind.lower()))
    return out


def fetch_exports(cur, root: Path = EXPORT_ROOT, out=None) -> dict:
    """GET raw/ and expected/ from the stage into a staging dir under `root`,
    normalise the names, and only then replace root/{raw,expected}. If the GET
    or the normalisation fails, the previous exports stay untouched and the
    staging dir is removed. Returns {kind: [canonical file names]}.

    The staging dir starts with a dot: minio_upload.py skips dot paths and
    molinia.py ingest only reads raw/*.parquet and expected/*.parquet."""
    out = out or sys.stdout
    incoming = root / ".incoming"
    shutil.rmtree(incoming, ignore_errors=True)
    got: dict = {}
    try:
        for kind in EXPORT_KINDS:
            d = incoming / kind
            d.mkdir(parents=True)
            cur.execute(f"GET @{STAGE}/{kind}/ '{_file_url(d)}'")
            n = len(cur.fetchall())
            print(f"GET @{STAGE}/{kind}/ -> {n} files", file=out)
            for old, new in normalise_dir(d, kind):
                print(f"  renamed {kind}/{old} -> {kind}/{new}", file=out)
            got[kind] = sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
        for kind in EXPORT_KINDS:  # regenerable output: replace wholesale so stale files never ship
            dest = root / kind
            if dest.exists():
                shutil.rmtree(dest)
            (incoming / kind).rename(dest)
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    return got


def check_exports(got: dict, planned: dict, out=None) -> int:
    """Warn about files that 02_unload.sql does not write (stale stage
    leftovers, e.g. after --no-copy) and files it writes that did not arrive.
    Returns the number of warnings."""
    out = out or sys.stderr
    warnings = 0
    for kind in EXPORT_KINDS:
        have, want = set(got.get(kind, [])), planned.get(kind, set())
        for name in sorted(have - want):
            print(f"WARNING: {kind}/{name} is not written by {UNLOAD_SQL.name} (stale stage file?); "
                  f"it would be uploaded and ingested", file=out)
            warnings += 1
        for name in sorted(want - have):
            print(f"WARNING: {kind}/{name} is written by {UNLOAD_SQL.name} but was not on the stage", file=out)
            warnings += 1
    return warnings


def cmd_unload(args) -> int:
    if not args.no_copy and not UNLOAD_SQL.is_file():
        print(f"error: {UNLOAD_SQL} not found", file=sys.stderr)
        return 2
    sql_text = UNLOAD_SQL.read_text(encoding="utf-8") if UNLOAD_SQL.is_file() else ""
    conn = connect()
    try:
        if not args.no_copy:
            stmts = with_stage_clear(split_statements(sql_text, dialect="snowflake"))
            print(f"{UNLOAD_SQL.relative_to(KIT)} (+ clear @{STAGE}/raw/ and /expected/ first): "
                  f"{len(stmts)} statements")
            if run_statements(conn, stmts):
                return 1
        cur = conn.cursor()
        try:
            got = fetch_exports(cur, EXPORT_ROOT)
        finally:
            cur.close()
    finally:
        conn.close()
    if sql_text:
        check_exports(got, planned_exports(sql_text))
    print()
    print_export_table(EXPORT_ROOT)
    return 0


# --------------------------------------------------------------------------- dbt

def build_dbt_argv(args: Sequence[str]) -> List[str]:
    a = list(args)
    if a and a[0] == "--":
        a = a[1:]
    argv = [str(DBT_BIN)] + a
    if not a or a[0].startswith("-"):
        return argv  # top-level flags only (e.g. --version): nothing to add
    flags = set()
    for x in a:
        flags.add(x.split("=", 1)[0])
    if "--profiles-dir" not in flags:
        argv += ["--profiles-dir", "."]
    if "--target" not in flags and "-t" not in flags:
        argv += ["--target", "snowflake"]
    return argv


def cmd_dbt(args) -> int:
    if not args.dbt_args or args.dbt_args == ["--"]:
        print("usage: tools/sf.py dbt -- <dbt args>   e.g. tools/sf.py dbt -- build", file=sys.stderr)
        return 2
    _env.load_env()
    _env.require(*SF_VARS)
    if not DBT_PROJECT.is_dir():
        print(f"error: {DBT_PROJECT} not found", file=sys.stderr)
        return 2
    argv = build_dbt_argv(args.dbt_args)
    env = dict(os.environ)
    env.setdefault("DO_NOT_TRACK", "1")
    print("+ " + " ".join(["dbt"] + argv[1:]) + f"   (in {DBT_PROJECT.relative_to(KIT)}/)", flush=True)
    return subprocess.call(argv, cwd=str(DBT_PROJECT), env=env)


# --------------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools/sf.py",
        description="Snowflake side of the Molinia demo kit (loads .secrets/*.env; never prints secrets).",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="{run,unload,dbt}")

    r = sub.add_parser("run", help="execute a multi-statement SQL file on Snowflake",
                       description="Execute a multi-statement SQL file, one statement at a time, "
                                   "printing a one-line result per statement. Stops at the first failure.")
    r.add_argument("file", help="path to the .sql file")
    r.add_argument("--dry-run", action="store_true", help="only split and list the statements (no connection)")
    r.add_argument("--keep-going", action="store_true", help="continue after a failing statement")
    r.set_defaults(func=cmd_run)

    u = sub.add_parser("unload", help="unload raw tables + models to Parquet and GET them into exports/",
                       description=f"Clear @{STAGE}/raw/ and /expected/, run setup/snowflake/02_unload.sql, "
                                   f"then GET both into exports/{ENGAGEMENT.export_prefix}/{{raw,expected}}/ (replaced "
                                   "only after every file downloaded and normalised), normalise names to "
                                   "<name>.parquet and print sizes and row counts.")
    u.add_argument("--no-copy", action="store_true",
                   help="skip the stage REMOVE and 02_unload.sql; only GET + normalise what is on the stage")
    u.set_defaults(func=cmd_unload)

    d = sub.add_parser("dbt", help=f"run dbt in {DBT_PROJECT.name}/ against the snowflake target",
                       description=f"Run .venv/bin/dbt in {DBT_PROJECT.name}/ with --profiles-dir . and --target snowflake "
                                   "appended unless given. Example: tools/sf.py dbt -- build --select staging")
    d.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments after -- are passed to dbt")
    d.set_defaults(func=cmd_dbt)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _env.EnvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:  # e.g. an exported file name normalise_export_name cannot map
        print(f"error: {exc}", file=sys.stderr)
        if getattr(args, "cmd", None) == "unload":
            print(f"(the previous files under {EXPORT_ROOT} were left untouched)", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
