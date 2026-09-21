"""Unit tests for tools/_engagement.py (the per-client engagement config).

Two jobs. First, lock the shipped engagement.yml to the values the kit had
before it was parameterised: if any of these drift, the demo changes behaviour
and this suite says so by name. Second, prove that a second engagement file
actually re-points the kit, and that a dangerous config is refused at load time
rather than at DROP SCHEMA time.

Local only: temp files, no Snowflake, Molinia or MinIO calls.

Run: .venv/bin/python -m unittest discover -s agent/tests -v   (or `make test`)
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "tools"))

import _engagement  # noqa: E402

# Imported here, before any test reloads the singleton: these modules capture
# ENGAGEMENT at import time, and the WiredToolsTests below assert on what they
# captured. Importing them inside a test would race the reload test.
import _env  # noqa: E402
import dbt_molinia  # noqa: E402
import molinia  # noqa: E402
import sf  # noqa: E402

# Exactly what the kit hardcoded before engagement.yml existed. Do not "fix"
# these to match a changed config: they are the demo's contract.
DEMO = {
    "project_dir": KIT / "acme_shop",
    "export_prefix": "acme-snowflake-export",
    "export_root": KIT / "exports" / "acme-snowflake-export",
    "stage": "MOLINIA_DEMO.PUBLIC.MOLINIA_EXPORT",
    "source_schema": "main",
    "dbt_schemas": ("staging", "intermediate", "marts"),
    "raw_prefix": "raw_",
    "answer_key_prefix": "sf_",
    "data_source_id": 1,
    "location_id": 1,
    "forbidden_tables": ("main.raw_customer_contacts",),
    "build_sa": "demo-dbt-build",
    "readonly_sa": "demo-agent-readonly",
}

# tools/molinia.py's status query, as it read before it became a function.
STATUS_SQL_BEFORE = (
    "SELECT table_catalog, table_schema, table_name, table_type, "
    "current_database() AS current_db, current_setting('search_path') AS search_path "
    "FROM information_schema.tables "
    "WHERE lower(table_schema) IN ('staging', 'intermediate', 'marts') "
    "OR (lower(table_schema) = 'main' AND (starts_with(lower(table_name), 'raw_') "
    "OR starts_with(lower(table_name), 'sf_'))) "
    "ORDER BY 2, 3, 1"
)

OTHER_CLIENT = textwrap.dedent("""
    name: northwind
    project_dir: client_dbt
    export_prefix: northwind-export
    snowflake:
      stage: NW_PROD.PUBLIC.NW_UNLOAD
    molinia:
      source_schema: landing
      dbt_schemas: [bronze, silver, gold]
      raw_prefix: src_
      answer_key_prefix: expected_
      data_source_id: 7
      location_id: 3
    forbidden_tables:
      - landing.src_patients
      - src_staff
    service_accounts:
      build: nw-dbt-build
      readonly: nw-readonly
