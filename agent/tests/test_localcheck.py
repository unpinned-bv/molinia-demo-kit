"""Unit tests for agent/localcheck.py (the free local dry-run) and for the
macro snippet the rulebook ships.

Local only: every project used here is built in a temp dir. No Snowflake,
Molinia or MinIO calls, nothing under .secrets/ is read, and acme_shop/ is
never touched.

Run: .venv/bin/python -m unittest discover -s agent/tests -v   (or `make test`)
"""
from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "agent"))

import localcheck  # noqa: E402

PROJECT_YML = textwrap.dedent("""\
    name: tiny
    profile: tiny
    vars:
      as_of_date: "2026-06-30"
    """)

# marts/a_total sorts before staging/stg_orders, so a build in path order fails:
# this project only runs if the ref() graph is honoured.
STG_ORDERS = """\
select
    order_id,
    upper(trim(status))                          as status,
    order_meta ->> '$.coupon'                    as coupon_code,
    {{ cents_to_cost('order_id') }}              as fee,
    datediff('day', order_ts::date, '{{ var("as_of_date") }}'::date) as age_days
from {{ source('raw', 'orders') }}
"""

A_TOTAL = """\
{{ config(materialized='table') }}
select count(*) as orders, count_if(coupon_code is not null)::bigint as with_coupon
from {{ ref('stg_orders') }}
"""

MACRO = """\
{% macro cents_to_cost(column_name) -%}
({{ column_name }} * 0.01)::decimal(12, 2)
{%- endmacro %}
"""

SINGULAR_TEST = """\
select order_id from {{ ref('stg_orders') }} where age_days < 0
"""


def make_project(root: Path, *, broken: bool = False) -> Path:
    project = root / "tiny"
    (project / "models" / "staging").mkdir(parents=True)
    (project / "models" / "marts").mkdir(parents=True)
    (project / "macros").mkdir()
    (project / "tests").mkdir()
    (project / "dbt_project.yml").write_text(PROJECT_YML)
    (project / "macros" / "cents_to_cost.sql").write_text(MACRO)
    body = STG_ORDERS.replace("order_meta ->> '$.coupon'", "order_meta:coupon::string") if broken else STG_ORDERS
    (project / "models" / "staging" / "stg_orders.sql").write_text(body)
    (project / "models" / "marts" / "a_total.sql").write_text(A_TOTAL)
    (project / "tests" / "assert_age_nonnegative.sql").write_text(SINGULAR_TEST)
    return project


def run(argv):
    out = io.StringIO()
    code = localcheck.main(argv, out=out)
    return code, out.getvalue()


class GreenProjectTests(unittest.TestCase):
    def test_ported_project_runs_every_model_and_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            code, text = run(["--project", str(project)])
        self.assertEqual(code, 0, text)
        self.assertIn("OK   stg_orders", text)
        self.assertIn("OK   a_total", text)
        self.assertIn("OK   assert_age_nonnegative", text)
        self.assertIn("0 failure(s)", text)
        self.assertNotIn("FAIL", text)
        # a_total refs stg_orders, so it must run after it even though its path sorts first
        self.assertLess(text.index("OK   stg_orders"), text.index("OK   a_total"))

    def test_types_flag_reports_the_engine_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            code, text = run(["--project", str(project), "--types"])
        self.assertEqual(code, 0, text)
        self.assertIn("order_id DECIMAL(38,0)", text)   # ingest leaves NUMBER(38,0) as DECIMAL
        self.assertIn("fee DECIMAL(12,2)", text)        # the macro's cast survived
        self.assertIn("with_coupon BIGINT", text)       # count_if(...)::bigint, rule N12

    def test_only_and_values_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            code, text = run(["--project", str(project), "--only", "stg_orders", "--values"])
        self.assertEqual(code, 0, text)
        self.assertIn("OK   stg_orders", text)
        self.assertNotIn("OK   a_total", text)
        self.assertIn("PLACED", text)  # a fixture row, printed only with --values

    def test_only_a_downstream_model_builds_its_upstreams(self):
        """`--only a_total` must not fail on the inputs it did not ask for."""
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            code, text = run(["--project", str(project), "--only", "a_total"])
        self.assertEqual(code, 0, text)         # stg_orders is built, silently
        self.assertIn("OK   a_total", text)
        self.assertNotIn("FAIL", text)
        self.assertNotIn("OK   stg_orders", text)   # built, but only the selection is reported
        self.assertIn("1/1 model(s) ran", text)     # the denominator is what was reported

    def test_only_an_unknown_name_is_an_error_not_a_green_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            code, text = run(["--project", str(project), "--only", "stg_order"])
        self.assertNotEqual(code, 0, text)      # a typo must never exit 0 with nothing checked
        self.assertEqual(code, 2, text)
        self.assertIn("unknown name(s) for --only: stg_order", text)
        self.assertIn("stg_orders", text)       # the known names are listed, as parity.py does
        self.assertIn("assert_age_nonnegative", text)
        self.assertNotIn("OK", text)


