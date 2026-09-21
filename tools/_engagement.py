"""Load `engagement.yml`: every value in the kit that changes per client.

Rules (DESIGN.md, "Tool CLIs"):
  * one file describes the engagement; no tool under tools/ hardcodes a client
    value, so re-pointing the kit at another client is a config change;
  * MOLINIA_KIT_ENGAGEMENT points at another file (a second client, or a test);
  * the built-in defaults ARE the acme_shop demo, so a missing file degrades to
    the demo rather than to a crash -- it warns once on stderr and carries on;
  * a file that IS present must be valid: a bad value raises EngagementError
    with the key named. Loading fails loudly, running never does.

The validation is not decoration. `tools/molinia.py reset` drops every schema
in `dbt_schemas`, so listing `main` there would drop the Snowflake answer keys
that parity compares against. That is refused here, at load time.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

KIT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = Path(os.environ.get("MOLINIA_KIT_ENGAGEMENT") or (KIT_ROOT / "engagement.yml"))

# Schemas that must never appear in dbt_schemas: dropping one loses the answer
# keys (main) or breaks the catalog. Shared with tools/molinia.py.
PROTECTED_SCHEMAS = frozenset({"main", "information_schema", "pg_catalog", "temp", "system"})
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
PREFIX_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
# <database>.<schema>.<stage>, Snowflake's unquoted identifier shape.
STAGE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){2}$")

DEFAULTS: Dict[str, Any] = {
    "name": "acme-shop-demo",
    "project_dir": "acme_shop",
    "export_prefix": "acme-snowflake-export",
    "snowflake": {"stage": "MOLINIA_DEMO.PUBLIC.MOLINIA_EXPORT"},
    "molinia": {
        "source_schema": "main",
        "dbt_schemas": ["staging", "intermediate", "marts"],
        "raw_prefix": "raw_",
        "answer_key_prefix": "sf_",
        "data_source_id": 1,
        "location_id": 1,
    },
    "forbidden_tables": ["main.raw_customer_contacts"],
    "service_accounts": {"build": "demo-dbt-build", "readonly": "demo-agent-readonly"},
}

_WARNED = False


class EngagementError(ValueError):
    """A value in engagement.yml is missing or unusable. Names the key."""


# --------------------------------------------------------------------------- helpers

def _warn_once(message: str) -> None:
    global _WARNED
    if not _WARNED:
        print(f"warning: {message}", file=sys.stderr)
        _WARNED = True


def _section(doc: Mapping[str, Any], key: str) -> Dict[str, Any]:
    """doc[key] merged over DEFAULTS[key]; a non-mapping is an error."""
    base = dict(DEFAULTS[key])
    got = doc.get(key, {})
    if got is None:
        return base
    if not isinstance(got, Mapping):
        raise EngagementError(f"{key}: expected a mapping, got {type(got).__name__}")
    base.update({str(k): v for k, v in got.items()})
    return base


def _text(value: Any, key: str) -> str:
    if value is None or not str(value).strip():
        raise EngagementError(f"{key}: must not be empty")
    if not isinstance(value, str):
        raise EngagementError(f"{key}: expected a string, got {type(value).__name__}")
    return value.strip()


def _ident(value: Any, key: str) -> str:
    v = _text(value, key).lower()
    if not IDENT_RE.match(v):
        raise EngagementError(f"{key}: {value!r} is not a plain lower-case identifier")
    return v


def _prefix(value: Any, key: str) -> str:
    v = _text(value, key).lower()
    if not PREFIX_RE.match(v):
        raise EngagementError(f"{key}: {value!r} is not usable as a table-name prefix")
    return v


def _positive_int(value: Any, key: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise EngagementError(f"{key}: expected an integer, got {value!r}") from None
    if n <= 0:
        raise EngagementError(f"{key}: expected a positive integer, got {n}")
    return n


def _str_list(value: Any, key: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):
        raise EngagementError(f"{key}: expected a list, got {type(value).__name__}")
    return [_text(v, f"{key}[{i}]") for i, v in enumerate(value)]


def _qualify(name: str, default_schema: str) -> str:
    """'raw_customer_contacts' -> 'main.raw_customer_contacts'; lower-cased."""
    n = name.strip().lower().replace('"', "")
    return n if "." in n else f"{default_schema}.{n}"


# --------------------------------------------------------------------------- model

@dataclass(frozen=True)
class Engagement:
    """Resolved engagement settings. Paths are absolute; names are lower-case."""

    name: str
    project_dir: Path
    export_prefix: str
    snowflake_stage: str
    source_schema: str
    dbt_schemas: Tuple[str, ...]
    raw_prefix: str
    answer_key_prefix: str
    data_source_id: int
    location_id: int
    forbidden_tables: Tuple[str, ...]
    service_account_build: str
    service_account_readonly: str
    path: Optional[Path] = None          # the file it came from, None = defaults
    kit_root: Path = field(default=KIT_ROOT)

    # -- derived ----------------------------------------------------------

    @property
    def from_file(self) -> bool:
        return self.path is not None

    @property
    def exports_dir(self) -> Path:
        return self.kit_root / "exports"

    @property
    def export_root(self) -> Path:
        """exports/<export_prefix>/ -- where sf.py writes and molinia.py reads."""
        return self.exports_dir / self.export_prefix

    @property
    def target_prefix(self) -> Dict[str, str]:
        """Export kind -> table-name prefix in the source schema."""
        return {"raw": self.raw_prefix, "expected": self.answer_key_prefix}

    @property
    def schema_order(self) -> Dict[str, int]:
        """Display order: the source schema first, then the dbt schemas."""
        return {s: i for i, s in enumerate((self.source_schema,) + self.dbt_schemas)}

    def raw_table(self, table: str) -> str:
        return f"{self.source_schema}.{self.raw_prefix}{table}"

    def answer_key(self, model: str) -> str:
        return f"{self.source_schema}.{self.answer_key_prefix}{model}"

    def is_forbidden(self, name: str) -> bool:
        """True for a table rule H2 puts off limits, qualified or bare."""
        return _qualify(name, self.source_schema) in self.forbidden_tables

    def schema_list(self, sep: str = " / ") -> str:
        return sep.join(self.dbt_schemas)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "project_dir": str(self.project_dir),
            "export_prefix": self.export_prefix,
            "snowflake": {"stage": self.snowflake_stage},
            "molinia": {
                "source_schema": self.source_schema,
                "dbt_schemas": list(self.dbt_schemas),
                "raw_prefix": self.raw_prefix,
                "answer_key_prefix": self.answer_key_prefix,
                "data_source_id": self.data_source_id,
                "location_id": self.location_id,
            },
            "forbidden_tables": list(self.forbidden_tables),
            "service_accounts": {"build": self.service_account_build,
                                 "readonly": self.service_account_readonly},
        }


# --------------------------------------------------------------------------- load

def from_doc(doc: Mapping[str, Any], *, path: Optional[Path] = None,
             kit_root: Path = KIT_ROOT) -> Engagement:
    """Validate a parsed document into an Engagement. Raises EngagementError."""
    if not isinstance(doc, Mapping):
        raise EngagementError(f"expected a YAML mapping at the top level, got {type(doc).__name__}")

    sf = _section(doc, "snowflake")
    mol = _section(doc, "molinia")
    sa = _section(doc, "service_accounts")

    stage = _text(sf.get("stage"), "snowflake.stage")
    if not STAGE_RE.match(stage):
        raise EngagementError(
            f"snowflake.stage: {stage!r} is not <database>.<schema>.<stage> "
            "(tools/sf.py matches COPY INTO targets against it, so a wrong value unloads nothing)")

    source_schema = _ident(mol.get("source_schema"), "molinia.source_schema")

    raw_schemas = _str_list(mol.get("dbt_schemas"), "molinia.dbt_schemas")
    if not raw_schemas:
        raise EngagementError("molinia.dbt_schemas: must list at least one schema")
    dbt_schemas: List[str] = []
    for i, s in enumerate(raw_schemas):
        v = _ident(s, f"molinia.dbt_schemas[{i}]")
        if v in PROTECTED_SCHEMAS:
            raise EngagementError(
                f"molinia.dbt_schemas[{i}]: {v!r} is protected. `tools/molinia.py reset` drops "
                "every schema listed here, which would destroy the ingested sources and the "
                "Snowflake answer keys parity compares against.")
        if v == source_schema:
            raise EngagementError(
                f"molinia.dbt_schemas[{i}]: {v!r} is also molinia.source_schema; the dbt output "
                "would overwrite the ingested sources.")
        if v in dbt_schemas:
            raise EngagementError(f"molinia.dbt_schemas[{i}]: {v!r} listed twice")
        dbt_schemas.append(v)

    raw_prefix = _prefix(mol.get("raw_prefix"), "molinia.raw_prefix")
    answer_key_prefix = _prefix(mol.get("answer_key_prefix"), "molinia.answer_key_prefix")
    if raw_prefix == answer_key_prefix:
        raise EngagementError(
            "molinia.raw_prefix and molinia.answer_key_prefix are both "
            f"{raw_prefix!r}; the ingested sources and the answer keys would collide")
    if raw_prefix.startswith(answer_key_prefix) or answer_key_prefix.startswith(raw_prefix):
        raise EngagementError(
            f"molinia.raw_prefix {raw_prefix!r} and molinia.answer_key_prefix "
            f"{answer_key_prefix!r}: one is a prefix of the other, so `status` cannot tell "
            "an ingested source from an answer key")

    project = Path(_text(doc.get("project_dir", DEFAULTS["project_dir"]), "project_dir"))
    if not project.is_absolute():
        project = kit_root / project

    export_prefix = _text(doc.get("export_prefix", DEFAULTS["export_prefix"]), "export_prefix")
    if export_prefix.strip("/") != export_prefix or "\\" in export_prefix:
        raise EngagementError(
            f"export_prefix: {export_prefix!r} must be a bare object-key prefix "
            "(no leading or trailing '/', no backslash)")

    forbidden = tuple(sorted({
        _qualify(t, source_schema)
        for t in _str_list(doc.get("forbidden_tables", DEFAULTS["forbidden_tables"]),
                           "forbidden_tables")
    }))

    return Engagement(
        name=_text(doc.get("name", DEFAULTS["name"]), "name"),
        project_dir=project,
        export_prefix=export_prefix,
        snowflake_stage=stage,
        source_schema=source_schema,
        dbt_schemas=tuple(dbt_schemas),
        raw_prefix=raw_prefix,
        answer_key_prefix=answer_key_prefix,
        data_source_id=_positive_int(mol.get("data_source_id"), "molinia.data_source_id"),
        location_id=_positive_int(mol.get("location_id"), "molinia.location_id"),
        forbidden_tables=forbidden,
        service_account_build=_text(sa.get("build"), "service_accounts.build"),
        service_account_readonly=_text(sa.get("readonly"), "service_accounts.readonly"),
        path=path,
        kit_root=kit_root,
    )


def load(path: Optional[Path] = None, *, kit_root: Path = KIT_ROOT,
         required: bool = False) -> Engagement:
    """Read engagement.yml (or `path`). A missing file falls back to the demo
    defaults unless `required`; a present-but-invalid file always raises."""
    p = Path(path) if path is not None else DEFAULT_PATH
    if not p.is_file():
        if required:
            raise EngagementError(f"engagement file not found: {p}")
        _warn_once(f"{p} not found; using the built-in acme_shop demo defaults")
        return from_doc(dict(DEFAULTS), path=None, kit_root=kit_root)
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is a kit dependency
        raise EngagementError(
            f"{p} exists but PyYAML is not installed; `pip install pyyaml` in the kit venv") from None
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EngagementError(f"{p}: not valid YAML: {' '.join(str(exc).split())[:200]}") from None
    return from_doc(doc or {}, path=p, kit_root=kit_root)


#: The engagement every tool uses. Tests call load() with their own file.
ENGAGEMENT = load()
