#!/usr/bin/env python3
"""Free local dry-run of a ported dbt project: render every model with jinja2 and
run it on local DuckDB 1.5.5 against tiny synthetic raw_* tables.

What it proves: the ported SQL parses, binds and executes on the engine Molinia
runs, and what column types it produces. Nothing more.

What it does NOT prove: that the values equal Snowflake's. The fixture rows are
invented, five rows wide. Only `agent/parity.py` against `main.sf_*` proves
equality, and it is the only thing that can.

Why it exists: every dbt build spends about 165 paced API requests and six
minutes. This costs nothing and takes a second, so run it after every layer and
let the build find real problems, not typos.

No network, no keys, no kit data files: it reads only the engagement's dbt
project directory (engagement.yml, `project_dir`). The engagement's forbidden
tables have no fixture on purpose - no model may read them (rule H2).

The fixture column types are the answer to the source-type discovery query in
MIGRATION_RULES.md section 9, measured on this dataset: ids and counters are
DECIMAL(38,0) (Snowflake NUMBER(38,0)), `order_meta` and `payload` are VARCHAR
holding JSON text (rule J0), timestamps are TIMESTAMP, money is DECIMAL(10,2).
Re-run that query on a new dataset and edit FIXTURES below.

Run:  .venv/bin/python agent/localcheck.py [--help]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
if str(KIT / "tools") not in sys.path:
    sys.path.insert(0, str(KIT / "tools"))

from _engagement import ENGAGEMENT  # noqa: E402


def display_path(path: Path) -> str:
    """The project as the reader knows it: relative to the kit root when it is
    inside it (`acme_shop`), absolute when it is somewhere else."""
    try:
        return str(Path(path).resolve().relative_to(KIT))
    except ValueError:
        return str(path)

# The synthetic raw_* tables. Column types are real (see the docstring); the
# values are fake, and chosen to exercise the traps the rulebook warns about:
# untrimmed and mixed-case text, NULL money, mixed-case status values, a
# discount that lands on a half cent, an order with no payment, a web event
# with no `utm` key and one with no `items` key.
FIXTURES = """
create table raw_customers(
    customer_id decimal(38,0), first_name varchar, last_name varchar, email varchar,
    country_code varchar, signup_ts timestamp, marketing_opt_in boolean);
insert into raw_customers values
    (1, ' A ', ' B ', 'X@Y.COM ', 'nl', timestamp '2025-01-02 10:00:00', null),
    (2, 'C', 'D', 'x@y.com', 'DE', timestamp '2025-03-07 14:05:09', true);

create table raw_products(
    product_id decimal(38,0), sku varchar, product_name varchar, category_code varchar,
    unit_price decimal(10,2), is_active boolean);
insert into raw_products values
    (1, 'ab-1', 'P1', 'el', 10.20, true),
    (2, 'cd-2', 'P2', null, null, false);

create table raw_orders(
    order_id decimal(38,0), customer_id decimal(38,0), order_ts timestamp, status varchar,
    channel varchar, order_meta varchar);
insert into raw_orders values
    (1, 1, timestamp '2025-03-07 14:05:09', 'Placed', 'Web',
     '{"coupon":"SAVE10","device":"ios","shipping":{"method":"express","cost":"4.95"},"gift":true}'),
    (2, 2, timestamp '2025-04-01 09:00:00', 'delivered', 'app', '{}');

create table raw_order_items(
    order_item_id decimal(38,0), order_id decimal(38,0), product_id decimal(38,0),
    quantity decimal(38,0), unit_price decimal(10,2), discount_pct decimal(5,2));
insert into raw_order_items values
    (1, 1, 1, 3, 10.20, 12.50),
    (2, 1, 2, 1, 5.00, 0.00),
    (3, 2, 1, 2, 10.20, 0.00);

create table raw_payments(
    payment_id decimal(38,0), order_id decimal(38,0), payment_method varchar, status varchar,
    amount_cents decimal(38,0), paid_ts timestamp);
insert into raw_payments values
    (1, 1, 'iDEAL', 'Success', 3178, timestamp '2025-03-07 15:00:00'),
    (2, 1, 'iDEAL', 'Failed', 3178, null),
    (3, 2, 'card', 'Refunded', 2040, timestamp '2025-04-02 09:00:00');

create table raw_web_events(
    event_id decimal(38,0), customer_id decimal(38,0), event_ts timestamp, event_type varchar,
    payload varchar);
