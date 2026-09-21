"""Unit tests for tools/dbt_molinia.py (the agent's dbt wrapper).

Local only: a fake runner stands in for dbt and a temp dir for .secrets/.
No Snowflake, Molinia or MinIO calls; nothing under the real .secrets/ is read.

Run: .venv/bin/python -m unittest discover -s agent/tests -v   (or `make test`)
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(KIT / "tools"))

import dbt_molinia  # noqa: E402

SECRET = "rd_sa_test_value_never_printed"
BIN = Path("/venv/bin/dbt")


def run(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = dbt_molinia.main(argv, **kw)
    return code, out.getvalue(), err.getvalue()


class BuildArgvTests(unittest.TestCase):
    def test_always_molinia_target_and_local_profiles(self):
        self.assertEqual(dbt_molinia.build_argv(["build"], BIN),
                         [str(BIN), "build", "--target", "molinia", "--profiles-dir", "."])
        self.assertEqual(dbt_molinia.build_argv(["build", "-s", "stg_orders+"], BIN)[-2:],
                         ["-s", "stg_orders+"])
        self.assertEqual(dbt_molinia.build_argv(["--", "retry"], BIN)[1:4], ["retry", "--target", "molinia"])
        for cmd in ("parse", "ls", "list", "compile", "run", "test"):
            self.assertEqual(dbt_molinia.build_argv([cmd], BIN)[1], cmd)

    def test_refuses_target_and_directory_overrides(self):
        for bad in (["build", "--target", "snowflake"], ["build", "--target=snowflake"],
                    ["build", "-t", "snowflake"], ["run", "-tsnowflake"],
                    ["parse", "--profiles-dir", "/tmp"], ["build", "--project-dir", ".."]):
            with self.subTest(bad=bad), self.assertRaises(dbt_molinia.UsageError):
                dbt_molinia.build_argv(bad, BIN)

    def test_refuses_row_returning_and_unsupported_commands(self):
        for cmd in ("show", "seed", "snapshot", "docs", "run-operation", "debug", "clean", ""):
            with self.subTest(cmd=cmd), self.assertRaises(dbt_molinia.UsageError):
                dbt_molinia.build_argv([cmd] if cmd else [], BIN)
        with self.assertRaises(dbt_molinia.UsageError) as cm:
            dbt_molinia.build_argv(["show", "--inline", "select 1"], BIN)
        self.assertIn("H8", str(cm.exception))


class ChildEnvTests(unittest.TestCase):
    def test_strips_other_credentials_and_opts_out_of_tracking(self):
        env = dbt_molinia.child_env({
            "PATH": "/bin", "MOLINIA_API_URL": "http://x", "MOLINIA_ORG_ID": "org_1",
            "MOLINIA_API_KEY": SECRET, "MOLINIA_READONLY_KEY": "ro", "SNOWFLAKE_ACCOUNT": "a",
            "SNOWFLAKE_PRIVATE_KEY_PATH": "/k.p8", "MINIO_SECRET_KEY": "m"})
        self.assertEqual(env["MOLINIA_API_KEY"], SECRET)
        self.assertEqual(env["PATH"], "/bin")
        for gone in ("MOLINIA_READONLY_KEY", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_PRIVATE_KEY_PATH",
                     "MINIO_SECRET_KEY"):
            self.assertNotIn(gone, env)
        self.assertEqual(env["DBT_SEND_ANONYMOUS_USAGE_STATS"], "false")
        self.assertEqual(env["DO_NOT_TRACK"], "1")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.secrets = root / "secrets"
        self.secrets.mkdir()
        self.project = root / "acme_shop"
        self.project.mkdir()
        self.dbt = root / "dbt"
        self.dbt.write_text("#!/bin/sh\n")
        self.calls = []
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for k in list(os.environ):
            if k.startswith(("MOLINIA_", "SNOWFLAKE_", "MINIO_")):
                del os.environ[k]

    def runner(self, argv, cwd, env):
        self.calls.append((argv, cwd, env))
        return 7

    def write_env(self, key=SECRET):
        (self.secrets / "molinia.env").write_text(
            "MOLINIA_API_URL=http://127.0.0.1:9\nMOLINIA_ORG_ID=org_test\n"
            f"MOLINIA_API_KEY={key}\nMOLINIA_READONLY_KEY=rd_sa_readonly\n")
        (self.secrets / "snowflake.env").write_text("SNOWFLAKE_ACCOUNT=acct\n")

    def main(self, argv):
        return run(argv, project_dir=self.project, dbt_bin=self.dbt, secrets_dir=self.secrets,
                   runner=self.runner)

    def test_runs_dbt_in_project_with_key_loaded_and_never_prints_it(self):
        self.write_env()
        code, out, err = self.main(["build", "-s", "stg_orders+"])
        self.assertEqual(code, 7)  # dbt's exit code
        (argv, cwd, env), = self.calls
        self.assertEqual(argv, [str(self.dbt), "build", "--target", "molinia", "--profiles-dir", ".",
                                "-s", "stg_orders+"])
        self.assertEqual(cwd, str(self.project))
        self.assertEqual(env["MOLINIA_API_KEY"], SECRET)
        self.assertNotIn("MOLINIA_READONLY_KEY", env)
        self.assertNotIn("SNOWFLAKE_ACCOUNT", env)
        self.assertIn("dbt build --target molinia", out)
        self.assertNotIn(SECRET, out + err)

    def test_missing_or_placeholder_key_is_a_config_error_naming_the_variable(self):
        self.write_env(key="PASTE_HERE")
        code, out, err = self.main(["parse"])
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("MOLINIA_API_KEY", err)
        self.assertIn("molinia.env", err)

    def test_refused_arguments_never_load_secrets_or_run_dbt(self):
        self.write_env()
        code, out, err = self.main(["build", "--target", "snowflake"])
        self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])
        self.assertNotIn("MOLINIA_API_KEY", os.environ)


class AdapterGateTests(unittest.TestCase):
    """The `adapter-ok` pre-flight gate: it must run, and the docs must agree.

    The gate exists to catch a venv reinstalled from the wrong dbt-molinia
    commit.  Until 2026-09-20 it only asserted that `requests_per_minute` is a
    credentials field, which `0bac8b09` -- the commit immediately before the
    pinned `aec93568` -- already had, so the gate printed ok on an adapter that
    then reported every green dbt test as `'0' is not of type 'integer'`.  It
    now imports `row_types` and coerces a BIGINT, which only `aec93568` can do.

    Local only: no Snowflake, Molinia or MinIO call; no `.secrets/` read.
    """

    COMMAND_RE = re.compile(r'\.venv/bin/python -c "([^"]+)"')

    def gate_from(self, doc):
        text = (KIT / doc).read_text(encoding="utf-8")
        found = self.COMMAND_RE.findall(text)
        self.assertEqual(len(found), 1, f"exactly one `python -c` belongs in {doc}")
        return found[0]

    @unittest.skipUnless((KIT / "RUNSHEET.md").is_file(),
                         "RUNSHEET.md is presenter material, kept out of the public repo")
    def test_claude_md_and_runsheet_carry_the_same_command(self):
        self.assertEqual(self.gate_from("CLAUDE.md"), self.gate_from("RUNSHEET.md"))

    @unittest.skipUnless(importlib.util.find_spec("dbt") is not None
                         and importlib.util.find_spec("dbt.adapters.molinia") is not None,
                         "dbt-molinia is not installed (pre-release, not on PyPI yet: see README)")
    def test_the_documented_command_passes_on_the_installed_adapter(self):
        done = subprocess.run([sys.executable, "-c", self.gate_from("CLAUDE.md")],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "adapter-ok")

    def test_the_gate_asserts_the_bigint_conversion_dbt_tests_depend_on(self):
        gate = self.gate_from("CLAUDE.md")
        self.assertIn("coerce_rows", gate)
        from dbt.adapters.molinia.row_types import coerce_rows
        # The shape a dbt test's query really returns on Molinia:
        # `select count(*) as failures, count(*) != 0 as should_warn`
        # -> columnTypeIds [BIGINT 5, BOOLEAN 1], the count as a JSON string.
        self.assertEqual(coerce_rows([["0", False], ["150", True]], [5, 1]),
                         [(0, False), (150, True)])


if __name__ == "__main__":
    unittest.main()
