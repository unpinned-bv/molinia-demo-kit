"""Unit tests for the demo kit's parity checker and tools.

Local only: builds DuckDB fixtures in a temp dir and drives the Molinia client
through a fake transport. No Snowflake, Molinia or MinIO calls; nothing under
.secrets/ is read.

Run: .venv/bin/python -m unittest discover -s agent/tests -v   (or `make test`)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "tools"))
sys.path.insert(0, str(KIT / "agent"))

import duckdb  # noqa: E402

import _env  # noqa: E402
import _molinia_client as mc  # noqa: E402
import minio_upload  # noqa: E402
import molinia  # noqa: E402
import parity  # noqa: E402
import sf  # noqa: E402


# =========================================================================== fixtures

FIXTURE_SQL = """
CREATE SCHEMA staging;
CREATE SCHEMA intermediate;
CREATE SCHEMA marts;

-- ok: Snowflake-style upper-case DECIMAL/DATE answer key vs lower-case
-- BIGINT/DOUBLE/TIMESTAMP output, float noise in ratio. Molinia side is a VIEW.
CREATE TABLE main.sf_ok ("ID" DECIMAL(38,0), "AMOUNT" DECIMAL(12,2), "RATIO" DECIMAL(10,4),
                         "NAME" VARCHAR, "SIGNUP_DATE" DATE, "FLAG" BOOLEAN);
INSERT INTO main.sf_ok VALUES
  (1, 10.10, 0.3000, 'Ann', DATE '2025-01-01', true),
  (2, 20.20, 0.1000, 'Bob', DATE '2025-02-01', NULL),
  (3, NULL,  NULL,   NULL,  NULL,              false);
CREATE TABLE staging.ok_base (id BIGINT, amount DOUBLE, ratio DOUBLE, name VARCHAR,
                              signup_date TIMESTAMP, flag BOOLEAN);
INSERT INTO staging.ok_base VALUES
  (1, 10.1, 0.1 + 0.2, 'Ann', TIMESTAMP '2025-01-01 00:00:00', true),
  (2, 20.2, 0.1,       'Bob', TIMESTAMP '2025-02-01 00:00:00', NULL),
  (3, NULL, NULL,      NULL,  NULL,                            false);
CREATE VIEW staging.ok AS SELECT * FROM staging.ok_base;

-- cent: one row differs by exactly one cent
CREATE TABLE main.sf_cent ("ORDER_ID" DECIMAL(38,0), "NET_AMOUNT" DECIMAL(12,2));
INSERT INTO main.sf_cent VALUES (1, 100.00), (2, 1234567.89), (3, 0.05);
CREATE TABLE marts.cent (order_id INTEGER, net_amount DECIMAL(12,2));
INSERT INTO marts.cent VALUES (1, 100.00), (2, 1234567.90), (3, 0.05);

-- keys: key 3 only in Snowflake, key 4 only in Molinia
CREATE TABLE main.sf_keys ("K" INTEGER, "V" VARCHAR);
INSERT INTO main.sf_keys VALUES (1, 'a'), (2, 'b'), (3, 'c');
CREATE TABLE marts.keys (k INTEGER, v VARCHAR);
INSERT INTO marts.keys VALUES (1, 'a'), (2, 'b'), (4, 'd');

-- dates: DATE vs TIMESTAMP compared as TIMESTAMP (a 1-second offset is a diff)
CREATE TABLE main.sf_dates ("K" INTEGER, "D" DATE);
INSERT INTO main.sf_dates VALUES (1, DATE '2025-03-01'), (2, DATE '2025-03-02');
CREATE TABLE marts.dates (k INTEGER, d TIMESTAMP);
INSERT INTO marts.dates VALUES (1, TIMESTAMP '2025-03-01 00:00:00'), (2, TIMESTAMP '2025-03-02 00:00:01');

-- cols: a column only on each side
CREATE TABLE main.sf_cols ("K" INTEGER, "SAME" VARCHAR, "EXTRA_SF" INTEGER);
INSERT INTO main.sf_cols VALUES (1, 'x', 9);
CREATE TABLE marts.cols (k INTEGER, same VARCHAR, extra_m INTEGER);
INSERT INTO marts.cols VALUES (1, 'x', 9);

-- typed: VARCHAR vs INTEGER -> compared as VARCHAR and flagged
CREATE TABLE main.sf_typed ("K" INTEGER, "CODE" VARCHAR);
INSERT INTO main.sf_typed VALUES (1, '7'), (2, '08');
CREATE TABLE marts.typed (k INTEGER, code INTEGER);
INSERT INTO marts.typed VALUES (1, 7), (2, 8);

-- composite: (order_month DATE vs TIMESTAMP, country_code) key, like rpt_monthly_revenue
CREATE TABLE main.sf_composite ("ORDER_MONTH" DATE, "COUNTRY_CODE" VARCHAR, "REVENUE" DECIMAL(14,2),
                                "CHANNELS" VARCHAR);
INSERT INTO main.sf_composite VALUES
  (DATE '2025-01-01', 'NL', 1000.50, 'app,web'),
  (DATE '2025-01-01', 'DE', 20.00, 'web'),
  (DATE '2025-02-01', 'NL', 0.00, NULL);
CREATE TABLE intermediate.composite (order_month TIMESTAMP, country_code VARCHAR, revenue DOUBLE,
                                     channels VARCHAR);
INSERT INTO intermediate.composite VALUES
  (TIMESTAMP '2025-02-01', 'NL', 0.0, NULL),
  (TIMESTAMP '2025-01-01', 'NL', 1000.5, 'app,web'),
  (TIMESTAMP '2025-01-01', 'DE', 20.0, 'web');

-- dups: duplicate key on the Molinia side
CREATE TABLE main.sf_dups ("K" INTEGER, "V" INTEGER);
INSERT INTO main.sf_dups VALUES (1, 1), (2, 2);
CREATE TABLE marts.dups (k INTEGER, v INTEGER);
INSERT INTO marts.dups VALUES (1, 1), (2, 2), (2, 2);

-- nokey: key column absent on the Molinia side
CREATE TABLE main.sf_nokey ("K" INTEGER, "V" INTEGER);
INSERT INTO main.sf_nokey VALUES (1, 1);
CREATE TABLE marts.nokey (other INTEGER, v INTEGER);
INSERT INTO marts.nokey VALUES (1, 1);

