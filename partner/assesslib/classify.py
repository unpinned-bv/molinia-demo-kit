"""The four migration classes, and how an object lands in one.

    AUTOMATIC            the agent ports it with no judgement
    AGENT_PLUS_REVIEW    the agent ports it, a senior checks it
    REDESIGN             a human reworks it (procedural SQL, Python, task->call)
    NOT_SUPPORTED_TODAY  there is no target-side feature at all

The line between the last two: REDESIGN means the work lands on Molinia after a
human rewrites it. NOT_SUPPORTED_TODAY means it does not land on Molinia at all
today, so it is dropped, deferred, or kept outside the platform. That difference
is what a fixed price has to price, so the report never blurs it.

Every classification carries `basis`:
    verified   backed by a fact in facts.py or a rule in agent/MIGRATION_RULES.md
    inference  an engineering judgement this kit has NOT proven
A reader can therefore tell a measured limit from an opinion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

AUTOMATIC = "AUTOMATIC"
AGENT_PLUS_REVIEW = "AGENT_PLUS_REVIEW"
REDESIGN = "REDESIGN"
NOT_SUPPORTED_TODAY = "NOT_SUPPORTED_TODAY"

CLASSES = (AUTOMATIC, AGENT_PLUS_REVIEW, REDESIGN, NOT_SUPPORTED_TODAY)
_SEVERITY = {c: i for i, c in enumerate(CLASSES)}

#: The two classes the report must name object by object.
NAMED_CLASSES = (REDESIGN, NOT_SUPPORTED_TODAY)

VERIFIED = "verified"
INFERENCE = "inference"


def worst(*classes: Optional[str]) -> str:
    """The most expensive class among the arguments (AUTOMATIC if none given)."""
    seen = [c for c in classes if c]
    if not seen:
        return AUTOMATIC
    for c in seen:
        if c not in _SEVERITY:
            raise ValueError(f"unknown class {c!r}")
    return max(seen, key=lambda c: _SEVERITY[c])


def severity(klass: str) -> int:
    return _SEVERITY[klass]


@dataclass
class Item:
    """One inventoried thing, placed in one class."""
    kind: str                 # 'table', 'view', 'procedure', 'dbt model', ...
    name: str                 # fully qualified where it matters
    klass: str
    reason: str               # why it is in that class, in one line
    basis: str = VERIFIED     # VERIFIED | INFERENCE
    rule: str = ""            # MIGRATION_RULES id, or a facts.py id
    detail: Dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "name": self.name, "class": self.klass,
             "reason": self.reason, "basis": self.basis}
        if self.rule:
            d["rule"] = self.rule
        if self.detail:
            d["detail"] = self.detail
        return d


def counts(items: List[Item]) -> Dict[str, int]:
    out = {c: 0 for c in CLASSES}
    for i in items:
        out[i.klass] += 1
    return out


# --------------------------------------------------------------------------- column types

#: Snowflake DATA_TYPE -> (Molinia/DuckDB type, class, basis, rule, note).
#: The mapping is MIGRATION_RULES section 3.2 (N1), 3.3 (D1) and 3.4 (J0).
TYPE_MAP: Dict[str, tuple] = {
    "NUMBER":           ("DECIMAL(p,s)", AUTOMATIC, VERIFIED, "N1", ""),
    "DECIMAL":          ("DECIMAL(p,s)", AUTOMATIC, VERIFIED, "N1", ""),
    "NUMERIC":          ("DECIMAL(p,s)", AUTOMATIC, VERIFIED, "N1", ""),
    "INT":              ("DECIMAL(38,0)", AUTOMATIC, VERIFIED, "N1", "never DuckDB INT: it is 32-bit"),
    "INTEGER":          ("DECIMAL(38,0)", AUTOMATIC, VERIFIED, "N1", "never DuckDB INT: it is 32-bit"),
    "BIGINT":           ("DECIMAL(38,0)", AUTOMATIC, VERIFIED, "N1", ""),
    "SMALLINT":         ("DECIMAL(38,0)", AUTOMATIC, VERIFIED, "N1", ""),
    "TINYINT":          ("DECIMAL(38,0)", AUTOMATIC, VERIFIED, "N1", ""),
    "BYTEINT":          ("DECIMAL(38,0)", AUTOMATIC, VERIFIED, "N1", ""),
    "FLOAT":            ("DOUBLE", AUTOMATIC, VERIFIED, "N1", ""),
    "FLOAT4":           ("DOUBLE", AUTOMATIC, VERIFIED, "N1", ""),
    "FLOAT8":           ("DOUBLE", AUTOMATIC, VERIFIED, "N1", ""),
    "DOUBLE":           ("DOUBLE", AUTOMATIC, VERIFIED, "N1", ""),
    "DOUBLE PRECISION": ("DOUBLE", AUTOMATIC, VERIFIED, "N1", ""),
    "REAL":             ("DOUBLE", AUTOMATIC, VERIFIED, "N1", ""),
    "TEXT":             ("VARCHAR", AUTOMATIC, VERIFIED, "", ""),
    "VARCHAR":          ("VARCHAR", AUTOMATIC, VERIFIED, "", ""),
    "CHAR":             ("VARCHAR", AUTOMATIC, VERIFIED, "", ""),
    "CHARACTER":        ("VARCHAR", AUTOMATIC, VERIFIED, "", ""),
    "STRING":           ("VARCHAR", AUTOMATIC, VERIFIED, "", ""),
    "BOOLEAN":          ("BOOLEAN", AUTOMATIC, VERIFIED, "", ""),
    "DATE":             ("DATE", AUTOMATIC, VERIFIED, "", ""),
    "TIME":             ("TIME", AUTOMATIC, VERIFIED, "", ""),
    "BINARY":           ("BLOB", AUTOMATIC, INFERENCE, "", "not exercised by this kit"),
    "VARBINARY":        ("BLOB", AUTOMATIC, INFERENCE, "", "not exercised by this kit"),
    "DATETIME":         ("TIMESTAMP", AUTOMATIC, VERIFIED, "D1", "unloads UTC-adjusted"),
    "TIMESTAMP":        ("TIMESTAMP", AUTOMATIC, VERIFIED, "D1", "unloads UTC-adjusted"),
    "TIMESTAMP_NTZ":    ("TIMESTAMP", AUTOMATIC, VERIFIED, "D1",
                         "Snowflake unloads it as a UTC-adjusted Parquet timestamp"),
    "TIMESTAMP_LTZ":    ("TIMESTAMPTZ", AGENT_PLUS_REVIEW, VERIFIED, "D1",
                         "depends on a session time zone: every reader must agree on it"),
    "TIMESTAMP_TZ":     ("TIMESTAMPTZ", AGENT_PLUS_REVIEW, VERIFIED, "D1",
                         "depends on a session time zone: every reader must agree on it"),
    "VARIANT":          ("VARCHAR holding JSON text", AGENT_PLUS_REVIEW, VERIFIED, "J0",
                         "every `v:path` expression over it must be rewritten to `v ->> '$.path'`"),
    "OBJECT":           ("VARCHAR holding JSON text", AGENT_PLUS_REVIEW, VERIFIED, "J0",
                         "every path expression over it must be rewritten"),
    "ARRAY":            ("VARCHAR holding JSON text", AGENT_PLUS_REVIEW, VERIFIED, "J0",
                         "FLATTEN over it becomes unnest(...) WITH ORDINALITY"),
    "GEOGRAPHY":        ("none", NOT_SUPPORTED_TODAY, INFERENCE, "",
                         "no geospatial type has been moved or queried by this kit"),
    "GEOMETRY":         ("none", NOT_SUPPORTED_TODAY, INFERENCE, "",
                         "no geospatial type has been moved or queried by this kit"),
    "VECTOR":           ("none", NOT_SUPPORTED_TODAY, INFERENCE, "",
                         "no vector type and no vector search on the query path"),
    "FILE":             ("none", NOT_SUPPORTED_TODAY, INFERENCE, "", "no FILE type"),
}

_UNKNOWN_TYPE = ("unknown", AGENT_PLUS_REVIEW, INFERENCE, "",
                 "this type is not in the kit's mapping table: check it by hand before quoting")


def map_type(data_type: str) -> tuple:
    """(molinia_type, class, basis, rule, note) for a Snowflake DATA_TYPE."""
    key = (data_type or "").strip().upper()
    key = key.split("(", 1)[0].strip()
    return TYPE_MAP.get(key, _UNKNOWN_TYPE)


# --------------------------------------------------------------------------- Snowflake objects

_PROCEDURAL_MARKERS = (
    "execute immediate", "declare", "begin", " let ", "for ", "while ", "loop",
    "return ", "call ", "exception",
)


def looks_procedural(body: Optional[str]) -> bool:
    """True when a SQL procedure body is Snowflake Scripting rather than a single
    statement. Conservative: a body we cannot read counts as procedural, because
    a procedure that is only one statement is the rare case."""
    if not body:
        return True
    low = " " + " ".join(body.lower().split()) + " "
    return any(m in low for m in _PROCEDURAL_MARKERS)


def classify_routine(kind: str, qualified_name: str, language: Optional[str],
                     body: Optional[str]) -> Item:
    """A stored procedure or user-defined function."""
    lang = (language or "").strip().upper() or "UNKNOWN"
    if lang in ("PYTHON", "JAVASCRIPT", "JAVA", "SCALA"):
        # REDESIGN, not NOT_SUPPORTED_TODAY: the routine's *language* has no
        # runtime on Molinia, but its logic normally lands there once a human
        # rewrites it as SQL — which is exactly this file's definition of
        # REDESIGN, and the same class the adjacent dbt Python model gets.
        # Only the residue that cannot be expressed in SQL leaves the platform.
        return Item(kind, qualified_name, REDESIGN,
                    f"{lang.title()} {kind}: Python, JavaScript, Java and Scala routines have no "
                    f"runtime on Molinia. A human rewrites the logic as SQL; whatever cannot be "
                    f"expressed in SQL leaves the platform, and that part has to be re-scoped.",
                    VERIFIED, "F-NOTPORT", {"language": lang})
    if kind == "function":
        return Item(kind, qualified_name, AGENT_PLUS_REVIEW,
                    "SQL function: Molinia has no verified user-defined-function surface, so the body is "
                    "inlined into the callers or becomes a view. Mechanical, but it changes every caller.",
                    INFERENCE, "", {"language": lang})
    if looks_procedural(body):
        detail = {"language": lang}
        if not body:
            detail["definition_readable"] = False
        return Item(kind, qualified_name, REDESIGN,
                    "Snowflake Scripting control flow: Molinia procedures are statement batches, so "
                    "IF/FOR/LOOP/DECLARE/EXCEPTION has to be rebuilt outside the procedure"
                    + ("" if body else " (body not readable with this role: assumed procedural)") + ".",
                    VERIFIED, "F-NOTPORT", detail)
    return Item(kind, qualified_name, AGENT_PLUS_REVIEW,
                "single-statement SQL procedure: portable as a statement batch, but every caller "
                "(task, pipeline step) has to be checked — CALL resolves only on the single-statement "
                "query path.", VERIFIED, "F-NOTPORT", {"language": lang})


def classify_table(kind: str, qualified_name: str, worst_column: str,
                   column_notes: List[str]) -> Item:
    """A base table. `worst_column` is the worst class among its columns."""
    if worst_column == NOT_SUPPORTED_TODAY:
        return Item(kind, qualified_name, NOT_SUPPORTED_TODAY,
                    "holds a column type with no Molinia equivalent: " + "; ".join(column_notes[:3]),
                    INFERENCE, "", {"columns": column_notes})
    if worst_column == AGENT_PLUS_REVIEW:
        return Item(kind, qualified_name, AGENT_PLUS_REVIEW,
                    "moves by COPY INTO + ingest, but a column type changes shape on the way: "
                    + "; ".join(column_notes[:3]),
                    VERIFIED, "F-UNLOAD", {"columns": column_notes})
    return Item(kind, qualified_name, AUTOMATIC,
                "moves by COPY INTO Parquet + ingest; every column type maps one to one.",
                VERIFIED, "F-NOREADER")
