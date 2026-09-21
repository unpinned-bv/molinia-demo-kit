#!/usr/bin/env python3
"""Prove that every ported dbt model on Molinia equals Snowflake's output.

For each model in agent/parity.yml the Molinia relation (<schema>.<model>) is
compared with its Snowflake answer key (main.sf_<model>) by a FULL OUTER JOIN on
the model's key columns. Only COUNTS come back -- never rows -- so whoever runs
this never receives row data:

  rows_sf, rows_molinia          row counts of both sides
  only_in_sf, only_in_molinia    rows whose key has no partner on the other side
  dup_keys_sf, dup_keys_molinia  surplus rows on a key that is not unique
  per non-key column             rows (among key-matched rows) whose values differ

Comparison rules (DESIGN.md "Parity"):
  * column names match case-insensitively (Snowflake Parquet is upper-case);
  * numeric vs numeric (any integer/DECIMAL/DOUBLE mix) passes when both are
    equal or both are finite and |a - b| <= tolerance * greatest(1, |a|, |b|)
    (tolerance 1e-9): float noise passes, a one-cent difference fails, and
    inf / NaN (DuckDB's 1/0 and 0/0) never pass against a number (DIV0 gives 0);
  * DATE vs TIMESTAMP compares as TIMESTAMP (TIMESTAMPTZ vs naive: as a UTC
    TIMESTAMP, flagged);
  * same types compare with IS DISTINCT FROM; any other type mismatch compares
    both sides CAST AS VARCHAR and is flagged (flags are warnings: the model
    still matches when no row differs);
  * columns that exist on one side only are reported and fail the model.

API use: ONE information_schema query discovers the columns of all relations,
then ONE comparison query per model, paced under Molinia's rate limits (shared
client in tools/_molinia_client.py). A relation that does not exist costs no
query. --local-duckdb <file> runs the identical SQL against a local DuckDB file.

Exit code: 0 only when every selected model matches; 1 otherwise; 2 on usage or
configuration errors.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "tools"))

import _env  # noqa: E402
from _molinia_client import MoliniaClient, MoliniaError, format_table, to_int  # noqa: E402
from molinia import choose_catalogs, qident, qlit  # noqa: E402

DEFAULT_CONFIG = KIT / "agent" / "parity.yml"
DEFAULT_TOLERANCE = 1e-9
PRESENT = "__parity_present"


# --------------------------------------------------------------------------- config

@dataclass
class ModelSpec:
    name: str
    relation: Tuple[str, str]      # (schema, table) on Molinia
    answer_key: Tuple[str, str]    # (schema, table), the Snowflake answer key
    key: List[str]


class ConfigError(ValueError):
    pass


def _split_rel(value: str, what: str) -> Tuple[str, str]:
    parts = [p.strip().strip('"') for p in str(value).split(".")]
    if len(parts) != 2 or not all(parts):
        raise ConfigError(f"{what} must be <schema>.<table>, got {value!r}")
    return parts[0], parts[1]


def load_config(path: Path) -> Tuple[List[ModelSpec], float]:
    import yaml

    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"config not found: {path}") from None
    tol = float(doc.get("tolerance", DEFAULT_TOLERANCE))
    specs: List[ModelSpec] = []
    seen = set()
    for i, m in enumerate(doc.get("models") or []):
        name = str(m.get("name") or "").strip()
        if not name:
            raise ConfigError(f"models[{i}] has no name")
        if name in seen:
            raise ConfigError(f"model {name!r} listed twice")
        seen.add(name)
        rel = _split_rel(m.get("relation") or "", f"{name}.relation")
        ak = _split_rel(m.get("answer_key") or f"main.sf_{name}", f"{name}.answer_key")
        key = m.get("key")
        if isinstance(key, str):
            key = [key]
        if not key or not all(isinstance(k, str) and k.strip() for k in key):
            raise ConfigError(f"{name}.key must be a non-empty list of column names")
        specs.append(ModelSpec(name, rel, ak, [k.strip() for k in key]))
    if not specs:
        raise ConfigError(f"no models in {path}")
    return specs, tol


# --------------------------------------------------------------------------- executors

class MoliniaExecutor:
    kind = "molinia"

    def __init__(self, client: MoliniaClient) -> None:
        self.client = client
        self.calls = 0

    def describe(self) -> str:
        return f"Molinia {self.client.api_url} org {self.client.org_id}"

    def query(self, sql: str) -> Tuple[List[str], List[List[Any]]]:
        self.calls += 1
        res = self.client.execute(sql, idempotent=True)
        return list(res.get("columns") or []), [list(r) for r in (res.get("rows") or [])]


class DuckDBExecutor:
    kind = "duckdb"

    def __init__(self, path: str) -> None:
        import duckdb

        if not Path(path).is_file():
            raise ConfigError(f"local DuckDB file not found: {path}")
        self.path = path
        self.con = duckdb.connect(path, read_only=True)
        self.calls = 0

    def describe(self) -> str:
        return f"local DuckDB {self.path}"

    def query(self, sql: str) -> Tuple[List[str], List[List[Any]]]:
        self.calls += 1
        try:
            cur = self.con.execute(sql)
        except Exception as exc:  # duckdb.Error
            raise MoliniaError(f"{type(exc).__name__}: {exc}") from None
        cols = [d[0] for d in cur.description or []]
        return cols, [list(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- types

_NUMERIC = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|INT|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|UHUGEINT|"
    r"INT1|INT2|INT4|INT8|SIGNED|FLOAT|FLOAT4|FLOAT8|REAL|DOUBLE|DOUBLE PRECISION|"
    r"DECIMAL|NUMERIC)(\s*\(\s*\d+\s*(,\s*\d+\s*)?\))?$")
_TIMESTAMP = {"TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS", "TIMESTAMP_US",
              "DATETIME", "TIMESTAMP WITHOUT TIME ZONE"}
_TIMESTAMPTZ = {"TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"}
_VARCHAR = re.compile(r"^(VARCHAR|TEXT|STRING|CHAR|BPCHAR|CHARACTER VARYING)(\s*\(\s*\d+\s*\))?$")


def type_family(t: str) -> str:
    u = " ".join(str(t).upper().split())
    if _NUMERIC.match(u):
        return "numeric"
    if u == "DATE":
        return "date"
    if u in _TIMESTAMP:
        return "timestamp"
    if u in _TIMESTAMPTZ:
        return "timestamptz"
    if _VARCHAR.match(u):
        return "varchar"
    if u in ("BOOLEAN", "BOOL", "LOGICAL"):
        return "boolean"
    return "other:" + u


def compare_mode(type_sf: str, type_m: str) -> Tuple[str, bool]:
    """(mode, flagged). mode in numeric | timestamp | exact | varchar.

    TIMESTAMP WITH TIME ZONE against a naive TIMESTAMP/DATE compares as a UTC
    TIMESTAMP (flagged): Parquet written with isAdjustedToUTC=true reads back as
    TIMESTAMPTZ, and a VARCHAR comparison would fail every row on the "+00"
    suffix alone. Every other cross-family mismatch compares as VARCHAR (flagged).
    """
    fa, fb = type_family(type_sf), type_family(type_m)
    if fa == "numeric" and fb == "numeric":
        return "numeric", False
    if {fa, fb} <= {"date", "timestamp"}:
        return ("exact" if fa == fb else "timestamp"), False
    if fa == fb:
        return "exact", False
    if "timestamptz" in (fa, fb) and {fa, fb} <= {"date", "timestamp", "timestamptz"}:
        return "timestamp", True
    return "varchar", True


def ts_expr(expr: str, type_name: str) -> str:
    """A naive TIMESTAMP, independent of the session TimeZone. A plain CAST of a
    TIMESTAMPTZ shifts it into the session zone (DuckDB+ICU), epoch_us does not."""
    if type_family(type_name) == "timestamptz":
        return f"make_timestamp(epoch_us({expr}))"
    return f"CAST({expr} AS TIMESTAMP)"


def diff_predicate(mode: str, a: str, b: str, tol: float,
                   type_a: str = "TIMESTAMP", type_b: str = "TIMESTAMP") -> str:
    """SQL that is TRUE when the two values differ.

    Numeric: equal (incl. NULL = NULL, NaN = NaN, inf = inf) or both FINITE and
    within the relative tolerance. The isfinite() guard matters: without it
    abs(inf - x) <= tol * greatest(1, inf, x) is inf <= inf (TRUE) and a NaN
    operand makes both sides NaN, which DuckDB orders as equal -- so a port that
    swaps Snowflake's DIV0 (0) for plain division (inf / NaN on DuckDB) would
    pass. coalesce(..., false) keeps NULL vs 0 a difference (isfinite(NULL) is
    NULL, which would otherwise drop the row from the FILTER).
    """
    if mode == "numeric":
        ad, bd = f"CAST({a} AS DOUBLE)", f"CAST({b} AS DOUBLE)"
        return (f"NOT ({ad} IS NOT DISTINCT FROM {bd} OR coalesce(isfinite({ad}) AND isfinite({bd}) "
                f"AND abs({ad} - {bd}) <= {tol!r} * greatest(1, abs({ad}), abs({bd})), false))")
    if mode == "timestamp":
        return f"{ts_expr(a, type_a)} IS DISTINCT FROM {ts_expr(b, type_b)}"
    if mode == "varchar":
        return f"CAST({a} AS VARCHAR) IS DISTINCT FROM CAST({b} AS VARCHAR)"
    return f"{a} IS DISTINCT FROM {b}"


def key_condition(mode: str, a: str, b: str,
                  type_a: str = "TIMESTAMP", type_b: str = "TIMESTAMP") -> str:
    """Join condition for one key column (exact, no tolerance)."""
    if mode == "timestamp":
        return f"{ts_expr(a, type_a)} IS NOT DISTINCT FROM {ts_expr(b, type_b)}"
    if mode == "varchar":
        return f"CAST({a} AS VARCHAR) IS NOT DISTINCT FROM CAST({b} AS VARCHAR)"
    return f"{a} IS NOT DISTINCT FROM {b}"


# --------------------------------------------------------------------------- SQL

def columns_sql(specs: Sequence[ModelSpec]) -> str:
    names = []
    for s in specs:
        for sch, tbl in (s.answer_key, s.relation):
            v = f"{sch.lower()}.{tbl.lower()}"
            if v not in names:
                names.append(v)
    in_list = ", ".join(qlit(n) for n in names)
    return (
        "SELECT table_catalog, table_schema, table_name, column_name, data_type, ordinal_position, "
        "current_database() AS current_db, current_setting('search_path') AS search_path "
        "FROM information_schema.columns "
        f"WHERE lower(table_schema) || '.' || lower(table_name) IN ({in_list}) "
        "ORDER BY table_schema, table_name, table_catalog, ordinal_position"
    )


Columns = List[Tuple[str, str]]  # [(column_name, data_type)] in ordinal order


def parse_columns(cols: List[str], rows: List[List[Any]]) -> Tuple[Dict[Tuple[str, str], Columns],
                                                                    Dict[Tuple[str, str], List[str]]]:
    """-> ({(schema, table) lower: [(col, type)]}, {(schema, table): [other catalogs]})"""
    idx = {c.lower(): i for i, c in enumerate(cols)}
    if not rows:
        return {}, {}
    current_db = rows[0][idx["current_db"]]
    search_path = rows[0][idx["search_path"]]
    chosen = choose_catalogs(
        [(r[idx["table_catalog"]], r[idx["table_schema"]], r[idx["table_name"]]) for r in rows],
        search_path, current_db)
    out: Dict[Tuple[str, str], Columns] = {}
    for r in sorted(rows, key=lambda r: int(r[idx["ordinal_position"]])):
        k = (str(r[idx["table_schema"]]).lower(), str(r[idx["table_name"]]).lower())
        if k not in chosen or r[idx["table_catalog"]] != chosen[k][0]:
            continue
        out.setdefault(k, []).append((str(r[idx["column_name"]]), str(r[idx["data_type"]])))
    return out, {k: v[1] for k, v in chosen.items() if v[1]}


@dataclass
class ColumnPlan:
    name: str            # lower-case display name
    sf_name: str
    m_name: str
    sf_type: str
    m_type: str
    mode: str
    flagged: bool
    is_key: bool


@dataclass
class ModelResult:
    name: str
    relation: str
    answer_key: str
    status: str = "error"            # match | mismatch | missing | error
    rows_sf: Optional[int] = None
    rows_molinia: Optional[int] = None
    only_in_sf: Optional[int] = None
    only_in_molinia: Optional[int] = None
    dup_keys_sf: Optional[int] = None
    dup_keys_molinia: Optional[int] = None
    columns: List[Dict[str, Any]] = field(default_factory=list)
    columns_only_in_sf: List[str] = field(default_factory=list)
    columns_only_in_molinia: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def plan_model(spec: ModelSpec, sf_cols: Columns, m_cols: Columns,
               res: ModelResult) -> Optional[List[ColumnPlan]]:
    sf_map = {c.lower(): (c, t) for c, t in sf_cols}
    m_map = {c.lower(): (c, t) for c, t in m_cols}
    keys = [k.lower() for k in spec.key]
    missing_keys = [(side, k) for k in keys for side, mp in (("answer key", sf_map), ("Molinia", m_map))
                    if k not in mp]
    res.columns_only_in_sf = [sf_map[c][0] for c in sf_map if c not in m_map]
    res.columns_only_in_molinia = [m_map[c][0] for c in m_map if c not in sf_map]
    if missing_keys:
        res.status = "error"
        res.error = "key column(s) missing: " + ", ".join(f"{k} in {side}" for side, k in missing_keys)
        return None
    plans: List[ColumnPlan] = []
    order = keys + [c.lower() for c, _ in sf_cols if c.lower() in m_map and c.lower() not in keys]
    for c in order:
        sfn, sft = sf_map[c]
        mn, mt = m_map[c]
        mode, flagged = compare_mode(sft, mt)
        plans.append(ColumnPlan(c, sfn, mn, sft, mt, mode, flagged, c in keys))
    return plans


def comparison_sql(spec: ModelSpec, plans: Sequence[ColumnPlan], tol: float) -> str:
    sf_rel = f"{qident(spec.answer_key[0])}.{qident(spec.answer_key[1])}"
    m_rel = f"{qident(spec.relation[0])}.{qident(spec.relation[1])}"
    keys = [p for p in plans if p.is_key]
    vals = [p for p in plans if not p.is_key]
    on = " AND ".join(key_condition(p.mode, f"s.{qident(p.sf_name)}", f"m.{qident(p.m_name)}",
                                    p.sf_type, p.m_type) for p in keys)
    sf_keys = ", ".join(qident(p.sf_name) for p in keys)
    m_keys = ", ".join(qident(p.m_name) for p in keys)
    both = f"s.{qident(PRESENT)} IS NOT NULL AND m.{qident(PRESENT)} IS NOT NULL"
    select = [
        f"(SELECT count(*) FROM {sf_rel})::BIGINT AS rows_sf",
        f"(SELECT count(*) FROM {m_rel})::BIGINT AS rows_molinia",
        f"(SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM {sf_rel} GROUP BY {sf_keys} "
        f"HAVING count(*) > 1) AS d)::BIGINT AS dup_keys_sf",
        f"(SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) AS n FROM {m_rel} GROUP BY {m_keys} "
        f"HAVING count(*) > 1) AS d)::BIGINT AS dup_keys_molinia",
        f"count(*) FILTER (WHERE m.{qident(PRESENT)} IS NULL)::BIGINT AS only_in_sf",
        f"count(*) FILTER (WHERE s.{qident(PRESENT)} IS NULL)::BIGINT AS only_in_molinia",
    ]
    for i, p in enumerate(vals):
        pred = diff_predicate(p.mode, f"s.{qident(p.sf_name)}", f"m.{qident(p.m_name)}", tol,
                              p.sf_type, p.m_type)
        select.append(f"count(*) FILTER (WHERE {both} AND ({pred}))::BIGINT AS diff_{i}")
    # Starts with SELECT (not WITH): Molinia classifies a WITH statement by
    # scanning for insert/update/delete words, a SELECT is always a read.
    return (
        "SELECT " + ", ".join(select) + " "
        f"FROM (SELECT 1 AS {qident(PRESENT)}, * FROM {sf_rel}) AS s "
        f"FULL OUTER JOIN (SELECT 1 AS {qident(PRESENT)}, * FROM {m_rel}) AS m ON {on}"
    )


def apply_counts(res: ModelResult, plans: Sequence[ColumnPlan], cols: List[str], row: List[Any]) -> None:
    v = {c.lower(): row[i] for i, c in enumerate(cols)}
    res.rows_sf = to_int(v["rows_sf"])
    res.rows_molinia = to_int(v["rows_molinia"])
    res.dup_keys_sf = to_int(v["dup_keys_sf"])
    res.dup_keys_molinia = to_int(v["dup_keys_molinia"])
    res.only_in_sf = to_int(v["only_in_sf"])
    res.only_in_molinia = to_int(v["only_in_molinia"])
    vals = [p for p in plans if not p.is_key]
    res.columns = []
    for p in plans:
        entry = {"column": p.name, "key": p.is_key, "sf_type": p.sf_type, "molinia_type": p.m_type,
                 "compare_as": p.mode, "type_mismatch_flagged": p.flagged, "differing_rows": None}
        if not p.is_key:
            entry["differing_rows"] = to_int(v[f"diff_{vals.index(p)}"])
        res.columns.append(entry)
    ok = (
        res.rows_sf == res.rows_molinia
        and res.only_in_sf == 0 and res.only_in_molinia == 0
        and res.dup_keys_sf == 0 and res.dup_keys_molinia == 0
        and all((c["differing_rows"] or 0) == 0 for c in res.columns)
        and not res.columns_only_in_sf and not res.columns_only_in_molinia
    )
    res.status = "match" if ok else "mismatch"


# --------------------------------------------------------------------------- run

def run_parity(executor, specs: Sequence[ModelSpec], tol: float,
               show_sql: bool = False) -> List[ModelResult]:
    results = [ModelResult(s.name, f"{s.relation[0]}.{s.relation[1]}",
                           f"{s.answer_key[0]}.{s.answer_key[1]}") for s in specs]
    sql = columns_sql(specs)
    if show_sql:
        print(f"-- columns\n{sql};", file=sys.stderr)
    try:
        cols, rows = executor.query(sql)
    except MoliniaError as exc:
        for r in results:
            r.error = f"column discovery failed: {exc}"
        return results
    by_rel, dupes = parse_columns(cols, rows)

    for spec, res in zip(specs, results):
        sf_key = (spec.answer_key[0].lower(), spec.answer_key[1].lower())
        m_key = (spec.relation[0].lower(), spec.relation[1].lower())
        for k, label in ((sf_key, "answer key"), (m_key, "Molinia relation")):
            if k in dupes:
                res.notes.append(f"{label} {k[0]}.{k[1]} also exists in catalog(s) {', '.join(dupes[k])}")
        missing = [lbl for k, lbl in ((sf_key, f"answer key {res.answer_key}"),
                                      (m_key, f"Molinia relation {res.relation}")) if k not in by_rel]
        if missing:
            res.status = "missing"
            res.error = " and ".join(missing) + " not found"
            continue
        plans = plan_model(spec, by_rel[sf_key], by_rel[m_key], res)
        if plans is None:
            continue
        sql = comparison_sql(spec, plans, tol)
        if show_sql:
            print(f"-- {spec.name}\n{sql};", file=sys.stderr)
        try:
            qcols, qrows = executor.query(sql)
        except MoliniaError as exc:
            res.status = "error"
            res.error = str(exc)
            continue
        if len(qrows) != 1:
            res.status = "error"
            res.error = f"comparison query returned {len(qrows)} rows, expected 1"
            continue
        apply_counts(res, plans, qcols, qrows[0])
    return results


def _fmt(n: Optional[int]) -> str:
    return "-" if n is None else f"{n:,}"


def render(results: Sequence[ModelResult], target: str, calls: int, elapsed: float,
           tol: float = DEFAULT_TOLERANCE) -> str:
    lines = [f"Parity: dbt output vs Snowflake answer key  [{target}]", ""]
    rows = []
    for r in results:
        diff_cols = sum(1 for c in r.columns if (c.get("differing_rows") or 0) > 0)
        diff_cols += len(r.columns_only_in_sf) + len(r.columns_only_in_molinia)
        rows.append([r.name, r.status.upper() if r.status != "match" else "match",
                     _fmt(r.rows_sf), _fmt(r.rows_molinia), _fmt(r.only_in_sf), _fmt(r.only_in_molinia),
                     "-" if r.status in ("missing", "error") else str(diff_cols)])
    lines.append(format_table(["model", "status", "rows sf", "rows molinia", "only sf", "only molinia",
                               "cols differing"], rows,
                              align_right=[False, False, True, True, True, True, True]))
    details = []
    for r in results:
        d = []
        if r.error:
            d.append(f"  error: {r.error}")
        if r.rows_sf is not None and r.rows_sf != r.rows_molinia:
            d.append(f"  row count: Snowflake {r.rows_sf:,}, Molinia {r.rows_molinia:,}")
        if r.only_in_sf:
            d.append(f"  {r.only_in_sf:,} key(s) only in the Snowflake answer key")
        if r.only_in_molinia:
            d.append(f"  {r.only_in_molinia:,} key(s) only in Molinia")
        if r.dup_keys_sf:
            d.append(f"  key not unique in the answer key: {r.dup_keys_sf:,} surplus row(s)")
        if r.dup_keys_molinia:
            d.append(f"  key not unique in Molinia: {r.dup_keys_molinia:,} surplus row(s)")
        for c in r.columns:
            if (c.get("differing_rows") or 0) > 0:
                how = {"numeric": f"numeric, relative tolerance {tol:g}", "timestamp": "as TIMESTAMP",
                       "varchar": "as VARCHAR", "exact": "exact"}[c["compare_as"]]
                d.append(f"  {c['column']}: {c['differing_rows']:,} row(s) differ ({how}; "
                         f"sf {c['sf_type']} vs molinia {c['molinia_type']})")
        if r.columns_only_in_sf:
            d.append("  column(s) only in the Snowflake answer key: " + ", ".join(r.columns_only_in_sf))
        if r.columns_only_in_molinia:
            d.append("  column(s) only in Molinia: " + ", ".join(r.columns_only_in_molinia))
        for c in r.columns:
            if c["type_mismatch_flagged"]:
                as_ = "UTC TIMESTAMP" if c["compare_as"] == "timestamp" else "VARCHAR"
                d.append(f"  type mismatch on {c['column']}{' (key)' if c['key'] else ''}: sf {c['sf_type']} vs "
                         f"molinia {c['molinia_type']} (compared as {as_})")
            elif c["compare_as"] == "timestamp":
                d.append(f"  note: {c['column']} compared as TIMESTAMP (sf {c['sf_type']} vs "
                         f"molinia {c['molinia_type']})")
        for n in r.notes:
            d.append(f"  note: {n}")
        if d:
            details.append(f"{r.name} ({r.relation} vs {r.answer_key}): {r.status.upper()}")
            details.extend(d)
    if details:
        lines += ["", "Details:"] + details
    matched = sum(1 for r in results if r.status == "match")
    lines += ["", f"{matched}/{len(results)} models match  ({calls} queries, {elapsed:.1f}s)"]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agent/parity.py",
        description="Compare each ported model on Molinia (<schema>.<model>) with its Snowflake answer key "
                    "(main.sf_<model>). Counts only, never rows. Exit 0 only if every model matches.",
    )
    p.add_argument("--model", action="extend", nargs="+", metavar="NAME", default=[],
                   help="only these models (repeatable; default: all in parity.yml)")
    p.add_argument("--local-duckdb", metavar="FILE",
                   help="run the identical SQL against a local DuckDB file instead of Molinia")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="parity config (default agent/parity.yml)")
    p.add_argument("--show-sql", action="store_true", help="print the generated SQL to stderr")
    return p


def main(argv: Optional[Sequence[str]] = None, executor=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        specs, tol = load_config(Path(args.config))
        if args.model:
            known = {s.name for s in specs}
            unknown = [m for m in args.model if m not in known]
            if unknown:
                raise ConfigError(f"unknown model(s): {', '.join(unknown)} (known: {', '.join(sorted(known))})")
            specs = [s for s in specs if s.name in set(args.model)]
        if executor is None:
            if args.local_duckdb:
                executor = DuckDBExecutor(args.local_duckdb)
            else:
                executor = MoliniaExecutor(MoliniaClient.from_env())
    except (ConfigError, _env.EnvError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    t0 = time.monotonic()
    results = run_parity(executor, specs, tol, show_sql=args.show_sql)
    elapsed = time.monotonic() - t0
    all_match = all(r.status == "match" for r in results)
    if args.json:
        print(json.dumps({
            "matched": all_match,
            "models_total": len(results),
            "models_matched": sum(1 for r in results if r.status == "match"),
            "target": executor.describe(),
            "queries": executor.calls,
            "tolerance": tol,
            "models": [r.to_dict() for r in results],
        }, indent=2, default=str))
    else:
        print(render(results, executor.describe(), executor.calls, elapsed, tol))
    return 0 if all_match else 1


if __name__ == "__main__":
    sys.exit(main())
