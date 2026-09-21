"""The rulebook cross-check: this tool may not invent a rule.

Offline. Run:
    .venv/bin/python -m unittest discover -s partner/tests -p 'test_*.py' -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "partner"))

from assesslib import classify as C, rules as R          # noqa: E402
from assesslib.facts import FACTS                        # noqa: E402

RULEBOOK = (KIT / "agent" / "MIGRATION_RULES.md").read_text(encoding="utf-8")


class RuleTableTests(unittest.TestCase):

    def test_every_rule_id_exists_in_the_rulebook_or_the_fact_table(self):
        """A citation that does not resolve is a bug. Either the id is in
        agent/MIGRATION_RULES.md, or it is a verified fact from facts.py."""
        unknown = []
        for rid in R.rule_ids():
            if rid in FACTS:
                continue
            if not re.search(r"(?<![A-Za-z0-9_-])" + re.escape(rid) + r"(?![A-Za-z0-9_])",
                             RULEBOOK):
                unknown.append(rid)
        self.assertEqual([], unknown,
                         "rule ids cited by partner/assesslib/rules.py that are in neither "
                         "MIGRATION_RULES.md nor facts.py")

    def test_every_pattern_compiles_and_matches_something(self):
        for rule in R.ALL_RULES:
            with self.subTest(rule=rule.id, construct=rule.construct):
                self.assertIsNotNone(rule.regex())

    def test_classes_are_known_and_basis_is_declared(self):
        for rule in R.ALL_RULES:
            with self.subTest(rule=rule.id):
                self.assertIn(rule.klass, C.CLASSES)
                self.assertIn(rule.basis, (C.VERIFIED, C.INFERENCE))
                self.assertIn(rule.precision, ("high", "medium", "low"))
                self.assertTrue(rule.molinia, "every rule says what it becomes")
                self.assertTrue(rule.why or rule.klass == C.AUTOMATIC)

    def test_contexts_partition_the_rule_sets(self):
        self.assertTrue(all("sql" in r.contexts for r in R.SQL_RULES))
        self.assertTrue(all("yaml" in r.contexts for r in R.YAML_RULES))
        self.assertTrue(all("proc" in r.contexts for r in R.PROC_RULES))
        # the proc set is additive, never a replacement
        self.assertGreater(len(R.rules_for("proc")), len(R.rules_for("sql")))

    def test_every_fact_cited_by_a_rule_exists(self):
        for rule in R.ALL_RULES:
            if rule.id.startswith("F-"):
                self.assertIn(rule.id, FACTS)


if __name__ == "__main__":
    unittest.main()
