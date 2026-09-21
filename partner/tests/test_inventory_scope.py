"""The --schemas filter has to reach every probe, not just the six that can
carry it in SQL.

Offline: no Snowflake connection, only the probe table and the row filter.
Run:
    .venv/bin/python -m unittest discover -s partner/tests -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "partner"))

from assesslib import snowflake_inventory as sfi          # noqa: E402


class ProbeScopeTests(unittest.TestCase):

    def test_every_probe_declares_a_scope(self):
        for p in sfi.build_probes("DB", [], 20):
            with self.subTest(probe=p.name):
                self.assertIn(p.scope, ("schema", "account"))

    def test_only_account_level_objects_are_exempt_from_the_schema_filter(self):
        exempt = {p.name for p in sfi.build_probes("DB", [], 20) if p.scope == "account"}
        self.assertEqual({"shares", "warehouses",
                          "query_history_by_type", "query_history_clients"}, exempt)

    def test_show_probes_are_filtered_on_their_rows(self):
        """SHOW ... IN DATABASE has no schema clause, so the rows carry the scope.

        Before this, `--schemas RAW` still counted the tasks, streams, stages,
        policies and external tables of every other schema in the database, and
        the report header claimed it was scoped to RAW."""
        rows = [{"schema_name": "RAW", "name": "t1"},
                {"schema_name": "ANALYTICS_MARTS", "name": "t2"}]
        self.assertEqual(["t1"],
                         [r["name"] for r in sfi.filter_rows_to_schemas(rows, ["RAW"])])

    def test_the_filter_is_case_insensitive_and_accepts_every_schema_spelling(self):
        for key in sfi.SCHEMA_KEYS:
            with self.subTest(key=key):
                rows = [{key: "raw", "n": 1}, {key: "OTHER", "n": 2}]
                self.assertEqual([1], [r["n"] for r in sfi.filter_rows_to_schemas(rows, ["RAW"])])

    def test_no_filter_means_no_filtering(self):
        rows = [{"schema_name": "A"}, {"schema_name": "B"}]
        self.assertEqual(rows, sfi.filter_rows_to_schemas(rows, []))

    def test_a_row_with_no_schema_column_is_kept_not_silently_dropped(self):
        """Dropping it would hide an object; the report says what could not be scoped."""
        rows = [{"name": "mystery"}]
        self.assertEqual(rows, sfi.filter_rows_to_schemas(rows, ["RAW"]))


class ReadOnlyTests(unittest.TestCase):

    def test_no_probe_is_a_write(self):
        for p in sfi.build_probes("DB", ["RAW"], 20):
            for sql in (p.sql, p.fallback_sql):
                if sql:
                    with self.subTest(probe=p.name):
                        self.assertIsNone(sfi._WRITE.match(sql))
                        self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "SHOW")))

    def test_identifiers_and_literals_are_quoted_against_injection(self):
        sql = " ".join(p.sql for p in sfi.build_probes('D"B', ["A'B"], 0))
        self.assertIn('"D""B"', sql)
        self.assertIn("'A''B'", sql)

    def test_errors_name_the_missing_setting_never_its_value(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in
                 ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_ROLE",
                  "SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_PRIVATE_KEY_PATH")}
        try:
            os.environ["SNOWFLAKE_ACCOUNT"] = "s3cr3t-account-value"
            with self.assertRaises(sfi.SnowflakeError) as ctx:
                sfi.connect("DB")
            self.assertNotIn("s3cr3t", str(ctx.exception))
            self.assertIn("SNOWFLAKE_USER", str(ctx.exception))
        finally:
            os.environ.pop("SNOWFLAKE_ACCOUNT", None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
