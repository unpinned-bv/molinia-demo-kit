"""Unit tests for the classifier and the plans built on top of it.

Offline: synthetic inventory rows, no Snowflake and no Molinia. Run:
    .venv/bin/python -m unittest discover -s partner/tests -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "partner"))

from assesslib import classify as C, plan, scan          # noqa: E402
from assesslib.facts import FACTS                        # noqa: E402


def scan_sql(text, label):
    return scan.scan_text(text, label, "sql")


class ClassOrderTests(unittest.TestCase):

    def test_worst_picks_the_most_expensive(self):
        self.assertEqual(C.AUTOMATIC, C.worst())
        self.assertEqual(C.AUTOMATIC, C.worst(C.AUTOMATIC, C.AUTOMATIC))
        self.assertEqual(C.REDESIGN, C.worst(C.AUTOMATIC, C.REDESIGN, C.AGENT_PLUS_REVIEW))
        self.assertEqual(C.NOT_SUPPORTED_TODAY,
                         C.worst(C.REDESIGN, C.NOT_SUPPORTED_TODAY))
        self.assertEqual(C.AGENT_PLUS_REVIEW, C.worst(None, C.AGENT_PLUS_REVIEW))

    def test_an_unknown_class_is_an_error_not_a_silent_pass(self):
        with self.assertRaises(ValueError):
            C.worst("PROBABLY_FINE")

    def test_redesign_is_cheaper_than_not_supported(self):
        """The two are different money, so the ordering must not drift."""
        self.assertLess(C.severity(C.REDESIGN), C.severity(C.NOT_SUPPORTED_TODAY))


class TypeMapTests(unittest.TestCase):

    def test_numbers_and_text_are_automatic(self):
        for t in ("NUMBER", "NUMBER(38,0)", "TEXT", "BOOLEAN", "DATE", "FLOAT"):
            self.assertEqual(C.AUTOMATIC, C.map_type(t)[1], t)

    def test_semi_structured_needs_review_and_cites_J0(self):
        molinia, klass, basis, rule, _note = C.map_type("VARIANT")
        self.assertEqual(C.AGENT_PLUS_REVIEW, klass)
        self.assertEqual("J0", rule)
        self.assertIn("JSON", molinia)

    def test_session_zone_timestamps_need_review(self):
        self.assertEqual(C.AGENT_PLUS_REVIEW, C.map_type("TIMESTAMP_LTZ")[1])
        self.assertEqual(C.AUTOMATIC, C.map_type("TIMESTAMP_NTZ")[1])

    def test_geospatial_is_not_supported_and_says_it_is_an_inference(self):
        _m, klass, basis, _r, _n = C.map_type("GEOGRAPHY")
        self.assertEqual(C.NOT_SUPPORTED_TODAY, klass)
        self.assertEqual(C.INFERENCE, basis)

    def test_an_unknown_type_is_flagged_rather_than_assumed_fine(self):
        _m, klass, basis, _r, note = C.map_type("SOMETHING_NEW")
        self.assertEqual(C.AGENT_PLUS_REVIEW, klass)
        self.assertEqual(C.INFERENCE, basis)
        self.assertIn("by hand", note)


class RoutineTests(unittest.TestCase):

    def test_python_and_javascript_routines_are_a_redesign(self):
        """REDESIGN, not NOT_SUPPORTED_TODAY.

        classify.py's own boundary is: REDESIGN means the work lands on Molinia
        after a human rewrites it; NOT_SUPPORTED_TODAY means it does not land at
        all. A Python UDF's logic normally does land, rewritten as SQL — which is
        also the class the adjacent dbt Python model gets, and what the verified
        facts say. Calling it NOT_SUPPORTED_TODAY told the client the work was
        undeliverable when it is merely expensive."""
        for lang in ("PYTHON", "JAVASCRIPT", "JAVA", "SCALA"):
            item = C.classify_routine("procedure", "S.P()", lang, "whatever")
            self.assertEqual(C.REDESIGN, item.klass, lang)
            self.assertEqual("F-NOTPORT", item.rule)

    def test_a_python_routine_and_a_python_dbt_model_get_the_same_class(self):
        """The two used to disagree, which is how the inconsistency hid."""
        node = scan.Node("model", "m", "models/m.py", language="python")
        proj = scan.Project(root=Path("."), ok=True, nodes=[node])
        scan._classify_nodes(proj)
        self.assertEqual(C.classify_routine("function", "S.F()", "PYTHON", None).klass,
                         node.klass)

    def test_procedural_sql_is_a_redesign(self):
        item = C.classify_routine("procedure", "S.P()", "SQL",
                                  "BEGIN\n LET x := 1;\n RETURN x;\nEND")
        self.assertEqual(C.REDESIGN, item.klass)

    def test_single_statement_sql_procedure_is_agent_plus_review(self):
        item = C.classify_routine("procedure", "S.P()", "SQL",
                                  "INSERT INTO t SELECT * FROM s")
        self.assertEqual(C.AGENT_PLUS_REVIEW, item.klass)

    def test_an_unreadable_body_is_assumed_procedural_and_says_so(self):
        item = C.classify_routine("procedure", "S.P()", "SQL", None)
        self.assertEqual(C.REDESIGN, item.klass)
        self.assertIn("not readable", item.reason)

    def test_sql_function_is_marked_an_inference(self):
        item = C.classify_routine("function", "S.F()", "SQL", None)
        self.assertEqual(C.AGENT_PLUS_REVIEW, item.klass)
        self.assertEqual(C.INFERENCE, item.basis)


class InventoryClassificationTests(unittest.TestCase):

    def rows(self, **over):
        base = {
            "tables": [
                {"table_schema": "RAW", "table_name": "ORDERS", "table_type": "BASE TABLE",
                 "row_count": 20000, "bytes": 1_000_000},
                {"table_schema": "RAW", "table_name": "GEO", "table_type": "BASE TABLE",
                 "row_count": 10, "bytes": 1000},
                {"table_schema": "RAW", "table_name": "V_ORDERS", "table_type": "VIEW",
                 "row_count": None, "bytes": None},
            ],
            "columns": [
                {"table_schema": "RAW", "table_name": "ORDERS", "column_name": "ORDER_ID",
                 "ordinal_position": 1, "data_type": "NUMBER"},
                {"table_schema": "RAW", "table_name": "ORDERS", "column_name": "META",
                 "ordinal_position": 2, "data_type": "VARIANT"},
                {"table_schema": "RAW", "table_name": "GEO", "column_name": "SHAPE",
                 "ordinal_position": 1, "data_type": "GEOGRAPHY"},
            ],
            "views": [{"table_schema": "RAW", "table_name": "V_ORDERS",
                       "view_definition": "create view v_orders as select iff(a,1,0) from orders",
                       "is_secure": "NO"}],
        }
        base.update(over)
        return base

    def test_a_variant_column_makes_its_table_reviewable_not_automatic(self):
        items, _types = plan.classify_inventory(self.rows(), scan_sql)
        orders = [i for i in items if i.name == "RAW.ORDERS"][0]
        self.assertEqual(C.AGENT_PLUS_REVIEW, orders.klass)
        self.assertIn("VARIANT", orders.reason)

    def test_a_geography_column_makes_its_table_not_supported(self):
        items, _types = plan.classify_inventory(self.rows(), scan_sql)
        geo = [i for i in items if i.name == "RAW.GEO"][0]
        self.assertEqual(C.NOT_SUPPORTED_TODAY, geo.klass)

    def test_a_view_is_scanned_with_the_same_rulebook(self):
        items, _types = plan.classify_inventory(self.rows(), scan_sql)
        view = [i for i in items if i.kind == "view"][0]
        self.assertEqual(C.AGENT_PLUS_REVIEW, view.klass)
        self.assertIn("S1", view.reason)

    def test_a_secure_view_becomes_a_redesign(self):
        rows = self.rows()
        rows["views"][0]["is_secure"] = "YES"
        items, _types = plan.classify_inventory(rows, scan_sql)
        view = [i for i in items if i.kind == "view"][0]
        self.assertEqual(C.REDESIGN, view.klass)

    def test_an_unreadable_view_definition_still_costs_a_review(self):
        rows = self.rows()
        rows["views"] = []
        items, _types = plan.classify_inventory(rows, scan_sql)
        view = [i for i in items if i.kind == "view"][0]
        self.assertEqual(C.AGENT_PLUS_REVIEW, view.klass)
        self.assertIn("could not be read", view.reason)

    def test_iceberg_and_external_tables_are_not_supported(self):
        rows = self.rows(iceberg_tables=[{"name": "ORDERS"}])
        items, _types = plan.classify_inventory(rows, scan_sql)
        orders = [i for i in items if i.name == "RAW.ORDERS"][0]
        self.assertEqual(C.NOT_SUPPORTED_TODAY, orders.klass)
        self.assertEqual("iceberg table", orders.kind)

    def test_a_task_that_calls_a_procedure_is_not_supported(self):
        rows = self.rows(tasks=[
            {"schema_name": "OPS", "name": "T1", "definition": "CALL ops.load()"},
            {"schema_name": "OPS", "name": "T2", "definition": "INSERT INTO t SELECT 1"},
        ])
        items, _types = plan.classify_inventory(rows, scan_sql)
        by_name = {i.name: i for i in items if i.kind == "task"}
        self.assertEqual(C.NOT_SUPPORTED_TODAY, by_name["OPS.T1"].klass)
        self.assertEqual(C.AGENT_PLUS_REVIEW, by_name["OPS.T2"].klass)

    def test_sequences_are_not_supported(self):
        rows = self.rows(sequences=[{"sequence_schema": "RAW", "sequence_name": "S1"}])
        items, _types = plan.classify_inventory(rows, scan_sql)
        self.assertEqual(C.NOT_SUPPORTED_TODAY,
                         [i for i in items if i.kind == "sequence"][0].klass)

    def test_client_shares_are_not_supported_but_snowflake_s_own_are_noise(self):
        rows = self.rows(shares=[
            {"name": "ACCOUNT_USAGE", "kind": "INBOUND", "owner_account": "SNOWFLAKE"},
            {"name": "SAMPLE_DATA", "kind": "INBOUND",
             "owner_account": "SFSALESSHARED.SFC_SAMPLES_X"},
            {"name": "VENDOR_FEED", "kind": "INBOUND", "owner_account": "ACME_DATA"},
            {"name": "TO_PARTNER", "kind": "OUTBOUND", "owner_account": ""},
        ])
        items, _types = plan.classify_inventory(rows, scan_sql)
        by_name = {i.name: i for i in items if i.kind.startswith("share")}
        self.assertEqual(C.AUTOMATIC, by_name["ACCOUNT_USAGE"].klass)
        self.assertEqual(C.AUTOMATIC, by_name["SAMPLE_DATA"].klass)
        self.assertEqual(C.NOT_SUPPORTED_TODAY, by_name["VENDOR_FEED"].klass)
        self.assertIn("ACME_DATA", by_name["VENDOR_FEED"].reason)
        self.assertEqual(C.NOT_SUPPORTED_TODAY, by_name["TO_PARTNER"].klass)
        self.assertEqual("share (outbound)", by_name["TO_PARTNER"].kind)

    def test_an_identity_column_is_named(self):
        rows = self.rows()
        rows["columns"][0]["is_identity"] = "YES"
        items, _types = plan.classify_inventory(rows, scan_sql)
        ident = [i for i in items if i.kind == "identity column"]
        self.assertEqual(1, len(ident))
        self.assertEqual(C.NOT_SUPPORTED_TODAY, ident[0].klass)

    def test_type_summary_counts_columns_and_tables(self):
        _items, types = plan.classify_inventory(self.rows(), scan_sql)
        by_type = {t["snowflake_type"]: t for t in types}
        self.assertEqual(1, by_type["VARIANT"]["columns"])
        self.assertEqual(1, by_type["VARIANT"]["tables"])
        self.assertEqual(C.AGENT_PLUS_REVIEW, by_type["VARIANT"]["class"])

    def test_a_relation_the_dbt_project_builds_is_not_counted_twice(self):
        rows = self.rows()
        rows["tables"].append({"table_schema": "ANALYTICS_MARTS", "table_name": "FCT_ORDERS",
                               "table_type": "BASE TABLE", "row_count": 20000, "bytes": 800000})
        items, _types = plan.classify_inventory(rows, scan_sql, ["fct_orders"])
        fct = [i for i in items if i.name == "ANALYTICS_MARTS.FCT_ORDERS"][0]
        self.assertEqual("table (dbt-built)", fct.kind)
        self.assertEqual(C.AUTOMATIC, fct.klass)
        self.assertIn("answer key", fct.reason)

    def test_empty_inventory_produces_no_items_and_no_crash(self):
        items, types = plan.classify_inventory({}, scan_sql)
        self.assertEqual([], items)
        self.assertEqual([], types)


class MovePlanTests(unittest.TestCase):

    TABLES = [
        {"table_schema": "RAW", "table_name": "ORDERS", "table_type": "BASE TABLE",
         "row_count": 20000, "bytes": 1_000_000},
        {"table_schema": "STAGE", "table_name": "ORDERS", "table_type": "BASE TABLE",
         "row_count": 5, "bytes": 500},
        {"table_schema": "RAW", "table_name": "LOGS", "table_type": "BASE TABLE",
         "row_count": 1, "bytes": 10},
        {"table_schema": "RAW", "table_name": "V", "table_type": "VIEW"},
    ]
    COLUMNS = [
        {"table_schema": "RAW", "table_name": "ORDERS", "column_name": "ID",
         "ordinal_position": 1, "data_type": "NUMBER"},
        {"table_schema": "RAW", "table_name": "ORDERS", "column_name": "META",
         "ordinal_position": 2, "data_type": "VARIANT"},
    ]

    def moves(self, proj=None):
        return plan.build_moves("DB", self.TABLES, self.COLUMNS, proj, 1.01,
                                "DB.PUBLIC.EXPORT", "org_1", "1", "1")

    def test_views_are_not_moved(self):
        self.assertEqual(3, len(self.moves()))

    def test_name_collisions_across_schemas_are_prefixed(self):
        targets = {m.target_table for m in self.moves()}
        self.assertIn("raw_raw_orders", targets)
        self.assertIn("raw_stage_orders", targets)
        self.assertIn("raw_logs", targets)
        note = [m for m in self.moves() if m.table == "ORDERS"][0].notes
        self.assertTrue(any("schema `main`" in n for n in note))

    def test_variant_columns_are_unloaded_with_to_json(self):
        m = [x for x in self.moves() if x.schema == "RAW" and x.table == "ORDERS"][0]
        self.assertIn('TO_JSON("META")', m.copy_sql)
        self.assertIn('"ID"', m.copy_sql)
        self.assertTrue(any("TO_JSON" in n for n in m.notes))

    def test_a_table_with_no_column_metadata_falls_back_to_star(self):
        m = [x for x in self.moves() if x.table == "LOGS"][0]
        self.assertIn("SELECT\n    *", m.copy_sql)
        self.assertTrue(any("SELECT *" in n for n in m.notes))

    def test_source_tables_come_first_then_smallest_first(self):
        proj = scan.Project(root=Path("."), ok=True)
        proj.sources = [{"name": "raw", "tables": [{"name": "logs", "identifier": "raw_logs"}]}]
        order = [(m.schema, m.table) for m in self.moves(proj)]
        self.assertEqual(("RAW", "LOGS"), order[0])
        self.assertEqual(("STAGE", "ORDERS"), order[1])

    def test_a_dbt_built_table_is_ordered_last_and_labelled(self):
        proj = scan.Project(root=Path("."), ok=True)
        proj.nodes = [scan.Node("model", "logs", "models/logs.sql")]
        moves = self.moves(proj)
        last = moves[-1]
        self.assertEqual(("RAW", "LOGS"), (last.schema, last.table))
        self.assertIn("do NOT migrate", last.why_order)
        self.assertTrue(any("rebuilds this relation" in n for n in last.notes))

    def test_a_huge_table_loses_single_true_and_says_why(self):
        big = [{"table_schema": "RAW", "table_name": "BIG", "table_type": "BASE TABLE",
                "row_count": 1, "bytes": 20 * 1024 ** 3}]
        m = plan.build_moves("DB", big, [], None, 1.0, "S", "o", "1", "1")[0]
        self.assertNotIn("SINGLE = TRUE", m.copy_sql)
        self.assertTrue(any("multi-file ingest" in n for n in m.notes))

    def test_ingest_body_matches_the_documented_wire_contract(self):
        m = self.moves()[0]
        self.assertIn("/api/orgs/org_1/datasources/1/ingest", m.ingest_body)
        self.assertIn('"targetTable"', m.ingest_body)
        self.assertIn('"fileFormat": "parquet"', m.ingest_body)


class ParityPlanTests(unittest.TestCase):

    def project(self):
        proj = scan.Project(root=Path("."), ok=True, name="demo")
        proj.target_schema = "ANALYTICS"
        keyed = scan.Node("model", "fct_orders", "models/marts/fct_orders.sql",
                          materialized="table", schema="marts")
        keyed.key, keyed.key_source = ["order_id"], "unique test on the column"
        keyless = scan.Node("model", "rpt_x", "models/marts/rpt_x.sql",
                            materialized="table", schema="marts")
        proj.nodes = [keyed, keyless]
        return proj

    def test_entries_and_keyless_models(self):
        entries, keyless = plan.build_parity(self.project(), "DB", "DB.PUBLIC.EXPORT")
        self.assertEqual(2, len(entries))
        self.assertEqual(["rpt_x"], keyless)
        e = entries[0]
        self.assertEqual("marts.fct_orders", e.relation)
        self.assertEqual("main.sf_fct_orders", e.answer_key)
        self.assertIn('"ANALYTICS_MARTS"."FCT_ORDERS"', e.snowflake_relation)

    def test_generated_parity_yaml_flags_a_missing_key(self):
        entries, _ = plan.build_parity(self.project(), "DB", "S")
        text = plan.parity_yaml(entries)
        self.assertIn("key: [order_id]", text)
        self.assertIn("CHOOSE_A_KEY", text)
        self.assertIn("NO unique test found", text)

    def test_no_project_is_not_a_crash(self):
        self.assertEqual(([], []), plan.build_parity(None, "DB", "S"))


class ProjectionAndBlockerTests(unittest.TestCase):

    def test_projection_reproduces_the_measured_run(self):
        p = plan.request_projection(10, 63)
        self.assertEqual(73, p["nodes"])
        self.assertEqual(164, p["requests"])
        self.assertAlmostEqual(6.6, p["minutes_at_25_per_min"], places=1)

    def test_blockers_always_include_the_bi_and_rate_limit_facts(self):
        bs = plan.build_blockers({}, None, [], [], snowflake_available=True)
        ids = {b.id for b in bs}
        self.assertIn("BI-PGWIRE", ids)
        self.assertIn("RATE", ids)
        self.assertIn("ADAPTER", ids)
        self.assertIn("NO-READER", ids)

    def test_bi_clients_in_the_query_history_escalate_the_pgwire_blocker(self):
        rows = {"query_history_clients": [
            {"client": "Tableau Desktop 2024.1", "queries": 4120},
            {"client": "PythonConnector 4.7.4", "queries": 12},
        ]}
        b = [x for x in plan.build_blockers(rows, None, [], [], True) if x.id == "BI-PGWIRE"][0]
        self.assertIn("Tableau", b.triggered_by)
        self.assertNotIn("PythonConnector", b.triggered_by)
        self.assertIn("live dashboards", b.instead)

    def test_a_dbt_only_run_says_not_to_quote_from_it(self):
        bs = plan.build_blockers({}, None, [], [], snowflake_available=False)
        no_sf = [b for b in bs if b.id == "NO-SF"][0]
        self.assertIn("Do not quote", no_sf.instead)

    def test_every_blocker_cites_a_fact_or_says_it_is_an_inference(self):
        texts = {f.text for f in FACTS.values()}
        for b in plan.build_blockers({"tasks": [{"definition": "call x()"}]}, None, [], [], True):
            with self.subTest(blocker=b.id):
                cited = any(t in b.evidence for t in texts)
                self.assertTrue(cited or "nference" in b.evidence or "not a verified fact"
                                in b.evidence or b.id == "NO-SF",
                                f"{b.id} states something with no fact behind it")


if __name__ == "__main__":
    unittest.main()
