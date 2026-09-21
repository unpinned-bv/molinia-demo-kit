"""Unit tests for the Snowflake-ism scanner and the dbt project reader.

Offline: everything runs against strings and temp directories. Run:
    .venv/bin/python -m unittest discover -s partner/tests -p 'test_*.py' -v
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "partner"))

from assesslib import classify as C, scan                # noqa: E402


def ids(hits):
    return sorted({h.rule.id for h in hits})


def hit_lines(hits, rule_id):
    return sorted(h.line for h in hits if h.rule.id == rule_id)


class MaskingTests(unittest.TestCase):

    def test_blank_sql_keeps_offsets_and_lines(self):
        src = "select 1 -- iff(a,b,c)\nfrom t\n"
        out = scan.blank_sql(src)
        self.assertEqual(len(src), len(out))
        self.assertEqual(src.count("\n"), out.count("\n"))
        self.assertNotIn("iff", out)

    def test_line_comment_does_not_count(self):
        hits = scan.scan_text("-- we used to call nvl(a, b) here\nselect 1\n", "m.sql")
        self.assertEqual([], ids(hits))

    def test_block_and_jinja_comments_do_not_count(self):
        src = "/* listagg(x, ',') */\n{# div0(a, b) #}\nselect 1\n"
        self.assertEqual([], ids(scan.scan_text(src, "m.sql")))

    def test_string_literals_do_not_count(self):
        src = "select 'iff(a,b,c)' as note, replace(x, 'div0', '') from t\n"
        self.assertEqual([], ids(scan.scan_text(src, "m.sql")))

    def test_an_escaped_quote_does_not_unmask_the_rest_of_the_file(self):
        src = "select 'it''s fine' as a, iff(x, 1, 0) as b from t\n"
        self.assertEqual(["S1"], ids(scan.scan_text(src, "m.sql")))


class SqlRuleTests(unittest.TestCase):

    def test_conditionals_and_nulls(self):
        src = ("select iff(a, 1, 0) as x, nvl(b, 0) as y, zeroifnull(c) as z,\n"
               "       decode(d, 'a', 1, 2) as w, concat(e, f) as v from t\n")
        self.assertEqual(["S1", "S2", "S3", "S5"], ids(scan.scan_text(src, "m.sql")))

    def test_money_and_division(self):
        src = "select round(q * p * (1 - discount_pct / 100), 2) as amt, div0(a, b) as r from t\n"
        got = ids(scan.scan_text(src, "m.sql"))
        self.assertIn("N2", got)
        self.assertIn("N7", got)

    def test_variant_paths_including_a_qualified_alias(self):
        src = ("select e.payload:session_id::string as s,\n"
               "       f.value:sku::string as sku,\n"
               "       order_meta:shipping.cost as c\n"
               "from t e\n")
        self.assertEqual([1, 2, 3], hit_lines(scan.scan_text(src, "m.sql"), "J1"))

    def test_a_double_colon_cast_is_not_a_variant_path(self):
        self.assertEqual([], ids(scan.scan_text("select x::varchar as y from t\n", "m.sql")))

    def test_flatten_and_windows(self):
        src = ("select f.index, ratio_to_report(x::float) over (partition by g) as share,\n"
               "       listagg(distinct c, ',') within group (order by c) as cs\n"
               "from t, lateral flatten(input => t.payload:items) f\n"
               "qualify row_number() over (partition by k order by k) = 1\n")
        got = ids(scan.scan_text(src, "m.sql"))
        for expected in ("A1", "A2", "A6", "F1", "J1"):
            self.assertIn(expected, got)

    def test_not_supported_constructs(self):
        src = "create table x clone y;\ngrant select on t to role r;\n"
        got = [h.rule for h in scan.scan_text(src, "m.sql")]
        self.assertTrue(any(r.klass == C.NOT_SUPPORTED_TODAY for r in got))

    def test_redesign_constructs(self):
        for src in ("select initcap(x) from t\n",
                    "select hash(a, b) as k from t\n",
                    "select typeof(v) from t\n",
                    "select approx_count_distinct(x) from t\n",
                    "select current_timestamp as now from t\n"):
            with self.subTest(src=src.strip()):
                classes = {h.rule.klass for h in scan.scan_text(src, "m.sql") if h.rule.weight}
                self.assertIn(C.REDESIGN, classes)

    def test_half_to_even_is_found_inside_its_string_literal(self):
        src = "select round(amount, 2, 'HALF_TO_EVEN') as a from t\n"
        self.assertIn("N5", ids(scan.scan_text(src, "m.sql")))

    def test_half_to_even_in_a_comment_is_still_ignored(self):
        src = "-- we used to round(x, 2, 'HALF_TO_EVEN')\nselect 1\n"
        self.assertEqual([], ids(scan.scan_text(src, "m.sql")))

    def test_begin_end_only_fires_in_a_procedure_body(self):
        body = "BEGIN\n  INSERT INTO t VALUES (1);\nEND"
        self.assertEqual([], ids(scan.scan_text(body, "p.sql", "sql")))
        self.assertIn("F-NOTPORT", ids(scan.scan_text(body, "p.sql", "proc")))

    def test_scripting_keywords_fire_anywhere(self):
        self.assertIn("F-NOTPORT",
                      ids(scan.scan_text("execute immediate :stmt;\n", "m.sql", "sql")))

    def test_three_part_names_are_flagged_but_jinja_is_not(self):
        self.assertIn("P5", ids(scan.scan_text("select * from MOLINIA_DEMO.RAW.ORDERS\n", "m.sql")))
        self.assertNotIn("P5", ids(scan.scan_text("select * from {{ source('raw', 'orders') }}\n",
                                                  "m.sql")))


class YamlRuleTests(unittest.TestCase):

    def test_project_configs(self):
        src = textwrap.dedent("""\
            models:
              acme:
                +transient: true
                +query_tag: acme
                marts:
                  +materialized: incremental
            """)
        got = ids(scan.scan_text(src, "dbt_project.yml", "yaml"))
        self.assertIn("P2", got)
        self.assertIn("P6", got)

    def test_grants_is_not_supported(self):
        hits = scan.scan_text("models:\n  acme:\n    +grants:\n      select: [analyst]\n",
                              "dbt_project.yml", "yaml")
        self.assertEqual([C.NOT_SUPPORTED_TODAY], [h.rule.klass for h in hits if h.rule.id == "P2"
                                                   and "grants" in h.rule.construct])

    def test_source_database_key(self):
        self.assertIn("P3", ids(scan.scan_text("sources:\n  - name: raw\n    database: DB\n",
                                               "sources.yml", "yaml")))


class HintTests(unittest.TestCase):

    def test_alias_shadowing_candidate(self):
        src = ("select\n"
               "    coalesce(discount_pct, 0) as discount_pct,\n"
               "    round(q * p * (1 - discount_pct * 0.01), 2) as line_amount\n"
               "from t\n")
        self.assertEqual([(3, "discount_pct")], scan.alias_shadow_lines(src))

    def test_a_cast_alias_is_not_an_alias(self):
        self.assertEqual([], scan.alias_shadow_lines("select cast(x as decimal) as y,\n"
                                                     "       decimal from t\n"))

    def test_regex_backslash_literals(self):
        got = scan.regex_backslash_lines("select regexp_substr(s, '\\\\d+') from t\n")
        self.assertEqual(1, len(got))
        self.assertEqual(1, got[0][0])


class ProjectTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, rel: str, text: str) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")

    def minimal(self) -> None:
        self.write("dbt_project.yml", """\
            name: demo
            profile: demo
            model-paths: ["models"]
            models:
              demo:
                staging:
                  +schema: staging
                  +materialized: view
            """)

    def test_missing_directory(self):
        proj = scan.load_project(self.root / "nope")
        self.assertFalse(proj.ok)
        self.assertIn("not a directory", proj.error)

    def test_directory_without_dbt_project_yml(self):
        proj = scan.load_project(self.root)
        self.assertFalse(proj.ok)
        self.assertIn("no dbt_project.yml", proj.error)

    def test_empty_project(self):
        self.minimal()
        proj = scan.load_project(self.root)
        self.assertTrue(proj.ok)
        self.assertEqual([], proj.models)
        self.assertEqual([], proj.tests)

    def test_models_seeds_snapshots_and_packages(self):
        self.minimal()
        self.write("models/staging/stg_a.sql", "select iff(x, 1, 0) as f from {{ source('r','a') }}\n")
        self.write("models/staging/_models.yml", """\
            version: 2
            models:
              - name: stg_a
                columns:
                  - name: id
                    data_tests:
                      - unique
                      - not_null
            """)
        self.write("seeds/countries.csv", "code,name\nNL,Netherlands\n")
        self.write("snapshots/snap_a.sql", "{% snapshot snap_a %}select 1{% endsnapshot %}\n")
        self.write("packages.yml", "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n")
        proj = scan.load_project(self.root)

        self.assertTrue(proj.ok)
        self.assertEqual(["stg_a"], [m.name for m in proj.models])
        self.assertEqual("view", proj.models[0].materialized)
        self.assertEqual("staging", proj.models[0].schema)
        self.assertEqual(["id"], proj.models[0].key)
        self.assertEqual(2, len(proj.tests))
        self.assertEqual(1, len([n for n in proj.nodes if n.kind == "seed"]))
        self.assertEqual(1, len([n for n in proj.nodes if n.kind == "snapshot"]))
        self.assertEqual(1, len(proj.packages))
        self.assertFalse(proj.packages_installed)

        seed = [n for n in proj.nodes if n.kind == "seed"][0]
        snap = [n for n in proj.nodes if n.kind == "snapshot"][0]
        self.assertEqual(C.NOT_SUPPORTED_TODAY, seed.klass)
        self.assertEqual(C.NOT_SUPPORTED_TODAY, snap.klass)

    def test_python_model_is_a_redesign(self):
        self.minimal()
        self.write("models/staging/py_model.py", "def model(dbt, session):\n    return None\n")
        proj = scan.load_project(self.root)
        node = [m for m in proj.models if m.name == "py_model"][0]
        self.assertEqual(C.REDESIGN, node.klass)
        self.assertEqual("python", node.language)

    def test_composite_key_from_dbt_utils(self):
        self.minimal()
        self.write("models/staging/stg_b.sql", "select 1 as a, 2 as b\n")
        self.write("models/staging/_b.yml", """\
            version: 2
            models:
              - name: stg_b
                data_tests:
                  - dbt_utils.unique_combination_of_columns:
                      arguments:
                        combination_of_columns: [a, b]
            """)
        proj = scan.load_project(self.root)
        node = proj.models[0]
        self.assertEqual(["a", "b"], node.key)
        self.assertIn("unique_combination_of_columns", node.key_source)

    def test_macro_hits_are_attributed_to_callers(self):
        self.minimal()
        self.write("macros/cents.sql",
                   "{% macro cents_to_euro(c) -%}(div0({{ c }}, 100)){%- endmacro %}\n")
        self.write("models/staging/stg_p.sql", "select {{ cents_to_euro('amount') }} as eur\n")
        proj = scan.load_project(self.root)
        self.assertIn("N7", ids(proj.macros[0].hits))
        self.assertEqual({"cents_to_euro": ["models/staging/stg_p.sql"]}, proj.macro_callers)

    def test_a_caller_inherits_its_macro_s_risk(self):
        self.minimal()
        self.write("macros/cents.sql",
                   "{% macro cents_to_euro(c) -%}(div0({{ c }}, 100)){%- endmacro %}\n")
        self.write("models/staging/stg_p.sql", "select {{ cents_to_euro('amount') }} as eur\n")
        proj = scan.load_project(self.root)
        node = proj.models[0]
        self.assertEqual(C.AGENT_PLUS_REVIEW, node.klass, "clean SQL, dialect code in the macro")
        self.assertIn("cents_to_euro", node.reason)
        self.assertTrue(any("carries rule(s) N7" in f for f in node.flags))

    def test_a_clean_macro_does_not_move_its_callers(self):
        self.minimal()
        self.write("macros/plain.sql", "{% macro plain(c) -%}({{ c }} * 2){%- endmacro %}\n")
        self.write("models/staging/stg_q.sql", "select {{ plain('x') }} as y\n")
        proj = scan.load_project(self.root)
        self.assertEqual(C.AUTOMATIC, proj.models[0].klass)
        self.assertEqual([], proj.models[0].flags)

    def test_broken_yaml_is_a_warning_not_a_crash(self):
        self.minimal()
        self.write("models/staging/_bad.yml", "version: 2\nmodels:\n  - name: [unclosed\n")
        proj = scan.load_project(self.root)
        self.assertTrue(proj.ok)
        self.assertTrue(any("_bad.yml" in w for w in proj.warnings))

    def test_incremental_model_is_flagged_as_unproven(self):
        self.minimal()
        self.write("models/staging/stg_i.sql",
                   "{{ config(materialized='incremental') }}\nselect 1 as a\n")
        proj = scan.load_project(self.root)
        node = proj.models[0]
        self.assertEqual("incremental", node.materialized)
        self.assertEqual(C.AGENT_PLUS_REVIEW, node.klass)
        self.assertTrue(any("NEVER proven" in f for f in node.flags))

    def test_profiles_yml_database_key_is_not_a_source_database(self):
        self.minimal()
        self.write("profiles.yml", """\
            demo:
              target: snowflake
              outputs:
                snowflake:
                  type: snowflake
                  database: SOMEDB
                  schema: ANALYTICS
            """)
        proj = scan.load_project(self.root)
        self.assertEqual("ANALYTICS", proj.target_schema)
        self.assertEqual([], [h for h in proj.project_config_hits if h.rule.id == "P3"])


class CoverageOfTheRulebookTests(unittest.TestCase):
    """Constructs the rulebook documents that the scanner used to walk past.

    Each miss here is an under-quote: the node classifies AUTOMATIC ("the agent
    ports it with no judgement") when it is nothing of the kind.
    """

    def klass(self, sql):
        hits = scan.scan_text(sql, "m.sql", "sql")
        return C.worst(*[h.rule.klass for h in hits if h.rule.weight]), ids(hits)

    def test_non_determinism_is_a_redesign_not_an_automatic_port(self):
        """H10 / section 8: RANDOM, UUID_STRING and SEQUENCE cannot be reproduced,
        so parity can never prove the model."""
        for sql in ("select uuid_string() as k from t",
                    "select random() as bucket from t",
                    "select randstr(8, random()) as s from t",
                    "select seq8() as n from t",
                    "select order_seq.nextval as id from t"):
            with self.subTest(sql=sql):
                klass, rules = self.klass(sql)
                self.assertIn("H10", rules)
                self.assertEqual(C.REDESIGN, klass)

    def test_a_whole_model_of_non_determinism_is_not_automatic(self):
        klass, _ = self.klass("select uuid_string() as sk, random() as b, "
                              "s.nextval as n from raw.orders")
        self.assertNotEqual(C.AUTOMATIC, klass)

    def test_nextval_does_not_fire_on_an_ordinary_column(self):
        _klass, rules = self.klass("select t.next_value, nextvalue from t")
        self.assertNotIn("H10", rules)

    def test_a_qualified_sequence_is_still_a_redesign(self):
        """A sequence normally lives in a schema, so `raw.order_seq.nextval` is
        the common spelling. Matching only the bare `seq.nextval` form left it
        scoring as a three-part name (P5, AGENT_PLUS_REVIEW) — cheaper than the
        REDESIGN it is."""
        for sql in ("select raw.order_seq.nextval as id from t",
                    "select acme.raw.order_seq.nextval as id from t",
                    'select "ORDER_SEQ".nextval as id from t'):
            with self.subTest(sql=sql):
                klass, rules = self.klass(sql)
                self.assertIn("H10", rules)
                self.assertEqual(C.REDESIGN, klass)

    def test_a_qualified_sequence_counts_once(self):
        hits = [h for h in scan.scan_text("select a.b.seq.nextval from t", "m.sql")
                if h.rule.id == "H10"]
        self.assertEqual(1, len(hits))

    def test_nextval_in_a_string_literal_does_not_fire(self):
        _klass, rules = self.klass("select 'seq.nextval' as label from t")
        self.assertNotIn("H10", rules)

    def test_integer_casts_are_found(self):
        """N1: a Snowflake INT is NUMBER(38,0); DuckDB INT is 32-bit."""
        for sql in ("select customer_id::int as c from t",
                    "select customer_id :: integer from t",
                    "select cast(qty as integer) from t"):
            with self.subTest(sql=sql):
                self.assertIn("N1", self.klass(sql)[1])

    def test_bigint_cast_is_deliberately_not_flagged(self):
        """`count_if(c)::bigint` is the rulebook's OWN target form (N12, A7).
        Flagging it made every correctly-ported model look like work."""
        _k, rules = self.klass("select count_if(x)::bigint as n from t")
        self.assertNotIn("N1", rules)

    def test_a_bare_decimal_cast_is_review_work_because_duckdb_defaults_it(self):
        klass, rules = self.klass("select amount::decimal as amt from t")
        self.assertIn("N1", rules)
        self.assertEqual(C.AGENT_PLUS_REVIEW, klass)

    def test_a_sized_decimal_cast_is_not_flagged_as_bare(self):
        klass, _ = self.klass("select amount::decimal(10,2) as amt from t")
        self.assertEqual(C.AUTOMATIC, klass)

    def test_desc_ordering_is_a_printed_hint_that_does_not_inflate_the_class(self):
        """A4 is real but low precision: a DESC on a NOT NULL key is harmless, and
        flagging every one would inflate AGENT_PLUS_REVIEW across the estate."""
        hits = scan.scan_text("select x from t order by paid_ts desc", "m.sql", "sql")
        a4 = [h for h in hits if h.rule.id == "A4"]
        self.assertTrue(a4)
        self.assertFalse(a4[0].rule.weight, "A4 must not move a node into a costlier class")
        self.assertEqual(C.AUTOMATIC, self.klass("select x from t order by ts desc")[0])

    def test_desc_with_an_explicit_nulls_clause_is_clean(self):
        _k, rules = self.klass("select x from t order by ts desc nulls first")
        self.assertNotIn("A4", rules)

    def test_desc_does_not_fire_inside_an_identifier(self):
        _k, rules = self.klass("select product_desc, description from t")
        self.assertNotIn("A4", rules)

    def test_variant_subscript_is_found(self):
        self.assertIn("J1", self.klass("select order_meta['coupon'] from t")[1])

    def test_a_table_qualified_variant_subscript_is_found(self):
        """`o.order_meta['k']` is how the subscript is written in any join, and
        it was invisible: the prefix was excluded along with the `.`."""
        for sql in ("select o.order_meta['coupon'] from orders o",
                    "select raw.o.meta['k'] from raw.o"):
            with self.subTest(sql=sql):
                self.assertIn("J1", self.klass(sql)[1])

    def test_a_qualified_subscript_counts_once(self):
        hits = [h for h in scan.scan_text("select raw.o.meta['k'] from raw.o", "m.sql")
                if h.rule.id == "J1"]
        self.assertEqual(1, len(hits))

    def test_a_list_index_is_not_a_variant_subscript(self):
        """The quote is the whole distinction: `arr[1]` ports unchanged."""
        for sql in ("select arr[1] from t", "select t.arr[1] from t",
                    "select arr[idx] from t"):
            with self.subTest(sql=sql):
                self.assertNotIn("J1", self.klass(sql)[1])

    def test_secure_view_is_a_redesign(self):
        klass, rules = self.klass("{{ config(secure=true) }} select 1")
        self.assertIn("Y2", rules)
        self.assertEqual(C.REDESIGN, klass)

    def test_comments_still_do_not_count(self):
        _k, rules = self.klass("-- uuid_string() and x::int and order by ts desc\nselect 1")
        self.assertEqual([], rules)


if __name__ == "__main__":
    unittest.main()