-- tz: Parquet written with isAdjustedToUTC=true reads back as TIMESTAMPTZ;
-- compared as a UTC TIMESTAMP whatever the session TimeZone (flagged, still a match)
SET TimeZone = 'Europe/Amsterdam';
CREATE TABLE main.sf_tz ("K" INTEGER, "TS" TIMESTAMPTZ);
INSERT INTO main.sf_tz VALUES (1, TIMESTAMPTZ '2025-01-01 10:00:00+00'), (2, NULL);
CREATE TABLE marts.tz (k INTEGER, ts TIMESTAMP);
INSERT INTO marts.tz VALUES (1, TIMESTAMP '2025-01-01 10:00:00'), (2, NULL);

-- missing: answer key exists, Molinia relation does not
CREATE TABLE main.sf_missing ("K" INTEGER);
INSERT INTO main.sf_missing VALUES (1);

-- nonfinite: Snowflake DIV0 gives 0.00 where DuckDB 1.5.5 plain division gives
-- NaN (0/0), +inf (x/0) or -inf (-x/0); none of those may pass the tolerance.
-- NULL vs 0 differs too. SHARE is non-finite on BOTH sides and matches.
CREATE TABLE main.sf_nonfinite ("K" INTEGER, "AVG_ORDER_VALUE" DECIMAL(12,2), "SHARE" DOUBLE);
INSERT INTO main.sf_nonfinite VALUES
  (1, 0.00, 'NaN'::DOUBLE), (2, 0.00, 'inf'::DOUBLE), (3, 0.00, '-inf'::DOUBLE),
  (4, 12.34, 0.5), (5, 0.00, NULL);
CREATE TABLE marts.nonfinite (k INTEGER, avg_order_value DOUBLE, share DOUBLE);
INSERT INTO marts.nonfinite VALUES
  (1, 'NaN'::DOUBLE, 'NaN'::DOUBLE), (2, 'inf'::DOUBLE, 'inf'::DOUBLE), (3, '-inf'::DOUBLE, '-inf'::DOUBLE),
  (4, 12.34, 0.5), (5, NULL, NULL);
"""

FIXTURE_CONFIG = """
tolerance: 1.0e-9
models:
  - {name: ok,        relation: staging.ok,             answer_key: main.sf_ok,        key: [id]}
  - {name: cent,      relation: marts.cent,             answer_key: main.sf_cent,      key: [order_id]}
  - {name: keys,      relation: marts.keys,             answer_key: main.sf_keys,      key: [k]}
  - {name: dates,     relation: marts.dates,            answer_key: main.sf_dates,     key: [k]}
  - {name: cols,      relation: marts.cols,             answer_key: main.sf_cols,      key: [k]}
  - {name: typed,     relation: marts.typed,            answer_key: main.sf_typed,     key: [k]}
  - {name: composite, relation: intermediate.composite, answer_key: main.sf_composite, key: [order_month, country_code]}
  - {name: dups,      relation: marts.dups,             answer_key: main.sf_dups,      key: [k]}
  - {name: nokey,     relation: marts.nokey,            answer_key: main.sf_nokey,     key: [k]}
  - {name: tz,        relation: marts.tz,               answer_key: main.sf_tz,        key: [k]}
  - {name: missing,   relation: marts.missing,          answer_key: main.sf_missing,   key: [k]}
  - {name: nonfinite, relation: marts.nonfinite,        answer_key: main.sf_nonfinite, key: [k]}
