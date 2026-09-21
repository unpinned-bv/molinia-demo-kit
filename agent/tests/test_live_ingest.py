"""Unit tests for the live-ingest opening: `molinia.py ingest` planned from the
bucket, and the presenter-only `molinia.py empty`.

The demo now starts from an empty org and ingests on stage, so two properties
carry the risk. Ingest must not need the unmasked Parquet inside the kit (rule
H11), which is why it plans from the bucket. And `empty` must never reach a
table that is not an ingested input, because it runs against the same org the
client's governance lives in.

Local only: a fake S3 client and a fake Molinia client. No MinIO, no Molinia,
and nothing under the real .secrets/ is read (load_env is patched out).

Run: .venv/bin/python -m unittest discover -s agent/tests -v   (or `make test`)
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "tools"))

import molinia  # noqa: E402

FAKE_MINIO = {"MINIO_ENDPOINT": "https://minio.invalid", "MINIO_BUCKET": "test-bucket",
              "MINIO_ACCESS_KEY": "test-access", "MINIO_SECRET_KEY": "test-secret-never-printed"}
PREFIX = molinia.EXPORT_PREFIX


def parquet_bytes(n_rows: int) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq
    buf = io.BytesIO()
    pq.write_table(pa.table({"ID": list(range(n_rows))}), buf)
    return buf.getvalue()


class FakeS3:
    """list_objects_v2 paginator + get_object over an in-memory {key: bytes}."""

    def __init__(self, objects):
        self.objects = dict(objects)
        self.listed = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix):
                outer.listed.append((Bucket, Prefix))
                hits = [{"Key": k, "Size": len(v)} for k, v in sorted(outer.objects.items())
                        if k.startswith(Prefix)]
                return [{"Contents": hits}] if hits else [{}]
        return _P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}


class FakeClient:
    """Molinia client double: records SQL and ingest calls."""

    def __init__(self, handler=None, ingest_rows=None):
        self.sql = []
        self.ingests = []
        self.handler = handler or (lambda sql: {"columns": [], "rows": []})
        self.ingest_rows = ingest_rows or {}

    def execute(self, sql, idempotent=None):
        self.sql.append(sql)
        return self.handler(sql)

    def ingest(self, data_source_id, target_table, file_path, *, location_id=None, file_format=None):
        self.ingests.append((data_source_id, target_table, file_path, location_id))
        return {"rowCount": self.ingest_rows.get(target_table, 0), "durationMs": 100}


def run(fn):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fn()
    return code, out.getvalue(), err.getvalue()


def ingest_args(**kw):
    base = dict(only=None, table=None, dry_run=False, data_source=1, location=1, source="bucket")
    base.update(kw)
    return argparse.Namespace(**base)


def export_objects():
    return {
        f"{PREFIX}/raw/orders.parquet": parquet_bytes(3),
        f"{PREFIX}/raw/customers.parquet": parquet_bytes(2),
        f"{PREFIX}/expected/fct_orders.parquet": parquet_bytes(1),
        # not part of the export layout: nested, or not Parquet
        f"{PREFIX}/raw/archive/old.parquet": parquet_bytes(9),
        f"{PREFIX}/raw/README.txt": b"hello",
        "some-other-prefix/raw/orders.parquet": parquet_bytes(5),
    }


@contextlib.contextmanager
def fake_bucket_env():
    """MinIO settings for the test, and no reading of the real .secrets/."""
    with mock.patch.dict(os.environ, FAKE_MINIO), mock.patch.object(molinia._env, "load_env"):
        yield


# =========================================================================== ingest from the bucket

class BucketPlanTests(unittest.TestCase):
    def test_plan_lists_only_the_export_layout(self):
        plan = molinia.plan_ingest_bucket(FakeS3(export_objects()), "b")
        self.assertEqual(
            [(p["target"], p["file_path"], p["local_rows"], p["source"]) for p in plan],
            [("raw_customers", f"{PREFIX}/raw/customers.parquet", 2, "bucket"),
             ("raw_orders", f"{PREFIX}/raw/orders.parquet", 3, "bucket"),
             ("sf_fct_orders", f"{PREFIX}/expected/fct_orders.parquet", 1, "bucket")])

    def test_only_and_table_filters(self):
        s3 = FakeS3(export_objects())
        self.assertEqual([p["target"] for p in molinia.plan_ingest_bucket(s3, "b", only="expected")],
                         ["sf_fct_orders"])
        self.assertEqual([p["target"] for p in molinia.plan_ingest_bucket(s3, "b", only="raw")],
                         ["raw_customers", "raw_orders"])
        self.assertEqual([p["target"] for p in molinia.plan_ingest_bucket(s3, "b", tables=["orders"])],
                         ["raw_orders"])
        self.assertEqual([p["target"] for p in molinia.plan_ingest_bucket(s3, "b", tables=["sf_fct_orders"])],
                         ["sf_fct_orders"])

    def test_bucket_plan_equals_the_local_plan_for_the_same_files(self):
        """The two planners must agree, or switching the default changed behaviour."""
        objects = {k: v for k, v in export_objects().items()
                   if k.count("/") == 2 and k.startswith(PREFIX) and k.endswith(".parquet")}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for key, data in objects.items():
                path = root / key.split("/", 1)[1]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            local = molinia.plan_ingest(root)
        bucket = molinia.plan_ingest_bucket(FakeS3(objects), "b")
        pick = lambda plan: [(p["kind"], p["target"], p["file_path"], p["local_rows"]) for p in plan]
        self.assertEqual(pick(bucket), pick(local))

    def test_unreadable_object_gives_no_row_count_rather_than_crashing(self):
        s3 = FakeS3({f"{PREFIX}/raw/orders.parquet": b"not parquet"})
        plan = molinia.plan_ingest_bucket(s3, "b")
        self.assertEqual(len(plan), 1)
        self.assertIsNone(plan[0]["local_rows"])


class IngestCommandTests(unittest.TestCase):
    def test_ingests_the_bucket_keys_and_checks_rows(self):
        s3 = FakeS3(export_objects())
        fc = FakeClient(ingest_rows={"raw_customers": 2, "raw_orders": 3, "sf_fct_orders": 1})
        with fake_bucket_env():
            code, out, _ = run(lambda: molinia.cmd_ingest(ingest_args(), client=fc, s3=s3))
        self.assertEqual(code, 0, out)
        self.assertEqual([c[2] for c in fc.ingests], [f"{PREFIX}/raw/customers.parquet",
                                                      f"{PREFIX}/raw/orders.parquet",
                                                      f"{PREFIX}/expected/fct_orders.parquet"])
        self.assertTrue(all(c[0] == 1 and c[3] == 1 for c in fc.ingests))
        self.assertIn("3/3 ingested", out)
        self.assertIn("main.raw_orders", out)
        self.assertIn("(bucket 3)", out)
        self.assertNotIn(FAKE_MINIO["MINIO_SECRET_KEY"], out)

    def test_only_raw_is_the_first_beat_of_the_opening(self):
        s3 = FakeS3(export_objects())
        fc = FakeClient(ingest_rows={"raw_customers": 2, "raw_orders": 3})
        with fake_bucket_env():
            code, out, _ = run(lambda: molinia.cmd_ingest(ingest_args(only="raw"), client=fc, s3=s3))
        self.assertEqual(code, 0)
        self.assertEqual([c[1] for c in fc.ingests], ["raw_customers", "raw_orders"])
        self.assertIn("2/2 ingested", out)

    def test_row_mismatch_fails(self):
        s3 = FakeS3({f"{PREFIX}/raw/orders.parquet": parquet_bytes(3)})
        fc = FakeClient(ingest_rows={"raw_orders": 2})
        with fake_bucket_env():
            code, out, _ = run(lambda: molinia.cmd_ingest(ingest_args(), client=fc, s3=s3))
        self.assertEqual(code, 1)
        self.assertIn("ROWS", out)

    def test_dry_run_ingests_nothing(self):
        s3 = FakeS3(export_objects())
        fc = FakeClient()
        with fake_bucket_env():
            code, out, _ = run(lambda: molinia.cmd_ingest(ingest_args(dry_run=True), client=fc, s3=s3))
        self.assertEqual(code, 0)
        self.assertEqual(fc.ingests, [])
        self.assertIn("planned from s3://test-bucket/", out)
        self.assertIn("dry run: 3 ingests", out)

    def test_empty_bucket_is_an_error_that_says_what_to_run(self):
        fc = FakeClient()
        with fake_bucket_env():
            code, _, err = run(lambda: molinia.cmd_ingest(ingest_args(), client=fc, s3=FakeS3({})))
        self.assertEqual(code, 1)
        self.assertIn("make minio-upload", err)
        self.assertEqual(fc.ingests, [])

    def test_missing_bucket_settings_name_the_variable(self):
        env = {k: v for k, v in FAKE_MINIO.items() if k != "MINIO_BUCKET"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(molinia._env, "load_env"):
            os.environ.pop("MINIO_BUCKET", None)
            with self.assertRaises(molinia._env.EnvError) as ctx:
                molinia.cmd_ingest(ingest_args(), client=FakeClient(), s3=FakeS3({}))
        self.assertIn("MINIO_BUCKET", str(ctx.exception))

    def test_local_mode_still_plans_from_exports(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "raw").mkdir()
            (root / "raw" / "orders.parquet").write_bytes(parquet_bytes(3))
            fc = FakeClient(ingest_rows={"raw_orders": 3})
            with mock.patch.object(molinia, "EXPORT_ROOT", root):
                code, out, _ = run(lambda: molinia.cmd_ingest(ingest_args(source="local"), client=fc))
        self.assertEqual(code, 0, out)
        self.assertIn("(local 3)", out)

    def test_refuses_a_source_schema_the_ingest_api_cannot_honour(self):
        with mock.patch.object(molinia, "SOURCE_SCHEMA", "landing"):
            with self.assertRaises(ValueError) as ctx:
                molinia.cmd_ingest(ingest_args(), client=FakeClient(), s3=FakeS3({}))
        self.assertIn("no schema parameter", str(ctx.exception))


# =========================================================================== empty

def list_rows(*names, catalog="org-1", schema="main", ttype="BASE TABLE"):
    return [[catalog, schema, n, ttype] for n in names]


class DropGuardTests(unittest.TestCase):
    def test_only_prefixed_inputs_can_be_dropped(self):
        self.assertEqual(molinia.drop_input_sql("raw_orders"), 'DROP TABLE IF EXISTS "main"."raw_orders"')
        self.assertEqual(molinia.drop_input_sql("sf_fct_orders"),
                         'DROP TABLE IF EXISTS "main"."sf_fct_orders"')
        self.assertEqual(molinia.drop_input_sql("raw_v", "VIEW"), 'DROP VIEW IF EXISTS "main"."raw_v"')
        for bad in ("orders", "stg_orders", "fct_orders", "customers_backup", "rawx"):
            with self.assertRaises(ValueError, msg=bad):
                molinia.drop_input_sql(bad)

    def test_injection_and_odd_kinds_are_refused(self):
        with self.assertRaises(ValueError):
            molinia.drop_input_sql('raw_x"; DROP SCHEMA main; --')
        with self.assertRaises(ValueError):
            molinia.drop_input_sql("raw_x", "SCHEMA")

    def test_plan_skips_masking_views_other_schemas_and_unprefixed_tables(self):
        rows = (list_rows("raw_customer_contacts", catalog="temp", ttype="VIEW")      # masking view
                + list_rows("raw_customer_contacts", catalog="__rd_lake")             # the table it masks
                + list_rows("raw_orders", catalog="__rd_lake")                        # lake copy
                + list_rows("raw_orders")                                             # org-file copy
                + list_rows("customers_backup")                                       # a client table
                + list_rows("raw_x", schema="staging", ttype="VIEW"))                  # dbt output
        self.assertEqual(molinia.plan_empty(rows), [("raw_orders", "TABLE")])
        self.assertEqual(molinia.masked_names(rows), ["raw_customer_contacts"])

    def test_the_masked_table_is_kept_exactly_as_measured_live(self):
        """2026-09-21 on dev: information_schema returned these two rows, and the
        build key's DROP failed with 'is of type View, trying to drop type Table'."""
        rows = [["__rd_lake", "main", "raw_customer_contacts", "BASE TABLE"],
                ["temp", "main", "raw_customer_contacts", "VIEW"]]
        self.assertEqual(molinia.masked_names(rows), ["raw_customer_contacts"])
        self.assertEqual(molinia.plan_empty(rows), [])

    def test_a_view_elsewhere_does_not_mark_a_table_masked(self):
        rows = (list_rows("raw_orders", catalog="__rd_lake")
                + list_rows("raw_orders", catalog="temp", schema="staging", ttype="VIEW"))
        self.assertEqual(molinia.masked_names(rows), [])
        self.assertEqual(molinia.plan_empty(rows), [("raw_orders", "TABLE")])

    def test_list_query_is_scoped_to_the_source_schema_and_both_prefixes(self):
        sql = molinia.empty_list_sql()
        self.assertIn("lower(table_schema) = 'main'", sql)
        self.assertIn("starts_with(lower(table_name), 'raw_')", sql)
        self.assertIn("starts_with(lower(table_name), 'sf_')", sql)
        self.assertNotIn("staging", sql)