class FailingProjectTests(unittest.TestCase):
    def test_unported_snowflake_sql_fails_with_the_engine_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp), broken=True)
            code, text = run(["--project", str(project)])
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL stg_orders", text)
        self.assertIn("1 failure(s)", text)
        # the children are skipped, not reported as separate failures
        self.assertIn("SKIP a_total", text)
        self.assertIn("SKIP assert_age_nonnegative", text)

    def test_only_a_downstream_model_skips_when_its_upstream_is_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp), broken=True)
            code, text = run(["--project", str(project), "--only", "a_total"])
        self.assertEqual(code, 1, text)
        self.assertIn("FAIL stg_orders", text)       # the real cause is named
        self.assertIn("(upstream of --only)", text)
        self.assertIn("SKIP a_total", text)          # the selection is skipped, not blamed

    def test_missing_project_is_reported_not_raised(self):
        code, text = run(["--project", "/nonexistent/project"])
        self.assertEqual(code, 2)
        self.assertIn("no such project directory", text)

    def test_project_without_models_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "models").mkdir()
            code, text = run(["--project", tmp])
        self.assertEqual(code, 2)
        self.assertIn("no models", text)


class ListingTests(unittest.TestCase):
    def test_list_prints_dependency_order_and_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = make_project(Path(tmp))
            code, text = run(["--project", str(project), "--list"])
        self.assertEqual(code, 0, text)
        self.assertLess(text.index("stg_orders"), text.index("a_total"))
        self.assertIn("raw_orders", text)
        self.assertIn("raw_customer_contacts", text)  # named only to say it has no fixture (H2)

    def test_help_parses(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), self.assertRaises(SystemExit) as cm:
            localcheck.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--project", captured.getvalue())
        self.assertIn("agent/parity.py", captured.getvalue())  # the honesty line is in --help


class FixtureTests(unittest.TestCase):
    def test_no_pii_fixture_and_the_documented_types(self):
        self.assertNotIn("raw_customer_contacts", localcheck.FIXTURES)
        self.assertIn("order_meta varchar", localcheck.FIXTURES)   # J0: VARIANT arrives as VARCHAR
        self.assertIn("payload varchar", localcheck.FIXTURES)
        for ident in ("customer_id", "order_id", "product_id", "payment_id", "event_id"):
            self.assertRegex(localcheck.FIXTURES, rf"{ident} decimal\(38,0\)")

    def test_ref_regex_finds_both_quote_styles(self):
        found = localcheck.REF_RE.findall("{{ ref('a') }} {{ ref(\"b\") }} {{ ref( 'c' ) }}")
        self.assertEqual(found, ["a", "b", "c"])


class MacroSnippetTests(unittest.TestCase):
    """The rulebook and agent/snippets/ must not drift apart (rehearsal lesson 2)."""

    SNIPPET = KIT / "agent" / "snippets" / "molinia_round_div.sql"
    RULES = KIT / "agent" / "MIGRATION_RULES.md"

    def test_snippet_matches_the_rulebook_block_byte_for_byte(self):
        text = self.RULES.read_text(encoding="utf-8")
        blocks = re.findall(
            r"<!-- snippet:molinia_round_div:begin -->\s*```sql\n(.*?)```\s*"
            r"<!-- snippet:molinia_round_div:end -->", text, re.S)
        self.assertEqual(len(blocks), 1, "exactly one marked macro block belongs in the rulebook")
        self.assertEqual(blocks[0], self.SNIPPET.read_text(encoding="utf-8"))

    def test_snippet_renders_and_rounds_half_away_from_zero(self):
        import duckdb
        import jinja2
        macro = self.SNIPPET.read_text(encoding="utf-8")
        env = jinja2.Environment()

        def call(expr):
            return env.from_string(macro + "{{ " + expr + " }}").render().strip()

        con = duckdb.connect()
        got = con.execute(
            f"select {call(chr(39).join(['molinia_round_div(', '17.85', ', ', '2', ', 2)']))} as half_up,"
            f" {call('molinia_round_div(\"1.00\", \"0\", 2)')} as by_zero,"
            f" {call('molinia_round_div(\"1.00\", \"0\", 2, div0=true)')} as div0"
        ).fetchone()
        self.assertEqual(str(got[0]), "8.93")   # 8.925 rounds away from zero, as Snowflake does
        self.assertIsNone(got[1])
        self.assertEqual(int(got[2]), 0)


if __name__ == "__main__":
    unittest.main()