"""


class CountingExecutor:
    """Wraps DuckDBExecutor to record every SQL statement sent."""

    def __init__(self, inner):
        self.inner = inner
        self.sql = []

    @property
    def calls(self):
        return len(self.sql)

    def describe(self):
        return self.inner.describe()

    def query(self, sql):
        self.sql.append(sql)
        return self.inner.query(sql)


def run_main(fn, argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn(argv, **kw)
    return code, out.getvalue(), err.getvalue()


class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="parity-test-"))
        cls.db = cls.tmp / "fixture.duckdb"
        con = duckdb.connect(str(cls.db))
        con.execute(FIXTURE_SQL)
        con.close()
        cls.cfg = cls.tmp / "parity.yml"
        cls.cfg.write_text(FIXTURE_CONFIG)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_parity(self, *extra):
        inner = parity.DuckDBExecutor(str(self.db))
        inner.con.execute("SET TimeZone = 'America/New_York'")  # results must not depend on it
        ex = CountingExecutor(inner)
        code, out, _ = run_main(parity.main, ["--config", str(self.cfg), "--json", *extra], executor=ex)
        doc = json.loads(out)
        return code, {m["name"]: m for m in doc["models"]}, doc, ex

    def col(self, model, name):
        return next(c for c in model["columns"] if c["column"] == name)

    def test_all_models_counts_and_exit_code(self):
        code, m, doc, ex = self.run_parity()
        self.assertEqual(code, 1)
        self.assertFalse(doc["matched"])

        ok = m["ok"]
        self.assertEqual(ok["status"], "match", ok)
        self.assertEqual((ok["rows_sf"], ok["rows_molinia"], ok["only_in_sf"], ok["only_in_molinia"]),
                         (3, 3, 0, 0))
        self.assertEqual(self.col(ok, "amount")["compare_as"], "numeric")   # DECIMAL vs DOUBLE
        self.assertEqual(self.col(ok, "amount")["differing_rows"], 0)
        self.assertEqual(self.col(ok, "ratio")["differing_rows"], 0)        # 0.1+0.2 vs 0.3000
        self.assertEqual(self.col(ok, "signup_date")["compare_as"], "timestamp")
        self.assertFalse(self.col(ok, "signup_date")["type_mismatch_flagged"])
        self.assertEqual(self.col(ok, "flag")["compare_as"], "exact")

        cent = m["cent"]
        self.assertEqual(cent["status"], "mismatch")
        self.assertEqual(self.col(cent, "net_amount")["differing_rows"], 1)
        self.assertEqual((cent["only_in_sf"], cent["only_in_molinia"]), (0, 0))

        keys = m["keys"]
        self.assertEqual(keys["status"], "mismatch")
        self.assertEqual((keys["rows_sf"], keys["rows_molinia"]), (3, 3))
        self.assertEqual((keys["only_in_sf"], keys["only_in_molinia"]), (1, 1))
        self.assertEqual(self.col(keys, "v")["differing_rows"], 0)  # only key-matched rows count

        dates = m["dates"]
        self.assertEqual(dates["status"], "mismatch")
        self.assertEqual(self.col(dates, "d")["compare_as"], "timestamp")
        self.assertEqual(self.col(dates, "d")["differing_rows"], 1)

        cols = m["cols"]
        self.assertEqual(cols["status"], "mismatch")
        self.assertEqual(cols["columns_only_in_sf"], ["EXTRA_SF"])
        self.assertEqual(cols["columns_only_in_molinia"], ["extra_m"])
        self.assertEqual(self.col(cols, "same")["differing_rows"], 0)

        typed = m["typed"]
        code_col = self.col(typed, "code")
        self.assertTrue(code_col["type_mismatch_flagged"])
        self.assertEqual(code_col["compare_as"], "varchar")
        self.assertEqual(code_col["differing_rows"], 1)  # '08' vs 8; '7' vs 7 is equal as text

        comp = m["composite"]
        self.assertEqual(comp["status"], "match", comp)
        self.assertEqual(self.col(comp, "order_month")["compare_as"], "timestamp")
        self.assertTrue(self.col(comp, "order_month")["key"])

        dups = m["dups"]
        self.assertEqual(dups["status"], "mismatch")
        self.assertEqual((dups["dup_keys_sf"], dups["dup_keys_molinia"]), (0, 1))

        self.assertEqual(m["nokey"]["status"], "error")
        self.assertIn("k in Molinia", m["nokey"]["error"])

        tz = m["tz"]
        self.assertEqual(tz["status"], "match", tz)
        self.assertEqual(self.col(tz, "ts")["compare_as"], "timestamp")
        self.assertTrue(self.col(tz, "ts")["type_mismatch_flagged"])

        self.assertEqual(m["missing"]["status"], "missing")
        self.assertIn("marts.missing", m["missing"]["error"])

        nf = m["nonfinite"]
        self.assertEqual(nf["status"], "mismatch", nf)
        self.assertEqual(self.col(nf, "avg_order_value")["compare_as"], "numeric")
        self.assertEqual(self.col(nf, "avg_order_value")["differing_rows"], 4)  # NaN, inf, -inf, NULL vs 0
        self.assertEqual(self.col(nf, "share")["differing_rows"], 0)            # NaN=NaN, inf=inf, NULL=NULL

    def test_one_discovery_query_plus_one_per_comparable_model(self):
        _, m, doc, ex = self.run_parity()
        compared = [n for n, r in m.items() if r["status"] in ("match", "mismatch")]
        self.assertEqual(ex.calls, 1 + len(compared))
        self.assertEqual(doc["queries"], ex.calls)
        self.assertIn("information_schema.columns", ex.sql[0])
        self.assertEqual(sum("information_schema" in s for s in ex.sql), 1)
        for s in ex.sql[1:]:
            # Molinia classifies statements by their first verb; a SELECT is a read.
            self.assertTrue(s.startswith("SELECT "), s[:40])
            self.assertNotRegex(s.lower(), r"\b(insert|update|delete)\s")

    def test_comparison_returns_counts_only(self):
        _, _, _, ex = self.run_parity("--model", "ok")
        con = duckdb.connect(str(self.db), read_only=True)
        try:
            rows = con.execute(ex.sql[1]).fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        self.assertTrue(all(isinstance(v, int) for v in rows[0]))

    def test_matching_subset_exits_zero(self):
        code, m, doc, _ = self.run_parity("--model", "ok", "composite", "--model", "tz")
        self.assertEqual(code, 0)
        self.assertTrue(doc["matched"])
        self.assertEqual(set(m), {"ok", "composite", "tz"})

    def test_single_failing_model_exits_one(self):
        for name in ("cent", "keys", "cols", "missing", "nokey", "dups", "dates", "nonfinite"):
            code, m, _, _ = self.run_parity("--model", name)
            self.assertEqual(code, 1, name)

    def test_unknown_model_is_usage_error(self):
        code, out, err = run_main(parity.main, ["--config", str(self.cfg), "--local-duckdb", str(self.db),
                                                "--model", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("unknown model", err)

    def test_local_duckdb_flag_and_pretty_output(self):
        code, out, _ = run_main(parity.main, ["--config", str(self.cfg), "--local-duckdb", str(self.db)])
        self.assertEqual(code, 1)
        self.assertIn("ok", out)
        self.assertIn("MISMATCH", out)
        self.assertIn("net_amount: 1 row(s) differ", out)
        self.assertIn("column(s) only in the Snowflake answer key: EXTRA_SF", out)
        self.assertIn("column(s) only in Molinia: extra_m", out)
        self.assertIn("type mismatch on code", out)
        self.assertIn("3/12 models match", out)
        self.assertIn("compared as UTC TIMESTAMP", out)

    def test_missing_duckdb_file_is_usage_error(self):
        code, _, err = run_main(parity.main, ["--config", str(self.cfg), "--local-duckdb",
                                              str(self.tmp / "nope.duckdb")])
        self.assertEqual(code, 2)

    def test_same_result_through_a_fake_molinia_http_server(self):
        """End to end over real HTTP (requests + client + pacing + one 429):
        a local server answers /query/execute from the fixture DuckDB file in
        Molinia's JSON shape. Results must equal the --local-duckdb run."""
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        db = str(self.db)
        seen = {"n": 0, "auth": set(), "paths": set()}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, headers=None):
                data = json.dumps(body, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen["n"] += 1
                seen["auth"].add(self.headers.get("Authorization"))
                seen["paths"].add(self.path)
                if seen["n"] == 2:  # one org-engine 429 mid-run
                    return self._send(429, {"code": "org_engine_rate_limit", "retryAfterSeconds": 0,
                                            "message": "Too many org-engine queries"})
                con = duckdb.connect(db, read_only=True)
                try:
                    cur = con.execute(body["sql"])
                    cols = [d[0] for d in cur.description]
                    rows = [list(r) for r in cur.fetchall()]
                except Exception as exc:
                    return self._send(400, {"statusCode": 400, "message": str(exc), "error": "Bad Request"})
                finally:
                    con.close()
                self._send(200, {"queryId": seen["n"], "status": "success", "columns": cols, "rows": rows,
                                 "rowCount": len(rows), "durationMs": 1})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            client = mc.MoliniaClient(f"http://127.0.0.1:{srv.server_address[1]}", "org_test", KEY,
                                      pacer=mc.Pacer({"query": 25, "http": 40}), sleep=lambda s: None,
                                      retry_cushion=0.0, log=lambda m: None)
            ex = parity.MoliniaExecutor(client)
            code, out, _ = run_main(parity.main, ["--config", str(self.cfg), "--json"], executor=ex)
        finally:
            srv.shutdown()
            srv.server_close()
        remote = json.loads(out)
        _, _, local, _ = self.run_parity()
        self.assertEqual(code, 1)
        strip = lambda d: [{k: v for k, v in m.items()} for m in d["models"]]  # noqa: E731
        self.assertEqual(strip(remote), strip(local))
        self.assertEqual(seen["auth"], {f"Bearer {KEY}"})
        self.assertEqual(seen["paths"], {"/api/orgs/org_test/query/execute"})
        self.assertEqual(seen["n"], remote["queries"] + 1)  # + the retried 429

    def test_repo_config_lists_the_ten_models(self):
        specs, tol = parity.load_config(parity.DEFAULT_CONFIG)
        self.assertEqual(tol, 1e-9)
        self.assertEqual([s.name for s in specs], [
            "stg_customers", "stg_products", "stg_orders", "stg_order_items", "stg_payments",
            "int_order_lines", "int_web_item_views", "dim_customers", "fct_orders", "rpt_monthly_revenue"])
        by = {s.name: s for s in specs}
        for s in specs:
            self.assertEqual(s.answer_key, ("main", f"sf_{s.name}"))
            self.assertEqual(s.relation[1], s.name)
        self.assertEqual(by["int_web_item_views"].key, ["event_id", "item_position"])
        self.assertEqual(by["rpt_monthly_revenue"].key, ["order_month", "country_code"])
        self.assertEqual(by["fct_orders"].relation, ("marts", "fct_orders"))
        self.assertEqual(by["int_order_lines"].relation, ("intermediate", "int_order_lines"))
        self.assertEqual(by["stg_payments"].relation, ("staging", "stg_payments"))

    def test_numeric_predicate_rejects_nan_and_infinity(self):
        pred = parity.diff_predicate("numeric", "a", "b", 1e-9)
        con = duckdb.connect()

        def differs(a, b):
            sql = f"SELECT count(*) FILTER (WHERE {pred}) FROM (SELECT {a}::DOUBLE AS a, {b} AS b)"
            return con.execute(sql).fetchone()[0] == 1

        nan, inf, ninf = "'NaN'", "'inf'", "'-inf'"
        for a, b in ((inf, "0.00::DECIMAL(12,2)"), (nan, "5::INTEGER"), (ninf, "0::BIGINT"),
                     (ninf, "'inf'::DOUBLE"), (inf, "5.25::DOUBLE"), ("NULL", "0::INTEGER"),
                     (nan, "NULL::DOUBLE"), ("12.34", "12.35::DECIMAL(12,2)"), ("1/0", "0::INTEGER"),
                     ("0.0/0", "0::INTEGER")):
            self.assertTrue(differs(a, b), f"{a} vs {b} must differ")
        for a, b in ((nan, "'NaN'::DOUBLE"), (inf, "'inf'::DOUBLE"), (ninf, "'-inf'::DOUBLE"),
                     ("NULL", "NULL::DOUBLE"), ("0.1 + 0.2", "0.3::DECIMAL(10,4)"),
                     ("1234567.89", "1234567.89::DECIMAL(12,2)")):
            self.assertFalse(differs(a, b), f"{a} vs {b} must match")

    def test_compare_mode_rules(self):
        cm = parity.compare_mode
        self.assertEqual(cm("DECIMAL(38,0)", "BIGINT"), ("numeric", False))
        self.assertEqual(cm("DECIMAL(12,2)", "DOUBLE"), ("numeric", False))
        self.assertEqual(cm("DATE", "TIMESTAMP"), ("timestamp", False))
        self.assertEqual(cm("TIMESTAMP_NS", "DATE"), ("timestamp", False))
        self.assertEqual(cm("DATE", "DATE"), ("exact", False))
        self.assertEqual(cm("VARCHAR", "VARCHAR"), ("exact", False))
        self.assertEqual(cm("VARCHAR", "INTEGER"), ("varchar", True))
        self.assertEqual(cm("TIMESTAMP WITH TIME ZONE", "TIMESTAMP"), ("timestamp", True))
        self.assertEqual(cm("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE"), ("exact", False))
        self.assertEqual(cm("TIMESTAMP WITH TIME ZONE", "VARCHAR"), ("varchar", True))
        self.assertEqual(cm("BOOLEAN", "BOOLEAN"), ("exact", False))

    def test_catalog_choice_follows_search_path(self):
        rows = [("org-12", "main", "sf_x"), ("__rd_lake", "main", "sf_x"), ("temp", "main", "raw_c"),
                ("org-12", "marts", "fct")]
        chosen = molinia.choose_catalogs(rows, '__rd_lake,"org-12"', "org-12")
        self.assertEqual(chosen[("main", "sf_x")], ("__rd_lake", ["org-12"]))
        self.assertEqual(chosen[("marts", "fct")], ("org-12", []))
        self.assertNotIn(("main", "raw_c"), chosen)  # temp (masking views) ignored
        self.assertEqual(molinia.parse_search_path('"org-12",__rd_lake'), ["org-12", "__rd_lake"])
        self.assertEqual(molinia.parse_search_path(""), [])


# =========================================================================== splitter

class SplitterTests(unittest.TestCase):
    def test_basic_and_trailing(self):
        self.assertEqual(sf.split_statements("select 1; select 2;\n"), ["select 1", "select 2"])
        self.assertEqual(sf.split_statements("select 1; select 2"), ["select 1", "select 2"])
        self.assertEqual(sf.split_statements(";; select 1;;"), ["select 1"])
        self.assertEqual(sf.split_statements("   \n"), [])

    def test_quotes(self):
        self.assertEqual(sf.split_statements("select 'a;b'; select 2"), ["select 'a;b'", "select 2"])
        self.assertEqual(sf.split_statements("select 'it''s; fine'; select 2"),
                         ["select 'it''s; fine'", "select 2"])
        self.assertEqual(sf.split_statements(r"select 'it\'s; fine'; select 2"),
                         [r"select 'it\'s; fine'", "select 2"])
        self.assertEqual(sf.split_statements('select 1 as "a;b"; select 2'), ['select 1 as "a;b"', "select 2"])
        self.assertEqual(sf.split_statements('select 1 as "a"";b"; select 2'),
                         ['select 1 as "a"";b"', "select 2"])

    def test_dollar_blocks(self):
        sql = textwrap.dedent("""\
            create or replace procedure p() returns string language javascript as
            $$
              var x = 1; var y = ';'; // not a comment boundary
              return 'ok';
            $$;
            call p();
            execute immediate $$ begin let a := 1; return a; end; $$;
        """)
        stmts = sf.split_statements(sql)
        self.assertEqual(len(stmts), 3)
        self.assertTrue(stmts[0].startswith("create or replace procedure"))
        self.assertTrue(stmts[0].endswith("$$"))
        self.assertEqual(stmts[1], "call p()")
        self.assertTrue(stmts[2].startswith("execute immediate $$"))

    def test_comments(self):
        sql = textwrap.dedent("""\
            -- header; with a semicolon
            create table t (a int); -- trailing; comment
            /* block; comment */ insert into t values (1);
            // snowflake line comment; here
            select * from t;
            -- only a comment at the end;
        """)
        stmts = sf.split_statements(sql)
        self.assertEqual(len(stmts), 3, stmts)
        self.assertIn("create table t (a int)", stmts[0])
        self.assertTrue(stmts[1].endswith("insert into t values (1)"))
        self.assertTrue(stmts[2].endswith("select * from t"))
        self.assertEqual(sf.one_line(stmts[1]), "insert into t values (1)")
        self.assertEqual(sf.one_line(stmts[2]), "select * from t")

    def test_comment_only_file(self):
        self.assertEqual(sf.split_statements("-- nothing;\n/* here; */\n"), [])

    def test_duckdb_dialect(self):
        from _sql import split_statements
        self.assertEqual(split_statements("select 7 // 2; select 1", dialect="duckdb"),
                         ["select 7 // 2", "select 1"])
        self.assertEqual(split_statements("select $tag$a;b$tag$; select 2", dialect="duckdb"),
                         ["select $tag$a;b$tag$", "select 2"])
        self.assertEqual(split_statements("select $1; select 2", dialect="duckdb"), ["select $1", "select 2"])

    def test_normalise_export_name(self):
        n = sf.normalise_export_name
        self.assertEqual(n("CUSTOMERS.parquet", "raw"), "customers.parquet")
        self.assertEqual(n("orders_0_0_0.snappy.parquet", "raw"), "orders.parquet")
        self.assertEqual(n("order_items", "raw"), "order_items.parquet")
        self.assertEqual(n("raw_orders.parquet", "raw"), "orders.parquet")
        self.assertEqual(n("FCT_ORDERS.parquet", "expected"), "fct_orders.parquet")
        self.assertEqual(n("sf_dim_customers.parquet", "expected"), "dim_customers.parquet")
        # multi-file unload to a path that already ends in .parquet
        self.assertEqual(n("customers.parquet_0_0_0.snappy.parquet", "raw"), "customers.parquet")
        self.assertEqual(n("FCT_ORDERS.PARQUET_0_1_0.snappy.parquet", "expected"), "fct_orders.parquet")
        with self.assertRaises(ValueError):
            n("weird name!.parquet", "raw")

    def test_normalise_dir_detects_collisions(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "ORDERS.parquet").write_bytes(b"x")
            (p / "orders_0_0_0.snappy.parquet").write_bytes(b"y")
            with self.assertRaises(ValueError):
                sf.normalise_dir(p, "raw")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            (p / "ORDERS.parquet").write_bytes(b"x")
            (p / "customers.parquet").write_bytes(b"y")
            self.assertEqual(sf.normalise_dir(p, "raw"), [("ORDERS.parquet", "orders.parquet")])
            self.assertEqual(sorted(x.name for x in p.iterdir()), ["customers.parquet", "orders.parquet"])

    def test_stage_is_cleared_after_use_and_before_copy(self):
        stmts = sf.split_statements(sf.UNLOAD_SQL.read_text(encoding="utf-8"), dialect="snowflake")
        out = sf.with_stage_clear(stmts)
        firsts = [sf.one_line(x, 200).split()[0].upper() for x in out]
        removes = [i for i, f in enumerate(firsts) if f == "REMOVE"]
        self.assertEqual([out[i] for i in removes], [f"REMOVE @{sf.STAGE}/raw/", f"REMOVE @{sf.STAGE}/expected/"])
        self.assertTrue(all(f == "USE" for f in firsts[:removes[0]]), firsts)
        self.assertGreater(removes[0], 0)
        self.assertEqual(firsts[removes[-1] + 1], "COPY")
        self.assertEqual(len(out), len(stmts) + 2)

    def test_planned_exports_from_unload_sql(self):
        planned = sf.planned_exports(sf.UNLOAD_SQL.read_text(encoding="utf-8"))
        self.assertEqual(planned["raw"], {f"{t}.parquet" for t in (
            "customers", "customer_contacts", "products", "orders", "order_items", "payments", "web_events")})
        specs, _ = parity.load_config(parity.DEFAULT_CONFIG)
        self.assertEqual(planned["expected"], {f"{s.name}.parquet" for s in specs})

    def test_dbt_argv(self):
        dbt = str(sf.DBT_BIN)
        self.assertEqual(sf.build_dbt_argv(["--", "build"]),
                         [dbt, "build", "--profiles-dir", ".", "--target", "snowflake"])
        self.assertEqual(sf.build_dbt_argv(["run", "--target", "molinia", "-s", "stg_orders"]),
                         [dbt, "run", "--target", "molinia", "-s", "stg_orders", "--profiles-dir", "."])
        self.assertEqual(sf.build_dbt_argv(["debug", "-t", "dev", "--profiles-dir=/x"]),
                         [dbt, "debug", "-t", "dev", "--profiles-dir=/x"])
        self.assertEqual(sf.build_dbt_argv(["--version"]), [dbt, "--version"])

    def test_run_statements_stops_at_first_failure(self):
        class Cur:
            def __init__(self):
                self.executed = []
                self.description = None
                self.rowcount = None

            def execute(self, s):
                self.executed.append(s)
                if "boom" in s:
                    raise RuntimeError("SQL compilation error: boom")
                self.description = [("status",)]
                self.rowcount = 1
                self._rows = [("Statement executed successfully.",)]

            def fetchmany(self, n):
                return self._rows[:n]

            def close(self):
                pass

        cur = Cur()

        class Conn:
            def cursor(self):
                return cur

        out = io.StringIO()
        failures = sf.run_statements(Conn(), ["select 1", "select boom", "select 3"], out=out)
        self.assertEqual(failures, 1)
        self.assertEqual(cur.executed, ["select 1", "select boom"])
        lines = out.getvalue().splitlines()
        self.assertIn("ok", lines[0])
        self.assertIn("Statement executed successfully.", lines[0])
        self.assertIn("FAIL", lines[1])
        self.assertIn("boom", lines[1])


class FakeSnowflake:
    """Connection + cursor for cmd_unload: records statements; GET writes the
    stage files named in `stage` ({kind: [file names]}) into the target dir."""

    def __init__(self, stage):
        self.stage = stage
        self.executed = []
        self.description = None
        self.rowcount = None
        self._rows = []

    def cursor(self):
        return self

    def close(self):
        pass

    def execute(self, sql):
        import re
        self.executed.append(sql)
        self.description, self.rowcount, self._rows = None, 0, []
        m = re.match(r"GET @\S+/(raw|expected)/ 'file://(.+)/'$", sql)
        if m:
            d = Path(m.group(2))
            assert d.is_dir(), d
            for name in self.stage.get(m.group(1), []):
                (d / name).write_bytes(b"not parquet")
            self._rows = [(n,) for n in self.stage.get(m.group(1), [])]

    def fetchall(self):
        return self._rows

    def fetchmany(self, n):
        return self._rows[:n]


class UnloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="unload-test-"))
        self.root = self.tmp / "acme-snowflake-export"
        for kind, names in (("raw", ["orders.parquet", "gone.parquet"]), ("expected", ["fct_orders.parquet"])):
            (self.root / kind).mkdir(parents=True)
            for n in names:
                (self.root / kind / n).write_bytes(b"previous")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def unload(self, stage, *argv):
        fake = FakeSnowflake(stage)
        with mock.patch.object(sf, "connect", return_value=fake), \
                mock.patch.object(sf, "EXPORT_ROOT", self.root), \
                mock.patch.object(sf, "print_export_table", return_value=0):
            code, out, err = run_main(sf.main, ["unload", *argv])
        return code, out, err, fake

    def tree(self):
        return {k: sorted(p.name for p in (self.root / k).iterdir()) for k in ("raw", "expected")}

    def test_clears_stage_then_replaces_exports(self):
        planned = sf.planned_exports(sf.UNLOAD_SQL.read_text(encoding="utf-8"))
        stage = {"raw": ["ORDERS_0_0_0.snappy.parquet"] + sorted(planned["raw"] - {"orders.parquet"}),
                 "expected": sorted(planned["expected"])}
        code, out, err, fake = self.unload(stage)
        self.assertEqual(code, 0, err)
        verbs = [sf.one_line(x, 200).split()[0].upper() for x in fake.executed]
        self.assertLess(verbs.index("REMOVE"), verbs.index("COPY"))
        self.assertEqual(verbs.count("REMOVE"), 2)
        self.assertEqual(verbs[-2:], ["GET", "GET"])
        self.assertEqual(self.tree(), {"raw": sorted(planned["raw"]), "expected": sorted(planned["expected"])})
        self.assertNotIn("gone.parquet", self.tree()["raw"])      # stale local file dropped
        self.assertFalse((self.root / ".incoming").exists())
        self.assertIn("renamed raw/ORDERS_0_0_0.snappy.parquet -> raw/orders.parquet", out)
        self.assertNotIn("WARNING", err)

    def test_bad_stage_file_name_keeps_previous_exports(self):
        stage = {"raw": ["orders.parquet", "weird name!.parquet"], "expected": ["fct_orders.parquet"]}
        before = self.tree()
        code, out, err, _ = self.unload(stage, "--no-copy")
        self.assertEqual(code, 2)
        self.assertIn("cannot derive a table name", err)
        self.assertIn("left untouched", err)
        self.assertEqual(self.tree(), before)
        self.assertEqual((self.root / "raw" / "orders.parquet").read_bytes(), b"previous")
        self.assertFalse((self.root / ".incoming").exists())

    def test_no_copy_warns_about_stale_and_missing_stage_files(self):
        stage = {"raw": ["orders.parquet"], "expected": ["fct_orders.parquet", "old_model.parquet"]}
        code, out, err, fake = self.unload(stage, "--no-copy")
        self.assertEqual(code, 0, err)
        self.assertFalse(any(x.startswith("REMOVE") or "COPY" in x for x in fake.executed))
        self.assertIn("WARNING: expected/old_model.parquet is not written by 02_unload.sql", err)
        self.assertIn("WARNING: raw/customers.parquet is written by 02_unload.sql but was not on the stage", err)
        self.assertEqual(self.tree()["expected"], ["fct_orders.parquet", "old_model.parquet"])