""")


def write(tmp: Path, text: str) -> Path:
    p = tmp / "engagement.yml"
    p.write_text(text, encoding="utf-8")
    return p


def load_yaml(text: str, **kw):
    with tempfile.TemporaryDirectory() as d:
        return _engagement.load(write(Path(d), text), **kw)


class ShippedEngagementTests(unittest.TestCase):
    """The file the demo runs on still says what the code used to hardcode."""

    @classmethod
    def setUpClass(cls):
        cls.e = _engagement.load(KIT / "engagement.yml", required=True)

    def test_engagement_file_is_present(self):
        self.assertTrue((KIT / "engagement.yml").is_file(),
                        "engagement.yml is missing; the tools would fall back to defaults")
        self.assertTrue(self.e.from_file)

    def test_project_and_exports(self):
        self.assertEqual(self.e.project_dir, DEMO["project_dir"])
        self.assertEqual(self.e.export_prefix, DEMO["export_prefix"])
        self.assertEqual(self.e.export_root, DEMO["export_root"])

    def test_snowflake_stage(self):
        self.assertEqual(self.e.snowflake_stage, DEMO["stage"])

    def test_schemas_and_prefixes(self):
        self.assertEqual(self.e.source_schema, DEMO["source_schema"])
        self.assertEqual(self.e.dbt_schemas, DEMO["dbt_schemas"])
        self.assertEqual(self.e.raw_prefix, DEMO["raw_prefix"])
        self.assertEqual(self.e.answer_key_prefix, DEMO["answer_key_prefix"])

    def test_ingest_ids(self):
        self.assertEqual(self.e.data_source_id, DEMO["data_source_id"])
        self.assertEqual(self.e.location_id, DEMO["location_id"])

    def test_forbidden_and_service_accounts(self):
        self.assertEqual(self.e.forbidden_tables, DEMO["forbidden_tables"])
        self.assertEqual(self.e.service_account_build, DEMO["build_sa"])
        self.assertEqual(self.e.service_account_readonly, DEMO["readonly_sa"])

    def test_derived_values_match_the_old_constants(self):
        self.assertEqual(self.e.target_prefix, {"raw": "raw_", "expected": "sf_"})
        self.assertEqual(self.e.schema_order,
                         {"main": 0, "staging": 1, "intermediate": 2, "marts": 3})
        self.assertEqual(self.e.answer_key("fct_orders"), "main.sf_fct_orders")
        self.assertEqual(self.e.raw_table("orders"), "main.raw_orders")
        self.assertEqual(self.e.schema_list(), "staging / intermediate / marts")


class WiredToolsTests(unittest.TestCase):
    """The tools read the config and still produce the pre-change values."""

    def test_status_sql_is_byte_identical_to_the_old_constant(self):
        self.assertEqual(molinia.status_list_sql(), STATUS_SQL_BEFORE)

    def test_molinia_constants(self):
        self.assertEqual(molinia.EXPORT_PREFIX, DEMO["export_prefix"])
        self.assertEqual(molinia.DATA_SOURCE_ID, DEMO["data_source_id"])
        self.assertEqual(molinia.LOCATION_ID, DEMO["location_id"])
        self.assertEqual(molinia.TARGET_PREFIX, {"raw": "raw_", "expected": "sf_"})
        self.assertEqual(tuple(molinia.DBT_SCHEMAS), DEMO["dbt_schemas"])

    def test_reset_still_refuses_every_protected_schema(self):
        for schema in sorted(_engagement.PROTECTED_SCHEMAS):
            with self.assertRaises(ValueError):
                molinia.drop_schema_sql(schema)

    def test_sf_stage_and_project(self):
        self.assertEqual(sf.STAGE, DEMO["stage"])
        self.assertEqual(sf.DBT_PROJECT, DEMO["project_dir"])

    def test_dbt_wrapper_project(self):
        self.assertEqual(dbt_molinia.DBT_PROJECT, DEMO["project_dir"])

    def test_env_export_root(self):
        self.assertEqual(_env.EXPORT_ROOT, DEMO["export_root"])

    def test_status_sql_follows_the_config(self):
        other = load_yaml(OTHER_CLIENT)
        sql = molinia.status_list_sql(other)
        self.assertIn("lower(table_schema) IN ('bronze', 'silver', 'gold')", sql)
        self.assertIn("lower(table_schema) = 'landing'", sql)
        self.assertIn("starts_with(lower(table_name), 'src_')", sql)
        self.assertIn("starts_with(lower(table_name), 'expected_')", sql)
        self.assertNotIn("staging", sql)
        self.assertNotIn("'raw_'", sql)


class RepointingTests(unittest.TestCase):
    """A second file actually moves the kit to another client."""

    def setUp(self):
        self.e = load_yaml(OTHER_CLIENT)

    def test_every_value_moves(self):
        self.assertEqual(self.e.name, "northwind")
        self.assertEqual(self.e.project_dir, KIT / "client_dbt")
        self.assertEqual(self.e.export_prefix, "northwind-export")
        self.assertEqual(self.e.export_root, KIT / "exports" / "northwind-export")
        self.assertEqual(self.e.snowflake_stage, "NW_PROD.PUBLIC.NW_UNLOAD")
        self.assertEqual(self.e.source_schema, "landing")
        self.assertEqual(self.e.dbt_schemas, ("bronze", "silver", "gold"))
        self.assertEqual(self.e.data_source_id, 7)
        self.assertEqual(self.e.location_id, 3)
        self.assertEqual(self.e.service_account_build, "nw-dbt-build")
        self.assertEqual(self.e.service_account_readonly, "nw-readonly")

    def test_derived_values_follow(self):
        self.assertEqual(self.e.answer_key("fct_sales"), "landing.expected_fct_sales")
        self.assertEqual(self.e.raw_table("patients"), "landing.src_patients")
        self.assertEqual(self.e.target_prefix, {"raw": "src_", "expected": "expected_"})
        self.assertEqual(self.e.schema_order,
                         {"landing": 0, "bronze": 1, "silver": 2, "gold": 3})

    def test_bare_forbidden_table_is_qualified_with_the_source_schema(self):
        self.assertEqual(self.e.forbidden_tables,
                         ("landing.src_patients", "landing.src_staff"))
        self.assertTrue(self.e.is_forbidden("src_staff"))
        self.assertTrue(self.e.is_forbidden("landing.src_staff"))
        self.assertTrue(self.e.is_forbidden("LANDING.SRC_STAFF"))
        self.assertFalse(self.e.is_forbidden("src_orders"))

    def test_absolute_project_dir_is_left_alone(self):
        e = load_yaml(OTHER_CLIENT.replace("project_dir: client_dbt",
                                           "project_dir: /srv/clients/nw/dbt"))
        self.assertEqual(e.project_dir, Path("/srv/clients/nw/dbt"))

    def test_env_var_repoints_the_module_singleton(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(Path(d), OTHER_CLIENT)
            try:
                with mock.patch.dict(os.environ, {"MOLINIA_KIT_ENGAGEMENT": str(path)}):
                    reloaded = importlib.reload(_engagement)
                    self.assertEqual(reloaded.ENGAGEMENT.name, "northwind")
                    self.assertEqual(reloaded.ENGAGEMENT.source_schema, "landing")
            finally:
                # Outside the patch, so the singleton goes back to engagement.yml.
                importlib.reload(_engagement)
        self.assertEqual(_engagement.ENGAGEMENT.source_schema, "main")
        self.assertEqual(molinia.ENGAGEMENT.source_schema, "main",
                         "a reload must not disturb a module that already captured it")


class ValidationTests(unittest.TestCase):
    """A config that would destroy data is refused when it is loaded."""

    def assertRefused(self, text, *expected_in_message):
        with self.assertRaises(_engagement.EngagementError) as ctx:
            load_yaml(text)
        message = str(ctx.exception)
        for fragment in expected_in_message:
            self.assertIn(fragment, message)

    def test_source_schema_in_dbt_schemas_is_refused(self):
        # `reset` drops every dbt schema; listing main would drop the answer keys.
        self.assertRefused(
            OTHER_CLIENT.replace("dbt_schemas: [bronze, silver, gold]",
                                 "dbt_schemas: [bronze, landing, gold]"),
            "molinia.dbt_schemas[1]", "source_schema")

    def test_protected_schema_in_dbt_schemas_is_refused(self):
        for schema in ("main", "information_schema", "pg_catalog"):
            self.assertRefused(
                OTHER_CLIENT.replace("dbt_schemas: [bronze, silver, gold]",
                                     f"dbt_schemas: [bronze, {schema}]"),
                "protected")

    def test_duplicate_schema_is_refused(self):
        self.assertRefused(
            OTHER_CLIENT.replace("dbt_schemas: [bronze, silver, gold]",
                                 "dbt_schemas: [bronze, bronze]"),
            "listed twice")

    def test_empty_dbt_schemas_is_refused(self):
        self.assertRefused(
            OTHER_CLIENT.replace("dbt_schemas: [bronze, silver, gold]", "dbt_schemas: []"),
            "at least one schema")

    def test_colliding_prefixes_are_refused(self):
        self.assertRefused(
            OTHER_CLIENT.replace("answer_key_prefix: expected_", "answer_key_prefix: src_"),
            "collide")

    def test_prefix_that_is_a_prefix_of_the_other_is_refused(self):
        self.assertRefused(
            OTHER_CLIENT.replace("answer_key_prefix: expected_", "answer_key_prefix: src_x_"),
            "prefix of the other")

    def test_malformed_stage_is_refused(self):
        for stage in ("NW_UNLOAD", "NW_PROD.NW_UNLOAD", "NW_PROD.PUBLIC.NW.UNLOAD", "a b.c.d"):
            self.assertRefused(
                OTHER_CLIENT.replace("stage: NW_PROD.PUBLIC.NW_UNLOAD", f"stage: {stage}"),
                "snowflake.stage")

    def test_non_identifier_schema_is_refused(self):
        self.assertRefused(
            OTHER_CLIENT.replace("source_schema: landing", 'source_schema: "my schema"'),
            "molinia.source_schema")

    def test_non_positive_ids_are_refused(self):
        self.assertRefused(OTHER_CLIENT.replace("data_source_id: 7", "data_source_id: 0"),
                           "molinia.data_source_id")
        self.assertRefused(OTHER_CLIENT.replace("location_id: 3", "location_id: -1"),
                           "molinia.location_id")
        self.assertRefused(OTHER_CLIENT.replace("location_id: 3", "location_id: two"),
                           "molinia.location_id")

    def test_bad_export_prefix_is_refused(self):
        for prefix in ("/northwind-export", "northwind-export/", "north\\\\wind"):
            self.assertRefused(
                OTHER_CLIENT.replace("export_prefix: northwind-export", f'export_prefix: "{prefix}"'),
                "export_prefix")

    def test_empty_required_value_is_refused(self):
        self.assertRefused(OTHER_CLIENT.replace("build: nw-dbt-build", 'build: ""'),
                           "service_accounts.build")

    def test_invalid_yaml_is_refused(self):
        self.assertRefused("name: [unclosed\n", "not valid YAML")

    def test_top_level_must_be_a_mapping(self):
        self.assertRefused("- a\n- b\n", "mapping")


class MissingFileTests(unittest.TestCase):
    def test_missing_file_falls_back_to_the_demo_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            _engagement._WARNED = True          # keep the warning out of test output
            e = _engagement.load(Path(d) / "nope.yml")
        self.assertIsNone(e.path)
        self.assertFalse(e.from_file)
        self.assertEqual(e.source_schema, DEMO["source_schema"])
        self.assertEqual(e.dbt_schemas, DEMO["dbt_schemas"])
        self.assertEqual(e.snowflake_stage, DEMO["stage"])

    def test_required_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(_engagement.EngagementError):
                _engagement.load(Path(d) / "nope.yml", required=True)

    def test_empty_file_is_the_defaults(self):
        self.assertEqual(load_yaml("").dbt_schemas, DEMO["dbt_schemas"])

    def test_partial_file_keeps_the_other_defaults(self):
        e = load_yaml("molinia:\n  data_source_id: 9\n")
        self.assertEqual(e.data_source_id, 9)
        self.assertEqual(e.dbt_schemas, DEMO["dbt_schemas"])
        self.assertEqual(e.source_schema, DEMO["source_schema"])


if __name__ == "__main__":
    unittest.main()


class ForbiddenTableCheckTests(unittest.TestCase):
    """tools/engagement.py check must catch a real H2 violation, and must not
    flag the declaration that documents the table as PII."""

    SOURCES = textwrap.dedent("""
        version: 2
        sources:
          - name: raw
            schema: main
            tables:
              - name: customers
                identifier: raw_customers
              - name: customer_contacts
                identifier: raw_customer_contacts
                description: PII, not modelled.
        """)

    def project(self, tmp: Path, sources: str, models: dict) -> Path:
        proj = tmp / "proj"
        (proj / "models").mkdir(parents=True)
        (proj / "dbt_project.yml").write_text("name: t\nversion: '1.0'\n", encoding="utf-8")
        (proj / "models" / "sources.yml").write_text(sources, encoding="utf-8")
        for name, sql in models.items():
            (proj / "models" / name).write_text(sql, encoding="utf-8")
        return proj

    def findings(self, sources: str, models: dict) -> list:
        import engagement as cli
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            proj = self.project(tmp, sources, models)
            e = load_yaml(f"project_dir: {proj}\n"
                          "forbidden_tables:\n  - main.raw_customer_contacts\n")
            return cli.check_forbidden_tables(e)

    def test_declaration_without_tests_is_not_a_finding(self):
        self.assertEqual(
            self.findings(self.SOURCES, {"ok.sql": "select * from {{ source('raw','customers') }}"}),
            [])

    def test_source_call_to_a_forbidden_table_is_caught(self):
        found = self.findings(self.SOURCES,
                              {"bad.sql": "select * from {{ source('raw', 'customer_contacts') }}"})
        self.assertEqual(len(found), 1, found)
        self.assertIn("bad.sql:1", found[0])
        self.assertIn("reads source raw.customer_contacts", found[0])

    def test_hand_written_relation_is_caught(self):
        found = self.findings(self.SOURCES,
                              {"bad.sql": "select count(*) from main.raw_customer_contacts"})
        self.assertEqual(len(found), 1, found)
        self.assertIn("names a forbidden table", found[0])

    def test_tests_on_the_forbidden_source_are_caught(self):
        marker = "        description: PII, not modelled.\n"
        self.assertIn(marker, self.SOURCES, "fixture drifted; the edit below would be a no-op")
        with_tests = self.SOURCES.replace(
            marker,
            "        columns:\n"
            "          - name: iban\n"
            "            data_tests:\n"
            "              - not_null\n")
        self.assertIn("data_tests", with_tests)
        found = self.findings(with_tests, {"ok.sql": "select 1"})
        self.assertEqual(len(found), 1, found)
        self.assertIn("carries tests", found[0])

    def test_a_comment_mentioning_it_is_not_a_read(self):
        self.assertEqual(
            self.findings(self.SOURCES,
                          {"ok.sql": "-- raw_customer_contacts is PII, not modelled\nselect 1"}),
            [])

    def test_the_shipped_project_is_clean(self):
        import engagement as cli
        e = _engagement.load(KIT / "engagement.yml", required=True)
        self.assertEqual(cli.check_forbidden_tables(e), [])


class DocDriftCheckTests(unittest.TestCase):
    """Re-pointing the engagement must produce a work list for the prose that
    a config cannot rewrite."""

    def test_no_drift_for_the_shipped_engagement(self):
        import engagement as cli
        e = _engagement.load(KIT / "engagement.yml", required=True)
        self.assertEqual(cli.check_docs(e), [])

    def test_moved_values_are_reported_with_file_and_line(self):
        import engagement as cli
        findings = cli.check_docs(load_yaml(OTHER_CLIENT))
        self.assertTrue(findings, "re-pointing the kit must flag the prose")
        joined = "\n".join(f for _, f in findings)
        self.assertIn("CLAUDE.md", joined)
        self.assertIn("acme_shop", joined)
        self.assertIn("client_dbt", joined)
        for tier, line in findings:
            self.assertIn(tier, ("agent", "partner", "demo"))
            self.assertRegex(line, r"^[\w./-]+:\d+: says ")

    def test_findings_are_tiered_so_the_list_is_actionable(self):
        import engagement as cli
        by_tier = {}
        for tier, finding in cli.check_docs(load_yaml(OTHER_CLIENT)):
            by_tier.setdefault(tier, []).append(finding)
        # The agent's own instructions and rulebook must be in the blocking tier.
        agent = "\n".join(by_tier.get("agent", []))
        self.assertIn("CLAUDE.md", agent)
        self.assertIn("agent/MIGRATION_RULES.md", agent)
        # The run-sheet describes this demo, not the engagement: informational.
        # It is presenter material and absent from the public repo.
        if (KIT / "RUNSHEET.md").is_file():
            demo = "\n".join(by_tier.get("demo", []))
            self.assertIn("RUNSHEET.md", demo)
            self.assertNotIn("RUNSHEET.md", agent)

    def test_check_exit_code_is_driven_by_the_blocking_tiers(self):
        import engagement as cli
        args = argparse.Namespace(quiet=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_check(args, _engagement.load(KIT / "engagement.yml")), 0)
            self.assertEqual(cli.cmd_check(args, load_yaml(OTHER_CLIENT)), 1)

    def test_parity_config_mismatch_is_reported(self):
        import engagement as cli
        e = load_yaml(OTHER_CLIENT)
        findings = cli.check_parity_config(e, KIT / "agent" / "parity.yml")
        self.assertTrue(findings)
        self.assertTrue(any("answer_key" in f for f in findings), findings)
