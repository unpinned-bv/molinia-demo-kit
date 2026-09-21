"""Load the kit's `.secrets/*.env` files into the process environment.

Rules (DESIGN.md, "Tool CLIs"):
  * every tool loads `.secrets/*.env` (the `*.env.example` templates are never
    loaded: their names end in `.example`, not `.env`);
  * values are never printed -- errors name the VARIABLE and the FILE that
    should hold it, never the value;
  * a variable already set in the process environment wins over the file, so
    an orchestrator can override one setting without editing the file.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _engagement  # noqa: E402

KIT_ROOT = Path(__file__).resolve().parent.parent
# MOLINIA_KIT_SECRETS_DIR points the tools at another directory (tests, dry runs).
SECRETS_DIR = Path(os.environ.get("MOLINIA_KIT_SECRETS_DIR") or (KIT_ROOT / ".secrets"))
EXPORTS_DIR = KIT_ROOT / "exports"
# Per client: engagement.yml -> exports/<export_prefix>/, never a constant here.
EXPORT_ROOT = _engagement.ENGAGEMENT.export_root

# A value still holding the template placeholder counts as "not set".
PLACEHOLDER_MARKERS = ("PASTE_HERE",)

# Which file a variable is expected to live in, for error messages.
VARIABLE_HOME = {
    "SNOWFLAKE_ACCOUNT": "snowflake.env",
    "SNOWFLAKE_USER": "snowflake.env",
    "SNOWFLAKE_ROLE": "snowflake.env",
    "SNOWFLAKE_WAREHOUSE": "snowflake.env",
    "SNOWFLAKE_DATABASE": "snowflake.env",
    "SNOWFLAKE_PRIVATE_KEY_PATH": "snowflake.env",
    "MOLINIA_API_URL": "molinia.env",
    "MOLINIA_ORG_ID": "molinia.env",
    "MOLINIA_API_KEY": "molinia.env",
    "MOLINIA_READONLY_KEY": "molinia.env",
    "MINIO_ENDPOINT": "minio.env",
    "MINIO_BUCKET": "minio.env",
    "MINIO_ACCESS_KEY": "minio.env",
    "MINIO_SECRET_KEY": "minio.env",
}


class EnvError(RuntimeError):
    """A required setting is missing. The message never contains a value."""


def parse_env_file(path: Path) -> Dict[str, str]:
    """Parse KEY=VALUE lines. Supports comments, blank lines, `export KEY=...`
    and single/double quoted values. Never logs anything."""
    out: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            # trailing inline comment on an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def load_env(secrets_dir: Optional[Path] = None, override: bool = False) -> List[str]:
    """Load every `<secrets_dir>/*.env` into os.environ.

    Returns the NAMES that were set (never values). Missing directory is not an
    error here -- `require()` reports what is actually missing.
    """
    d = Path(secrets_dir) if secrets_dir is not None else SECRETS_DIR
    loaded: List[str] = []
    if not d.is_dir():
        return loaded
    for f in sorted(d.glob("*.env")):
        if not f.is_file():
            continue
        for key, value in parse_env_file(f).items():
            if override or key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded


def is_placeholder(value: Optional[str]) -> bool:
    return value is None or value.strip() == "" or any(m in value for m in PLACEHOLDER_MARKERS)


def _home(name: str, secrets_dir: Optional[Path]) -> str:
    d = Path(secrets_dir) if secrets_dir is not None else SECRETS_DIR
    fname = VARIABLE_HOME.get(name)
    return str(d / fname) if fname else f"{d}/*.env"


def require(*names: str, secrets_dir: Optional[Path] = None) -> Dict[str, str]:
    """Return {name: value} for every name, or raise EnvError naming each missing
    (or still-placeholder) variable and the file it belongs in."""
    missing: List[str] = []
    values: Dict[str, str] = {}
    for n in names:
        v = os.environ.get(n)
        if is_placeholder(v):
            state = "still the PASTE_HERE placeholder" if v and not is_placeholder_empty(v) else "not set"
            missing.append(f"{n} ({state}; expected in {_home(n, secrets_dir)})")
        else:
            values[n] = v  # type: ignore[assignment]
    if missing:
        raise EnvError("missing required setting(s): " + "; ".join(missing))
    return values


def is_placeholder_empty(value: Optional[str]) -> bool:
    return value is None or value.strip() == ""


def optional(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return default if is_placeholder(v) else v


def has_file(name: str, secrets_dir: Optional[Path] = None) -> bool:
    d = Path(secrets_dir) if secrets_dir is not None else SECRETS_DIR
    return (d / name).is_file()


def env_names_present(names: Iterable[str]) -> List[str]:
    """Names (not values) that are set to a non-placeholder value."""
    return [n for n in names if not is_placeholder(os.environ.get(n))]
