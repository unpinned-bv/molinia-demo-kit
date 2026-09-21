#!/usr/bin/env python3
"""Run dbt in the engagement's dbt project against the Molinia target, with the
key loaded for you.

  .venv/bin/python tools/dbt_molinia.py <command> [dbt args ...]
      command: parse | ls | list | compile | build | run | test | retry
      e.g.  tools/dbt_molinia.py parse
            tools/dbt_molinia.py build
            tools/dbt_molinia.py build -s stg_orders+
            tools/dbt_molinia.py retry

Why it exists: the migration agent must never name `.secrets/` in a command.
Sourcing a credential file by hand is exactly what makes an agent stop and ask
the presenter mid-demo. Like every kit tool, this one loads `.secrets/*.env`
itself (tools/_env.py) and never prints a value.

What it enforces:
  * always `--target molinia --profiles-dir .`, run from the project dir, with
    DBT_SEND_ANONYMOUS_USAGE_STATS=false and DO_NOT_TRACK=1;
  * refuses --target / -t / --profiles-dir / --project-dir (the Snowflake
    target is tools/sf.py's job and off limits to the agent: rule H1);
  * only the commands above. `show` returns rows (H8); `seed`, `snapshot` and
    `docs` fail on dbt-molinia today; `run-operation` is hand-written DDL (H6);
  * dbt's environment gets MOLINIA_API_URL / MOLINIA_ORG_ID / MOLINIA_API_KEY
    and nothing from snowflake.env or minio.env, and no MOLINIA_READONLY_KEY
    (H4: one key).

Exit code: dbt's; 2 on a usage or configuration error.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _env  # noqa: E402
from _engagement import ENGAGEMENT  # noqa: E402

KIT = _env.KIT_ROOT
DBT_PROJECT = ENGAGEMENT.project_dir
DBT_BIN = KIT / ".venv" / "bin" / "dbt"
TARGET = "molinia"

ALLOWED_COMMANDS = ("parse", "ls", "list", "compile", "build", "run", "test", "retry")
REFUSED_COMMANDS = {
    "show": "it returns rows; diagnostics are counts only (rule H8)",
    "seed": "seeds fail on dbt-molinia today (rulebook section 5)",
    "snapshot": "snapshots fail on dbt-molinia today (rulebook section 5)",
    "docs": "the docs catalog fails on dbt-molinia today (rulebook section 5)",
    "run-operation": "that is hand-written DDL/DML (rule H6)",
}
REFUSED_FLAGS = ("--target", "-t", "--profiles-dir", "--project-dir")
MOLINIA_VARS = ("MOLINIA_API_URL", "MOLINIA_ORG_ID", "MOLINIA_API_KEY")
# Never handed to dbt: Snowflake and MinIO settings, and the presenter's key.
STRIPPED_PREFIXES = ("SNOWFLAKE_", "MINIO_")
STRIPPED_NAMES = ("MOLINIA_READONLY_KEY",)

USAGE = ("usage: .venv/bin/python tools/dbt_molinia.py <command> [dbt args ...]\n"
         "  command: " + " | ".join(ALLOWED_COMMANDS) + "\n"
         "  e.g. tools/dbt_molinia.py build -s stg_orders+")


class UsageError(ValueError):
    """The arguments are not allowed. The message says why."""


def build_argv(args: Sequence[str], dbt_bin: Path = DBT_BIN) -> List[str]:
    """[dbt, <command>, --target molinia, --profiles-dir ., <args...>] or UsageError."""
    a = list(args)
    if a and a[0] == "--":
        a = a[1:]
    if not a:
        raise UsageError("no dbt command given")
    cmd, rest = a[0], a[1:]
    if cmd in REFUSED_COMMANDS:
        raise UsageError(f"`dbt {cmd}` is refused here: {REFUSED_COMMANDS[cmd]}")
    if cmd not in ALLOWED_COMMANDS:
        raise UsageError(f"`{cmd}` is not an allowed dbt command")
    for x in rest:
        name = x.split("=", 1)[0]
        if name in REFUSED_FLAGS or (x.startswith("-t") and not x.startswith("--")):
            raise UsageError(f"`{x}` is refused: this tool always runs --target {TARGET} "
                             f"--profiles-dir . in {DBT_PROJECT.name}/ (Snowflake is off limits, H1)")
    return [str(dbt_bin), cmd, "--target", TARGET, "--profiles-dir", "."] + rest


def child_env(parent: Mapping[str, str]) -> Dict[str, str]:
    """dbt's environment: the parent's, minus Snowflake/MinIO settings and the
    read-only key, plus the anonymous-usage opt-outs."""
    env = {k: v for k, v in parent.items()
           if not k.startswith(STRIPPED_PREFIXES) and k not in STRIPPED_NAMES}
    env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    env["DO_NOT_TRACK"] = "1"
    return env


def main(argv: Optional[Sequence[str]] = None, *, project_dir: Path = DBT_PROJECT,
         dbt_bin: Path = DBT_BIN, secrets_dir: Optional[Path] = None, runner=subprocess.call) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    try:
        dbt_argv = build_argv(args, dbt_bin)
    except UsageError as e:
        print(f"error: {e}\n{USAGE}", file=sys.stderr)
        return 2
    _env.load_env(secrets_dir)
    try:
        _env.require(*MOLINIA_VARS, secrets_dir=secrets_dir)
    except _env.EnvError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not Path(project_dir).is_dir():
        print(f"error: dbt project {project_dir} not found", file=sys.stderr)
        return 2
    if not Path(dbt_bin).exists():
        print(f"error: {dbt_bin} not found (the kit venv has dbt-core + dbt-molinia)", file=sys.stderr)
        return 2
    shown = " ".join(["dbt"] + dbt_argv[1:])
    print(f"+ {shown}   (in {Path(project_dir).name}/, key loaded by tools/dbt_molinia.py)", flush=True)
    return runner(dbt_argv, cwd=str(project_dir), env=child_env(os.environ))


if __name__ == "__main__":
    sys.exit(main())