class EmptyCommandTests(unittest.TestCase):
    def stateful(self, before, after):
        state = {"dropped": False}

        def handler(sql):
            if sql.upper().startswith("DROP"):
                state["dropped"] = True
                return {"columns": [], "rows": []}
            return {"columns": ["table_catalog", "table_schema", "table_name", "table_type"],
                    "rows": after if state["dropped"] else before}
        return FakeClient(handler)

    def test_without_yes_it_lists_and_drops_nothing(self):
        fc = self.stateful(list_rows("raw_orders", "sf_fct_orders"), [])
        code, out, _ = run(lambda: molinia.cmd_empty(argparse.Namespace(yes=False), client=fc))
        self.assertEqual(code, 0)
        self.assertFalse(any(s.upper().startswith("DROP") for s in fc.sql))
        self.assertIn("dry run", out)
        self.assertIn("raw_orders", out)

    def test_with_yes_it_drops_exactly_the_inputs_and_verifies(self):
        masked = (list_rows("raw_customer_contacts", catalog="__rd_lake")
                  + list_rows("raw_customer_contacts", catalog="temp", ttype="VIEW"))
        fc = self.stateful(list_rows("raw_orders", "sf_fct_orders") + masked, masked)
        code, out, _ = run(lambda: molinia.cmd_empty(argparse.Namespace(yes=True), client=fc))
        self.assertEqual(code, 0, out)
        drops = [s for s in fc.sql if s.upper().startswith("DROP")]
        self.assertEqual(drops, ['DROP TABLE IF EXISTS "main"."raw_orders"',
                                 'DROP TABLE IF EXISTS "main"."sf_fct_orders"'])
        self.assertFalse(any("raw_customer_contacts" in s for s in drops))
        self.assertFalse(any("SCHEMA" in s.upper() for s in drops))
        self.assertIn("kept: main.raw_customer_contacts (masked", out)
        self.assertIn("verified: no other raw_* / sf_* tables left in main; kept 1 masked", out)
        self.assertIn("staging / intermediate / marts untouched", out)

    def test_only_masked_tables_left_is_success_not_failure(self):
        masked = (list_rows("raw_customer_contacts", catalog="__rd_lake")
                  + list_rows("raw_customer_contacts", catalog="temp", ttype="VIEW"))
        fc = self.stateful(masked, masked)
        code, out, _ = run(lambda: molinia.cmd_empty(argparse.Namespace(yes=True), client=fc))
        self.assertEqual(code, 0, out)
        self.assertIn("nothing else to drop", out)
        self.assertFalse(any(s.upper().startswith("DROP") for s in fc.sql))

    def test_leftover_after_drop_is_reported_and_fails(self):
        fc = self.stateful(list_rows("raw_orders"), list_rows("raw_orders", catalog="__rd_lake"))
        code, out, _ = run(lambda: molinia.cmd_empty(argparse.Namespace(yes=True), client=fc))
        self.assertEqual(code, 1)
        self.assertIn("STILL THERE", out)

    def test_nothing_to_drop(self):
        fc = self.stateful([], [])
        code, out, _ = run(lambda: molinia.cmd_empty(argparse.Namespace(yes=True), client=fc))
        self.assertEqual(code, 0)
        self.assertIn("nothing to drop", out)
        self.assertFalse(any(s.upper().startswith("DROP") for s in fc.sql))

    def test_refuses_a_prefix_without_a_separator(self):
        loose = dataclasses.replace(molinia.ENGAGEMENT, raw_prefix="raw")
        with mock.patch.object(molinia, "ENGAGEMENT", loose):
            with self.assertRaises(ValueError) as ctx:
                molinia.cmd_empty(argparse.Namespace(yes=True), client=FakeClient())
        self.assertIn("does not end in '_'", str(ctx.exception))


# =========================================================================== the agent may not run it

class AgentProhibitionTests(unittest.TestCase):
    """`empty` drops the answer key. The agent's instructions must forbid it by name."""

    def test_rule_h6_names_empty(self):
        h6 = next(line for line in (KIT / "agent" / "MIGRATION_RULES.md").read_text().splitlines()
                  if line.startswith("| H6 |"))
        self.assertIn("empty", h6)
        self.assertIn("make molinia-*", h6)

    def test_claude_md_names_empty(self):
        guard = next(line for line in (KIT / "CLAUDE.md").read_text().splitlines()
                     if line.startswith("- **Data in `main`.**"))
        self.assertIn("empty", guard)

    def test_every_live_ingest_target_is_under_molinia_star(self):
        text = (KIT / "Makefile").read_text()
        for target in ("molinia-empty:", "molinia-ingest:", "molinia-ingest-raw:", "molinia-ingest-expected:"):
            self.assertIn(target, text)
        self.assertIn("tools/molinia.py empty --yes", text)
        self.assertIn("tools/molinia.py ingest --only raw", text)
        self.assertIn("tools/molinia.py ingest --only expected", text)


if __name__ == "__main__":
    unittest.main()