# =========================================================================== client

class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


OK = {"queryId": 1, "status": "success", "columns": ["n"], "rows": [[1]], "rowCount": 1, "durationMs": 3}
KEY = "rd_sa_TESTKEY_never_print_me"


def make_client(responses, **kw):
    sleeps = []
    session = FakeSession(responses)
    pacer = mc.Pacer({}, sleep=lambda s: None)  # pacing tested separately
    client = mc.MoliniaClient("https://api.example.test/", "org_test", KEY, session=session, pacer=pacer,
                              sleep=sleeps.append, log=lambda m: None, **kw)
    return client, session, sleeps


class ClientTests(unittest.TestCase):
    def test_request_shape(self):
        client, session, _ = make_client([FakeResponse(200, OK)])
        res = client.execute("select 1;")
        self.assertEqual(res["rows"], [[1]])
        call = session.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.example.test/api/orgs/org_test/query/execute")
        self.assertEqual(call["json"], {"sql": "select 1"})
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertNotIn(KEY, repr(client))

    def test_api_suffix_is_normalised(self):
        client, session, _ = make_client([FakeResponse(200, OK)])
        client2 = mc.MoliniaClient("https://api.example.test/api", "o", KEY, session=session,
                                   pacer=mc.Pacer({}), log=lambda m: None)
        self.assertEqual(client2.api_url, "https://api.example.test")

    def test_429_honours_retry_after_header(self):
        client, session, sleeps = make_client(
            [FakeResponse(429, {"statusCode": 429, "message": "ThrottlerException: Too Many Requests"},
                          {"Retry-After": "7"}),
             FakeResponse(200, OK)], retry_cushion=0.5)
        client.execute("select 1")
        self.assertEqual(sleeps, [7.5])
        self.assertEqual(len(session.calls), 2)

    def test_429_uses_body_retry_after_seconds_when_no_header(self):
        body = {"code": "org_engine_rate_limit", "limitPerMin": 30, "retryAfterSeconds": 12,
                "message": "Too many org-engine queries"}
        client, session, sleeps = make_client([FakeResponse(429, body), FakeResponse(200, OK)],
                                              retry_cushion=1.0)
        client.execute("select 1")
        self.assertEqual(sleeps, [13.0])

    def test_429_http_date_retry_after(self):
        wait = mc._retry_after_seconds({"Retry-After": "Wed, 21 Oct 2015 07:28:10 GMT"}, None,
                                       now=1445412480.0)  # 07:28:00
        self.assertAlmostEqual(wait, 10.0)

    def test_429_without_hint_backs_off_exponentially(self):
        client, _, sleeps = make_client([FakeResponse(429, {"message": "x"}), FakeResponse(429, None),
                                         FakeResponse(200, OK)], retry_cushion=0.0)
        client.execute("select 1")
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_daily_budget_is_not_retried(self):
        body = {"code": "org_engine_daily_budget", "limitSeconds": 3600, "usedSeconds": 3601,
                "message": "Daily org-engine compute budget reached (60 minutes)."}
        client, session, sleeps = make_client([FakeResponse(429, body)])
        with self.assertRaises(mc.RateLimitedError) as cm:
            client.execute("select 1")
        self.assertEqual(sleeps, [])
        self.assertEqual(len(session.calls), 1)
        self.assertIn("daily", str(cm.exception).lower())

    def test_gives_up_after_max_attempts(self):
        client, session, sleeps = make_client([FakeResponse(429, None, {"Retry-After": "1"})] * 3,
                                              max_attempts=3)
        with self.assertRaises(mc.RateLimitedError):
            client.execute("select 1")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(sleeps), 2)

    def test_refuses_absurd_retry_after(self):
        client, _, sleeps = make_client([FakeResponse(429, None, {"Retry-After": "3600"})], max_wait=300)
        with self.assertRaises(mc.RateLimitedError):
            client.execute("select 1")
        self.assertEqual(sleeps, [])

    def test_error_message_and_no_key_leak(self):
        body = {"statusCode": 400, "message": "Catalog Error: Table with name nope does not exist!",
                "error": "Bad Request"}
        client, _, _ = make_client([FakeResponse(400, body)])
        with self.assertRaises(mc.MoliniaError) as cm:
            client.execute("select * from nope")
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("Table with name nope", str(cm.exception))
        self.assertNotIn(KEY, str(cm.exception))

    def test_gateway_errors_retried_only_when_idempotent(self):
        client, session, sleeps = make_client([FakeResponse(503, None), FakeResponse(200, OK)])
        client.execute("select 1")  # read-only -> retried
        self.assertEqual(len(session.calls), 2)
        client, session, sleeps = make_client([FakeResponse(503, None)])
        with self.assertRaises(mc.MoliniaError):
            client.execute("create table t as select 1")
        self.assertEqual(len(session.calls), 1)
        client, session, sleeps = make_client([ConnectionError("reset"), FakeResponse(200, OK)])
        client.execute("drop schema if exists staging cascade", idempotent=True)
        self.assertEqual(len(session.calls), 2)

    def test_one_statement_per_request(self):
        client, session, _ = make_client([])
        with self.assertRaises(ValueError):
            client.execute("select 1; drop table t")
        self.assertEqual(session.calls, [])

    def test_ingest_body(self):
        resp = {"rowCount": 20000, "durationMs": 1234, "phases": {}, "uri": "s3://b/k", "format": "parquet",
                "ingestMode": "inferred"}
        client, session, _ = make_client([FakeResponse(429, None, {"Retry-After": "2"}), FakeResponse(201, resp)])
        res = client.ingest(1, "raw_orders", "acme-snowflake-export/raw/orders.parquet", location_id=1)
        self.assertEqual(res["rowCount"], 20000)
        call = session.calls[-1]
        self.assertEqual(call["url"], "https://api.example.test/api/orgs/org_test/datasources/1/ingest")
        self.assertEqual(call["json"], {"targetTable": "raw_orders",
                                        "filePath": "acme-snowflake-export/raw/orders.parquet",
                                        "locationId": 1, "fileFormat": "parquet"})