insert into raw_web_events values
    (1, 1, timestamp '2025-03-01 10:00:00', 'Product_View',
     '{"session_id":"s1","utm":{"source":"google","campaign":"c1"},"items":[{"sku":"ab-1","qty":2},{"sku":"zz-9","qty":1}]}'),
    (2, null, timestamp '2025-03-02 10:00:00', 'add_to_cart', '{"session_id":"s2","items":[]}'),
    (3, 2, timestamp '2025-03-03 10:00:00', 'checkout', '{"session_id":"s3"}');
"""

REF_RE = re.compile(r"\bref\s*\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]", re.I)
DEFAULT_VAR = "2026-06-30"


class CheckError(Exception):
    """A problem with the project or the environment, not with a model."""


def project_vars(project: Path) -> dict:
    """The `vars:` block of dbt_project.yml, so `{{ var('as_of_date') }}` renders."""
    cfg = project / "dbt_project.yml"
    if not cfg.exists():
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover - dbt ships PyYAML
        return {}
    loaded = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    got = loaded.get("vars") or {}
    return got if isinstance(got, dict) else {}


def jinja_env(project: Path, variables: dict):
    """A jinja2 environment with dbt's calls stubbed and the project's macros loaded."""
    import jinja2

    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals.update(
        ref=lambda *parts: parts[-1],              # relations are bare names here
        source=lambda _source, table: "raw_" + table,
        var=lambda key, default=DEFAULT_VAR: variables.get(key, default),
        config=lambda **_kw: "",
        is_incremental=lambda: False,
        this="this_relation",
        target={"name": "molinia", "schema": "analytics", "type": "molinia"},
    )
    macros = "".join(p.read_text(encoding="utf-8") for p in sorted((project / "macros").glob("*.sql")))
    return env, macros


def sql_files(project: Path, subdir: str) -> list[Path]:
    return sorted((project / subdir).rglob("*.sql")) if (project / subdir).is_dir() else []


def upstream_closure(names: set[str], paths: list[Path]) -> set[str]:
    """`names` plus every model they reach through `ref()`, transitively.

    `--only` reports on the selected names, but their inputs still have to exist
    in DuckDB, so the closure is what gets built.
    """
    by_name = {p.stem: p for p in paths}
    need: set[str] = set()

    def visit(name: str) -> None:
        if name in need or name not in by_name:   # `in need` also breaks ref cycles
            return
        need.add(name)
        for dep in REF_RE.findall(by_name[name].read_text(encoding="utf-8")):
            visit(dep)

    for name in names:
        visit(name)
    return need


def order_models(paths: list[Path]) -> list[Path]:
    """Depth-first order by `ref()` edges, so a model is built after its inputs."""
    by_name = {p.stem: p for p in paths}
    ordered: list[Path] = []
    seen: set[str] = set()

    def visit(name: str, stack: tuple[str, ...] = ()) -> None:
        if name in seen or name not in by_name:
            return
        if name in stack:  # a ref cycle: leave the order alone and let dbt complain
            return
        for dep in REF_RE.findall(by_name[name].read_text(encoding="utf-8")):
            visit(dep, stack + (name,))
        if name not in seen:
            seen.add(name)
            ordered.append(by_name[name])

    for path in paths:
        visit(path.stem)
    return ordered


def render(env, macros: str, path: Path) -> str:
    return env.from_string(macros + path.read_text(encoding="utf-8")).render()


def run(project: Path, only: set[str] | None, show_types: bool, show_values: bool, out) -> int:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - the kit venv has duckdb
        raise CheckError(f"duckdb is not installed in this interpreter: {exc}") from exc
    if not project.is_dir():
        raise CheckError(f"no such project directory: {project}")
    models = order_models(sql_files(project, "models"))
    if not models:
        raise CheckError(f"no models under {project / 'models'}")
    tests = sql_files(project, "tests")

    needed: set[str] | None = None
    if only:
        known = {p.stem for p in models} | {p.stem for p in tests}
        unknown = sorted(only - known)
        if unknown:   # a typo must never look like a green check
            raise CheckError(f"unknown name(s) for --only: {', '.join(unknown)} "
                             f"(known: {', '.join(sorted(known))})")
        needed = upstream_closure(only, models + tests)

    env, macros = jinja_env(project, project_vars(project))
    con = duckdb.connect()
    con.execute(FIXTURES)
    print(f"localcheck: {display_path(project)}  DuckDB {duckdb.__version__}  "
          f"{len(models)} model(s), synthetic rows, no API calls", file=out)
    if only:
        print(f"--only {' '.join(sorted(only))}  "
              f"(upstream models are built too, so the selection can bind)", file=out)

    failed = 0                  # models and singular tests, for the exit code
    model_failed = 0
    built: list[str] = []
    skipped: list[str] = []
    missing: set[str] = set()   # models that failed or were skipped: don't blame their children
    for path in models:
        name = path.stem
        if needed is not None and name not in needed:
            continue
        reported = only is None or name in only    # upstreams are built, not reported
        upstream = sorted(missing.intersection(REF_RE.findall(path.read_text(encoding="utf-8"))))
        if upstream:
            missing.add(name)
            if reported:
                skipped.append(name)
                print(f"SKIP {name:24} needs {', '.join(upstream)}", file=out)
            continue
        try:
            con.execute(f'create table "{name}" as {render(env, macros, path)}')
            rows = con.execute(f'select count(*) from "{name}"').fetchone()[0]
        except Exception as exc:
            failed += 1
            model_failed += 1
            missing.add(name)
            tail = "" if reported else "   (upstream of --only)"
            print(f"FAIL {name:24} {first_line(exc)}{tail}", file=out)
            continue
        if not reported:
            continue
        built.append(name)
        print(f"OK   {name:24} rows={rows}", file=out)
        if show_types:
            cols = con.execute(f'describe "{name}"').fetchall()
            print("       " + ", ".join(f"{c[0]} {c[1]}" for c in cols), file=out)
        if show_values:
            for row in con.execute(f'select * from "{name}" limit 3').fetchall():
                print(f"       {row}", file=out)

    for path in tests:
        name = path.stem
        if only and name not in only:
            continue
        upstream = sorted(missing.intersection(REF_RE.findall(path.read_text(encoding="utf-8"))))
        if upstream:
            print(f"SKIP {name:24} needs {', '.join(upstream)}", file=out)
            continue
        try:
            failing = con.execute(f"select count(*) from ({render(env, macros, path)})").fetchone()[0]
        except Exception as exc:
            failed += 1
            print(f"FAIL {name:24} {first_line(exc)}", file=out)
            continue
        print(f"OK   {name:24} failing_rows={failing}  (on fixture rows, not on the real data)", file=out)

    reported_models = len(built) + model_failed + len(skipped)
    print(f"\n{len(built)}/{reported_models} model(s) ran; "
          f"{failed} failure(s). Binding and types only - parity proves the values.", file=out)
    return 1 if failed else 0


def first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(
        prog="agent/localcheck.py",
        description="Render and run a ported dbt project on local DuckDB against synthetic "
                    "raw_* tables. Proves the SQL binds and what types it produces; it does "
                    "NOT prove the values match Snowflake (only agent/parity.py does). "
                    "Makes no API calls and reads nothing but the project directory.",
        epilog="Run it after every layer you port, before spending a dbt build.",
    )
    parser.add_argument("--project", default=ENGAGEMENT.project_dir, type=Path,
                        help=f"dbt project directory (default: {ENGAGEMENT.project_dir.name})")
    parser.add_argument("--only", nargs="+", metavar="NAME", default=None,
                        help="report on these models or singular tests only, by name; their "
                             "upstream models are built too, and an unknown name is an error")
    parser.add_argument("--types", action="store_true",
                        help="print each model's output column types")
    parser.add_argument("--values", action="store_true",
                        help="print up to 3 fixture rows per model (synthetic values, never real data)")
    parser.add_argument("--list", action="store_true",
                        help="list the models in dependency order and the fixture tables, then exit")
    args = parser.parse_args(argv)

    try:
        if args.list:
            models = order_models(sql_files(args.project, "models"))
            print("models in dependency order:", file=out)
            for i, path in enumerate(models, 1):
                print(f"  {i:2}. {path.stem:24} {path.relative_to(args.project)}", file=out)
            tests = sql_files(args.project, "tests")
            print("singular tests:" if tests else "singular tests: none", file=out)
            for path in tests:
                print(f"      {path.stem:24} {path.relative_to(args.project)}", file=out)
            print("fixture tables: " + ", ".join(sorted(re.findall(r"create table (\w+)", FIXTURES))),
                  file=out)
            forbidden = ", ".join(t.split(".", 1)[-1] for t in ENGAGEMENT.forbidden_tables)
            if forbidden:
                print(f"  (no {forbidden} fixture: no model may read it, rule H2)", file=out)
            return 0
        return run(args.project, set(args.only) if args.only else None,
                   args.types, args.values, out)
    except CheckError as exc:
        print(f"localcheck: {exc}", file=out)
        return 2


if __name__ == "__main__":
    sys.exit(main())