class PacerTests(unittest.TestCase):
    def test_sliding_window(self):
        now = [1000.0]
        slept = []

        def sleep(s):
            slept.append(s)
            now[0] += s

        p = mc.Pacer({"query": 3}, window=60, clock=lambda: now[0], sleep=sleep, log=lambda m: None)
        for _ in range(3):
            self.assertEqual(p.acquire("query"), 0.0)
            now[0] += 1
        waited = p.acquire("query")  # 4th inside the window: waits until the first slot expires
        self.assertGreater(waited, 56)
        self.assertLessEqual(now[0] - 1000.0, 61)
        self.assertEqual(p.acquire("http"), 0.0)  # no limit configured for http

    def test_state_file_is_shared_between_instances(self):
        now = [5000.0]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pace.json"
            a = mc.Pacer({"query": 2}, state_path=path, clock=lambda: now[0], sleep=lambda s: None,
                         log=lambda m: None)
            b = mc.Pacer({"query": 2}, state_path=path, clock=lambda: now[0], sleep=lambda s: None,
                         log=lambda m: None)
            a.acquire("query")
            a.acquire("query")
            slept = []

            def sleep(s):
                slept.append(s)
                now[0] += s

            b.sleep = sleep
            b.acquire("query")
            self.assertEqual(len(slept), 1)
            self.assertGreater(slept[0], 59)

    def test_client_paces_queries_and_requests(self):
        now = [0.0]
        order = []

        def sleep(s):
            now[0] += s

        pacer = mc.Pacer({"query": 2, "http": 100}, clock=lambda: now[0], sleep=sleep, log=lambda m: None)
        session = FakeSession([FakeResponse(200, OK)] * 3)
        client = mc.MoliniaClient("https://x.test", "o", KEY, session=session, pacer=pacer,
                                  sleep=sleep, log=lambda m: None)
        for _ in range(3):
            client.execute("select 1")
            order.append(now[0])
        self.assertEqual(order[:2], [0.0, 0.0])
        self.assertGreaterEqual(order[2], 60.0)


# =========================================================================== molinia tool

class FakeClient:
    def __init__(self, handler=None):
        self.sql = []
        self.handler = handler or (lambda sql: {"columns": [], "rows": [], "rowCount": 0})

    def execute(self, sql, idempotent=None):
        self.sql.append(sql)
        return self.handler(sql)


class MoliniaToolTests(unittest.TestCase):
    def test_reset_never_touches_main(self):
        for bad in ("main", "MAIN", "information_schema", "temp", "pg_catalog"):
            with self.assertRaises(ValueError):
                molinia.drop_schema_sql(bad)
        with self.assertRaises(ValueError):
            molinia.drop_schema_sql('staging"; drop schema main')
        self.assertEqual(molinia.drop_schema_sql("staging"), 'DROP SCHEMA IF EXISTS "staging" CASCADE')

        fc = FakeClient()
        code, out, _ = run_main(lambda a: molinia.cmd_reset(mock.Mock(), client=fc), None)
        self.assertEqual(code, 0)
        drops = [s for s in fc.sql if s.upper().startswith("DROP")]
        self.assertEqual(drops, ['DROP SCHEMA IF EXISTS "staging" CASCADE',
                                 'DROP SCHEMA IF EXISTS "intermediate" CASCADE',
                                 'DROP SCHEMA IF EXISTS "marts" CASCADE'])
        self.assertFalse(any('"main"' in s for s in fc.sql))
        self.assertIn("verified", out)

    def test_reset_reports_leftover_schema(self):
        def handler(sql):
            if "schemata" in sql:
                return {"columns": ["catalog_name", "schema_name"], "rows": [["__rd_lake", "marts"]]}
            return {"columns": [], "rows": []}

        code, out, _ = run_main(lambda a: molinia.cmd_reset(mock.Mock(), client=FakeClient(handler)), None)
        self.assertEqual(code, 1)
        self.assertIn("STILL THERE", out)

    def test_ingest_plan(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "raw").mkdir()
            (root / "expected").mkdir()
            import pyarrow as pa
            import pyarrow.parquet as pq
            pq.write_table(pa.table({"ORDER_ID": [1, 2, 3]}), root / "raw" / "orders.parquet")
            pq.write_table(pa.table({"ORDER_ID": [1]}), root / "expected" / "fct_orders.parquet")
            plan = molinia.plan_ingest(root)
            self.assertEqual([(p["target"], p["file_path"], p["local_rows"]) for p in plan], [
                ("raw_orders", "acme-snowflake-export/raw/orders.parquet", 3),
                ("sf_fct_orders", "acme-snowflake-export/expected/fct_orders.parquet", 1)])
            self.assertEqual([p["target"] for p in molinia.plan_ingest(root, only="expected")], ["sf_fct_orders"])
            self.assertEqual([p["target"] for p in molinia.plan_ingest(root, tables=["orders"])], ["raw_orders"])

    def test_status_uses_two_queries(self):
        def handler(sql):
            if "information_schema.tables" in sql:
                return {"columns": ["table_catalog", "table_schema", "table_name", "table_type", "current_db",
                                    "search_path"],
                        "rows": [["org-1", "main", "raw_orders", "BASE TABLE", "org-1", '"org-1",__rd_lake'],
                                 ["temp", "main", "raw_customer_contacts", "VIEW", "org-1", '"org-1",__rd_lake'],
                                 ["org-1", "main", "raw_customer_contacts", "BASE TABLE", "org-1", ""],
                                 ["org-1", "marts", "fct_orders", "BASE TABLE", "org-1", ""]]}
            return {"columns": ["table_schema", "table_name", "row_count"],
                    "rows": [["main", "raw_customer_contacts", 2000], ["main", "raw_orders", "20000"],
                             ["marts", "fct_orders", 19000]]}

        fc = FakeClient(handler)
        rels = molinia.gather_status(fc)
        self.assertEqual(len(fc.sql), 2)
        self.assertEqual([(r["schema"], r["name"], r["rows"]) for r in rels], [
            ("main", "raw_customer_contacts", 2000), ("main", "raw_orders", 20000), ("marts", "fct_orders", 19000)])
        self.assertIn("UNION ALL", fc.sql[1])


# =========================================================================== env + minio

class EnvTests(unittest.TestCase):
    def test_load_and_require(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {}, clear=False):
            for k in ("KIT_TEST_A", "KIT_TEST_B", "KIT_TEST_C"):
                os.environ.pop(k, None)
            p = Path(d)
            (p / "one.env").write_text('# c\nexport KIT_TEST_A="val_q7_secret"\nKIT_TEST_B=PASTE_HERE\n')
            (p / "two.env.example").write_text("KIT_TEST_C=from_example\n")
            loaded = _env.load_env(p)
            self.assertEqual(sorted(loaded), ["KIT_TEST_A", "KIT_TEST_B"])
            self.assertNotIn("KIT_TEST_C", os.environ)  # *.env.example is never loaded
            self.assertEqual(_env.require("KIT_TEST_A", secrets_dir=p), {"KIT_TEST_A": "val_q7_secret"})
            with self.assertRaises(_env.EnvError) as cm:
                _env.require("KIT_TEST_A", "KIT_TEST_B", "KIT_TEST_C", secrets_dir=p)
            msg = str(cm.exception)
            self.assertIn("KIT_TEST_B (still the PASTE_HERE placeholder", msg)
            self.assertIn("KIT_TEST_C (not set", msg)
            self.assertNotIn("val_q7_secret", msg)

    def test_existing_environment_wins(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"KIT_TEST_D": "shell"}):
            (Path(d) / "x.env").write_text("KIT_TEST_D=file\n")
            _env.load_env(Path(d))
            self.assertEqual(os.environ["KIT_TEST_D"], "shell")


class MinioUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="minio-test-"))
        self.exports = self.tmp / "exports"
        (self.exports / "acme-snowflake-export" / "raw").mkdir(parents=True)
        (self.exports / "acme-snowflake-export" / "raw" / "orders.parquet").write_bytes(b"PAR1data")
        (self.exports / "acme-snowflake-export" / "raw" / ".DS_Store").write_bytes(b"junk")
        self.secrets = self.tmp / "secrets"
        self.secrets.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_without_minio_env_prints_manual_steps_and_exits_2(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            code, out, _ = run_main(minio_upload.main, ["--dry-run"], secrets_dir=self.secrets,
                                    exports_dir=self.exports)
        self.assertEqual(code, 2)
        self.assertIn("Upload by hand", out)
        self.assertIn("acme-snowflake-export/raw/orders.parquet", out)
        self.assertNotIn(".DS_Store", out)

    def test_uploads_to_same_keys(self):
        (self.secrets / "minio.env").write_text(
            "MINIO_ENDPOINT=https://minio.example.test\nMINIO_BUCKET=bkt\n"
            "MINIO_ACCESS_KEY=ak\nMINIO_SECRET_KEY=sk_secret_value\n")
        uploaded = []

        class FakeS3:
            def upload_file(self, filename, bucket, key, ExtraArgs=None):
                uploaded.append((Path(filename).name, bucket, key))

            def head_object(self, Bucket, Key):
                return {"ContentLength": 8}

        seen_cfg = {}

        def factory(v):
            seen_cfg.update(v)
            return FakeS3()

        names = ("MINIO_ENDPOINT", "MINIO_BUCKET", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY")
        with mock.patch.dict(os.environ, {}, clear=False):
            for n in names:
                os.environ.pop(n, None)
            code, out, _ = run_main(minio_upload.main, [], secrets_dir=self.secrets, exports_dir=self.exports,
                                    client_factory=factory)
        self.assertEqual(code, 0, out)
        self.assertEqual(uploaded, [("orders.parquet", "bkt", "acme-snowflake-export/raw/orders.parquet")])
        self.assertEqual(seen_cfg["MINIO_ENDPOINT"], "https://minio.example.test")
        self.assertNotIn("sk_secret_value", out)


if __name__ == "__main__":
    unittest.main()
