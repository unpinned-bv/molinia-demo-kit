"""Static scan of a dbt project: no `dbt run`, no `dbt parse`, no connection.

It reads the files on disk, so it works on a client's repository before anyone
has credentials, and it reports every Snowflake-only construct as `file:line`
so a reviewer can jump to it.

Two things it deliberately does NOT do:
  * render Jinja — a macro body is scanned where it lives, and the report says
    which models call it, so a `DIV0` hidden in a helper is counted once and
    attributed to every caller;
  * resolve package macros — `dbt_packages/` is scanned only if it is checked
    out, and the report says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from . import rules as R
from .classify import (AGENT_PLUS_REVIEW, AUTOMATIC, NOT_SUPPORTED_TODAY, REDESIGN,
                       severity as severity_of, worst)

# --------------------------------------------------------------------------- masking


def blank_sql(text: str, keep_strings: bool = False) -> str:
    """Return `text` with comments (and, by default, string literals) blanked.

    Lengths and newlines are preserved, so every offset still maps to the same
    line and column. Scanning the masked text is what keeps `-- listagg is
    gone now` out of the counts.

    `keep_strings=True` blanks only the comments, for the few rules whose
    construct lives inside a literal — `ROUND(x, 2, 'HALF_TO_EVEN')` is one, and
    blanking the literal would hide the only thing that makes it unportable.
    """
    out = list(text)
    n = len(text)
    i = 0
    state = None  # None | 'line' | 'block' | 'jinja' | 'squote' | 'dquote' | 'dollar'
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if ch == "-" and nxt == "-":
                state = "line"
            elif ch == "/" and nxt == "*":
                state = "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            elif ch == "{" and nxt == "#":
                state = "jinja"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            elif ch == "'" and not keep_strings:
                state = "squote"
                out[i] = " "
            elif ch == "$" and nxt == "$":
                state = "dollar"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if state in ("line", "squote"):
                out[i] = " "
            i += 1
            continue
        # inside something
        if state == "line":
            if ch == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if ch == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = None
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        if state == "jinja":
            if ch == "#" and nxt == "}":
                out[i] = out[i + 1] = " "
                i += 2
                state = None
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        if state == "dollar":
            if ch == "$" and nxt == "$":
                out[i] = out[i + 1] = " "
                i += 2
                state = None
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        if state == "squote":
            if ch == "'":
                out[i] = " "
                if nxt == "'":          # '' escape inside the literal
                    out[i + 1] = " "
                    i += 2
                    continue
                state = None
                i += 1
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
    return "".join(out)


_JINJA_SPAN = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def jinja_spans(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _JINJA_SPAN.finditer(text)]


def _in_spans(pos: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def line_index(text: str) -> List[int]:
    """Start offset of every line, for offset -> line number."""
    starts = [0]
    for m in re.finditer(r"\n", text):
        starts.append(m.end())
    return starts


def line_of(offset: int, starts: Sequence[int]) -> int:
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


# --------------------------------------------------------------------------- hits


@dataclass
class Hit:
    rule: R.Rule
    path: str          # relative to the project root
    line: int
    text: str          # the matched text, trimmed

    def as_dict(self) -> dict:
        return {"rule": self.rule.id, "construct": self.rule.construct, "class": self.rule.klass,
                "file": self.path, "line": self.line, "match": self.text,
                "basis": self.rule.basis, "precision": self.rule.precision,
                "counts": self.rule.weight}


def scan_text(text: str, path: str, context: str = "sql") -> List[Hit]:
    """Every rule hit in one file's text."""
    sql_like = context in ("sql", "proc")
    masked = blank_sql(text) if sql_like else text
    with_strings = blank_sql(text, keep_strings=True) if sql_like else text
    spans = jinja_spans(text) if sql_like else []
    starts = line_index(text)
    hits: List[Hit] = []
    flags = re.IGNORECASE | (re.MULTILINE if context == "yaml" else 0)
    for rule in R.rules_for(context):
        pattern = re.compile(rule.pattern, flags)
        haystack = with_strings if rule.needs_strings else masked
        for m in pattern.finditer(haystack):
            if rule.skip_jinja and _in_spans(m.start(), spans):
                continue
            frag = text[m.start():m.end()]
            hits.append(Hit(rule, path, line_of(m.start(), starts), " ".join(frag.split())[:80]))
    return hits


# --------------------------------------------------------------------------- Y4 alias shadowing

_ALIAS_AS = re.compile(r"[^a-z0-9_]as[ \t]+([a-z_][a-z0-9_]*)")
_SELECT = re.compile(r"(^|[^a-z0-9_])select([^a-z0-9_]|$)")
_TYPE_NAMES = {
    "varchar", "decimal", "numeric", "number", "timestamp", "timestamptz", "date", "double",
    "float", "real", "bigint", "hugeint", "integer", "int", "boolean", "json", "text", "string",
}


def alias_shadow_lines(text: str) -> List[Tuple[int, str]]:
    """Ports the Y4 scan from MIGRATION_RULES section 9 (the awk one-liner).

    Returns (line number, alias) candidates: a name used in the same select list
    where it was just aliased. DuckDB resolves a lateral column alias and
    Snowflake does not, so both a real bug and a harmless repeat look the same.
    Candidates, not defects — the rulebook says to read each one.
    """
    out: List[Tuple[int, str]] = []
    aliases: Dict[str, bool] = {}
    for n, raw in enumerate(blank_sql(text).splitlines(), 1):
        low = raw.lower()
        if _SELECT.search(low):
            aliases = {}
        stripped = _ALIAS_AS.sub(" ", low)
        for name in aliases:
            if re.search(r"(^|[^a-z0-9_.])" + re.escape(name) + r"([^a-z0-9_.(]|$)", stripped):
                out.append((n, name))
        for m in _ALIAS_AS.finditer(low):
            name = m.group(1)
            if name not in _TYPE_NAMES:
                aliases[name] = True
    return out


_REGEX_LIT = re.compile(r"\bregexp_\w+\s*\([^)]{0,400}?'((?:[^']|'')*)'", re.IGNORECASE | re.DOTALL)


def regex_backslash_lines(text: str) -> List[Tuple[int, str]]:
    """Rule X1: a Snowflake regex literal with `\\\\d` becomes `\\d` on DuckDB.
    A straight port silently matches nothing, so every literal is listed."""
    starts = line_index(text)
    out = []
    for m in _REGEX_LIT.finditer(text):
        if "\\" in m.group(1):
            out.append((line_of(m.start(), starts), m.group(1)[:60]))
    return out


# --------------------------------------------------------------------------- dbt project model


@dataclass
class Node:
    kind: str                       # model | test | seed | snapshot | macro | analysis
    name: str
    path: str
    materialized: str = ""
    schema: str = ""
    hits: List[Hit] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    klass: str = AUTOMATIC
    reason: str = ""
    key: List[str] = field(default_factory=list)
    key_source: str = ""
    language: str = "sql"

    def as_dict(self) -> dict:
        d = {"kind": self.kind, "name": self.name, "file": self.path, "class": self.klass,
             "reason": self.reason}
        if self.materialized:
            d["materialized"] = self.materialized
        if self.schema:
            d["schema"] = self.schema
        if self.language != "sql":
            d["language"] = self.language
        if self.flags:
            d["flags"] = self.flags
        if self.key:
            d["key"] = self.key
            d["key_source"] = self.key_source
        if self.hits:
            d["rule_hits"] = sorted({h.rule.id for h in self.hits})
        return d


@dataclass
class Project:
    root: Path
    name: str = ""
    ok: bool = False
    error: str = ""
    profile: str = ""
    dbt_version: str = ""
    model_paths: List[str] = field(default_factory=list)
    nodes: List[Node] = field(default_factory=list)
    macros: List[Node] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)
    tests: List[dict] = field(default_factory=list)
    packages: List[dict] = field(default_factory=list)
    packages_installed: bool = False
    target_name: str = ""
    target_schema: str = ""
    target_database: str = ""
    has_molinia_target: bool = False
    project_config_hits: List[Hit] = field(default_factory=list)
    alias_candidates: List[dict] = field(default_factory=list)
    regex_literals: List[dict] = field(default_factory=list)
    macro_callers: Dict[str, List[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def models(self) -> List[Node]:
        return [n for n in self.nodes if n.kind == "model"]


def _yaml_load(path: Path) -> object:
    import yaml
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:                      # a client's YAML can be broken
        raise ValueError(f"{path.name}: {exc}") from None


def _cfg_tree(project_cfg: dict, project_name: str, parts: Sequence[str]) -> dict:
    """Inherited `models:` config for a path, deepest `+key` wins."""
    node = (project_cfg.get("models") or {}).get(project_name)
    acc: Dict[str, object] = {}
    if not isinstance(node, dict):
        return acc
    for k, v in node.items():
        if k.startswith("+"):
            acc[k[1:]] = v
    for part in parts:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            break
        for k, v in node.items():
            if k.startswith("+"):
                acc[k[1:]] = v
    return acc


_CONFIG_CALL = re.compile(r"\{\{\s*config\s*\((.*?)\)\s*\}\}", re.DOTALL | re.IGNORECASE)
_KWARG = re.compile(r"(\w+)\s*=\s*['\"]([\w-]+)['\"]")
_MACRO_DEF = re.compile(r"\{%-?\s*macro\s+([A-Za-z_]\w*)\s*\(", re.IGNORECASE)
_SNAPSHOT_BLOCK = re.compile(r"\{%-?\s*snapshot\s+([A-Za-z_]\w*)", re.IGNORECASE)


def _inline_config(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for m in _CONFIG_CALL.finditer(text):
        for k, v in _KWARG.findall(m.group(1)):
            out[k] = v
    return out


def _walk_sql(root: Path, subdirs: Iterable[str]) -> List[Path]:
    found: List[Path] = []
    for sub in subdirs:
        d = root / sub
        if d.is_dir():
            found += sorted(p for p in d.rglob("*.sql") if p.is_file())
    return found


def _rel(root: Path, p: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def _test_entries(block: object, owner: str, path: str, kind: str) -> List[dict]:
    """Flatten `data_tests:` / `tests:` under a model, source or column."""
    out: List[dict] = []
    if not isinstance(block, list):
        return out
    for entry in block:
        if isinstance(entry, str):
            out.append({"type": entry, "on": owner, "file": path, "level": kind, "args": {}})
        elif isinstance(entry, dict):
            for name, args in entry.items():
                args = args or {}
                if isinstance(args, dict) and isinstance(args.get("arguments"), dict):
                    args = {**{k: v for k, v in args.items() if k != "arguments"},
                            **args["arguments"]}
                out.append({"type": name, "on": owner, "file": path, "level": kind,
                            "args": args if isinstance(args, dict) else {}})
    return out


def _tests_of(entry: dict) -> object:
    return entry.get("data_tests", entry.get("tests"))


def load_project(root: Path) -> Project:
    """Read a dbt project from disk. Never raises: `Project.ok` says whether the
    rest of the fields mean anything."""
    proj = Project(root=root)
    cfg_path = root / "dbt_project.yml"
    if not root.is_dir():
        proj.error = f"{root} is not a directory"
        return proj
    if not cfg_path.is_file():
        proj.error = (f"{root} has no dbt_project.yml — point --dbt-project at the directory that "
                      f"holds it (the repository root of the dbt project, not the models/ folder)")
        return proj
    try:
        cfg = _yaml_load(cfg_path) or {}
    except ValueError as exc:
        proj.error = f"dbt_project.yml could not be parsed: {exc}"
        return proj
    if not isinstance(cfg, dict):
        proj.error = "dbt_project.yml did not parse to a mapping"
        return proj

    proj.ok = True
    proj.name = str(cfg.get("name") or root.name)
    proj.profile = str(cfg.get("profile") or "")
    proj.dbt_version = str(cfg.get("require-dbt-version") or "")
    proj.model_paths = [str(p) for p in (cfg.get("model-paths") or ["models"])]
    macro_paths = [str(p) for p in (cfg.get("macro-paths") or ["macros"])]
    test_paths = [str(p) for p in (cfg.get("test-paths") or ["tests"])]
    seed_paths = [str(p) for p in (cfg.get("seed-paths") or ["seeds"])]
    snapshot_paths = [str(p) for p in (cfg.get("snapshot-paths") or ["snapshots"])]
    analysis_paths = [str(p) for p in (cfg.get("analysis-paths") or ["analyses"])]

    # dbt_project.yml itself carries Snowflake-only configs (P2).
    proj.project_config_hits = scan_text(cfg_path.read_text(encoding="utf-8", errors="replace"),
                                         "dbt_project.yml", "yaml")

    # ---- models -----------------------------------------------------------
    for path in _walk_sql(root, proj.model_paths):
        rel = _rel(root, path)
        text = path.read_text(encoding="utf-8", errors="replace")
        parts = list(path.relative_to(root).parts)[1:-1]
        inherited = _cfg_tree(cfg, proj.name, parts)
        inline = _inline_config(text)
        node = Node("model", path.stem, rel,
                    materialized=str(inline.get("materialized")
                                     or inherited.get("materialized") or "view"),
                    schema=str(inline.get("schema") or inherited.get("schema") or ""))
        node.hits = scan_text(text, rel, "sql")
        if inherited.get("cluster_by") or "cluster_by" in inline:
            node.flags.append("cluster_by config (P2: delete it)")
        proj.nodes.append(node)

    # Python models
    for mp in proj.model_paths:
        d = root / mp
        if d.is_dir():
            for path in sorted(d.rglob("*.py")):
                rel = _rel(root, path)
                node = Node("model", path.stem, rel, materialized="python", language="python")
                node.flags.append("Python model")
                proj.nodes.append(node)

    # ---- singular tests ---------------------------------------------------
    for path in _walk_sql(root, test_paths):
        rel = _rel(root, path)
        text = path.read_text(encoding="utf-8", errors="replace")
        node = Node("test", path.stem, rel)
        node.hits = scan_text(text, rel, "sql")
        proj.nodes.append(node)
        proj.tests.append({"type": "singular", "on": path.stem, "file": rel, "level": "file",
                           "args": {}})

    # ---- analyses ---------------------------------------------------------
    for path in _walk_sql(root, analysis_paths):
        rel = _rel(root, path)
        text = path.read_text(encoding="utf-8", errors="replace")
        node = Node("analysis", path.stem, rel)
        node.hits = scan_text(text, rel, "sql")
        proj.nodes.append(node)

    # ---- macros -----------------------------------------------------------
    macro_names: List[str] = []
    for path in _walk_sql(root, macro_paths):
        rel = _rel(root, path)
        text = path.read_text(encoding="utf-8", errors="replace")
        defined = _MACRO_DEF.findall(text)
        macro_names += defined
        node = Node("macro", ", ".join(defined) or path.stem, rel)
        node.hits = scan_text(text, rel, "sql")
        proj.macros.append(node)

    # which models call which macro (a rewrite in a macro touches every caller)
    if macro_names:
        model_text = {n.path: (root / n.path).read_text(encoding="utf-8", errors="replace")
                      for n in proj.nodes if n.language == "sql" and (root / n.path).is_file()}
        for macro in sorted(set(macro_names)):
            callers = [p for p, t in model_text.items()
                       if re.search(r"(?<![\w.])" + re.escape(macro) + r"\s*\(", t)]
            if callers:
                proj.macro_callers[macro] = sorted(callers)

    # ---- seeds and snapshots ---------------------------------------------
    for sp in seed_paths:
        d = root / sp
        if d.is_dir():
            for path in sorted(d.rglob("*.csv")):
                proj.nodes.append(Node("seed", path.stem, _rel(root, path)))
    for sp in snapshot_paths:
        d = root / sp
        if d.is_dir():
            for path in sorted(d.rglob("*.sql")):
                rel = _rel(root, path)
                text = path.read_text(encoding="utf-8", errors="replace")
                for name in _SNAPSHOT_BLOCK.findall(text) or [path.stem]:
                    node = Node("snapshot", name, rel)
                    node.hits = scan_text(text, rel, "sql")
                    proj.nodes.append(node)
    # dbt >= 1.9 also allows snapshots declared in YAML
    # ---- YAML: sources, tests, configs ------------------------------------
    yaml_files: List[Path] = []
    for sub in list(proj.model_paths) + snapshot_paths + seed_paths + ["."]:
        d = root / sub
        if d.is_dir():
            yaml_files += [p for p in (d.rglob("*.yml") if sub != "." else d.glob("*.yml"))]
            yaml_files += [p for p in (d.rglob("*.yaml") if sub != "." else d.glob("*.yaml"))]
    seen_yaml = set()
    model_yaml_keys: Dict[str, dict] = {}
    # profiles.yml is a connection file, not project config: its `database:` key
    # is not the source `database:` that rule P3 is about.
    skip_yaml = {"dbt_project.yml", "profiles.yml", "selectors.yml"}
    for path in sorted(set(yaml_files)):
        rel = _rel(root, path)
        if rel in seen_yaml or rel in skip_yaml:
            continue
        seen_yaml.add(rel)
        text = path.read_text(encoding="utf-8", errors="replace")
        proj.project_config_hits += scan_text(text, rel, "yaml")
        try:
            doc = _yaml_load(path)
        except ValueError as exc:
            proj.warnings.append(f"could not parse {rel}: {exc}")
            continue
        if not isinstance(doc, dict):
            continue
        for src in doc.get("sources") or []:
            if not isinstance(src, dict):
                continue
            tables = []
            for t in src.get("tables") or []:
                if not isinstance(t, dict):
                    continue
                meta = (t.get("config") or {}).get("meta") if isinstance(t.get("config"), dict) \
                    else None
                meta = meta if isinstance(meta, dict) else (t.get("meta")
                                                            if isinstance(t.get("meta"), dict)
                                                            else {})
                tables.append({"name": t.get("name"), "identifier": t.get("identifier"),
                               "meta": meta})
                proj.tests += _test_entries(_tests_of(t), f"source:{src.get('name')}.{t.get('name')}",
                                            rel, "source")
                for col in t.get("columns") or []:
                    if isinstance(col, dict):
                        proj.tests += _test_entries(
                            _tests_of(col),
                            f"source:{src.get('name')}.{t.get('name')}.{col.get('name')}",
                            rel, "source_column")
            proj.sources.append({"name": src.get("name"), "database": src.get("database"),
                                 "schema": src.get("schema"), "file": rel, "tables": tables})
        for mdl in doc.get("models") or []:
            if not isinstance(mdl, dict):
                continue
            name = str(mdl.get("name") or "")
            model_yaml_keys[name] = mdl
            proj.tests += _test_entries(_tests_of(mdl), name, rel, "model")
            for col in mdl.get("columns") or []:
                if isinstance(col, dict):
                    proj.tests += _test_entries(_tests_of(col), f"{name}.{col.get('name')}", rel,
                                                "column")
        for snp in doc.get("snapshots") or []:
            if isinstance(snp, dict) and snp.get("name"):
                if not any(n.kind == "snapshot" and n.name == snp["name"] for n in proj.nodes):
                    proj.nodes.append(Node("snapshot", str(snp["name"]), rel))
        for sd in doc.get("seeds") or []:
            if isinstance(sd, dict) and sd.get("name"):
                if not any(n.kind == "seed" and n.name == sd["name"] for n in proj.nodes):
                    proj.nodes.append(Node("seed", str(sd["name"]), rel))

    # ---- packages ---------------------------------------------------------
    for fname in ("packages.yml", "dependencies.yml"):
        p = root / fname
        if p.is_file():
            try:
                doc = _yaml_load(p) or {}
            except ValueError as exc:
                proj.warnings.append(f"could not parse {fname}: {exc}")
                continue
            for pkg in (doc.get("packages") or []) if isinstance(doc, dict) else []:
                if isinstance(pkg, dict):
                    proj.packages.append({"source": fname, **{k: str(v) for k, v in pkg.items()}})
    proj.packages_installed = (root / "dbt_packages").is_dir()

    # ---- profiles.yml, if it travels with the project ---------------------
    prof_path = root / "profiles.yml"
    if prof_path.is_file():
        try:
            prof = _yaml_load(prof_path) or {}
        except ValueError as exc:
            proj.warnings.append(f"could not parse profiles.yml: {exc}")
            prof = {}
        entry = (prof.get(proj.profile) if isinstance(prof, dict) else None) or {}
        outputs = entry.get("outputs") or {}
        proj.target_name = str(entry.get("target") or "")
        proj.has_molinia_target = any(
            isinstance(o, dict) and str(o.get("type", "")).lower() == "molinia"
            for o in outputs.values())
        chosen = outputs.get(proj.target_name) if isinstance(outputs, dict) else None
        if isinstance(chosen, dict):
            proj.target_schema = str(chosen.get("schema") or "")
            db = str(chosen.get("database") or "")
            proj.target_database = "" if "env_var" in db else db

    # ---- keys for the parity plan ----------------------------------------
    _derive_keys(proj, model_yaml_keys)

    # ---- low-precision review hints ---------------------------------------
    for node in proj.nodes + proj.macros:
        f = root / node.path
        if node.language != "sql" or not f.is_file() or f.suffix.lower() != ".sql":
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for line, alias in alias_shadow_lines(text):
            proj.alias_candidates.append({"file": node.path, "line": line, "alias": alias})
        for line, lit in regex_backslash_lines(text):
            proj.regex_literals.append({"file": node.path, "line": line, "literal": lit})

    _classify_nodes(proj)
    _push_macro_risk(proj)
    return proj


def _push_macro_risk(proj: "Project") -> None:
    """A macro with rule hits is ported once and reviewed in every caller.

    The caller's own SQL can be clean and still be wrong, because the dialect
    code is one `{{ }}` away. So a caller inherits the macro's class and says
    where it came from."""
    by_path = {n.path: n for n in proj.nodes}
    for macro in proj.macros:
        weighted = [h for h in macro.hits if h.rule.weight]
        if not weighted:
            continue
        ids = sorted({h.rule.id for h in weighted})
        for name, callers in proj.macro_callers.items():
            if name not in (macro.name or "").split(", "):
                continue
            for path in callers:
                node = by_path.get(path)
                if node is None:
                    continue
                node.flags.append(f"calls macro `{name}` ({macro.path}), which carries rule(s) "
                                  + ", ".join(ids))
                if severity_of(macro.klass) > severity_of(node.klass):
                    node.klass = macro.klass
                    node.reason = (f"its own SQL is clean, but it calls macro `{name}`, which "
                                   f"carries rule(s) " + ", ".join(ids))


_COMBO_KEYS = ("combination_of_columns", "combination_of_columns ")


def _derive_keys(proj: Project, model_yaml: Dict[str, dict]) -> None:
    for node in proj.nodes:
        if node.kind != "model":
            continue
        entry = model_yaml.get(node.name) or {}
        # model-level composite key (dbt_utils.unique_combination_of_columns)
        for t in _test_entries(_tests_of(entry), node.name, "", "model"):
            if "unique_combination_of_columns" in str(t["type"]):
                cols = t["args"].get("combination_of_columns")
                if isinstance(cols, list) and cols:
                    node.key = [str(c) for c in cols]
                    node.key_source = f"{t['type']} test"
                    break
        if node.key:
            continue
        for col in entry.get("columns") or []:
            if not isinstance(col, dict):
                continue
            names = [t["type"] for t in _test_entries(_tests_of(col), "", "", "column")]
            if "unique" in names:
                node.key = [str(col.get("name"))]
                node.key_source = "unique test on the column"
                break


def _classify_nodes(proj: Project) -> None:
    for node in proj.nodes + proj.macros:
        if node.kind == "seed":
            node.klass, node.reason = NOT_SUPPORTED_TODAY, (
                "dbt-molinia refuses seeds: the adapter cannot send bind parameters. Load the CSV "
                "through ingest instead and drop the seed, or keep it as a model with literals.")
            continue
        if node.kind == "snapshot":
            node.klass, node.reason = NOT_SUPPORTED_TODAY, (
                "dbt-molinia cannot run snapshots: there is no session state between statements. "
                "SCD2 history has to be rebuilt another way.")
            continue
        if node.language == "python":
            node.klass, node.reason = REDESIGN, (
                "Python model: rewrite as SQL. If the logic cannot be expressed in SQL it leaves "
                "the platform.")
            continue
        weighted = [h.rule.klass for h in node.hits if h.rule.weight]
        node.klass = worst(*weighted)
        if node.materialized == "incremental":
            node.klass = worst(node.klass, AGENT_PLUS_REVIEW)
            node.flags.append("incremental: implemented but NEVER proven on a live server")
        if node.klass == AUTOMATIC and not node.hits:
            node.reason = ("no Snowflake-only construct found: the agent ports the config and the "
                           "relation names, parity proves it.")
        elif node.klass == AUTOMATIC:
            ids = sorted({h.rule.id for h in node.hits if h.rule.weight})
            node.reason = ("only mechanical rewrites, each of which fails loudly if missed: rule(s) "
                           + ", ".join(ids))
        else:
            ids = sorted({h.rule.id for h in node.hits if h.rule.weight
                          and h.rule.klass == node.klass})
            node.reason = f"{node.klass.lower().replace('_', ' ')} because of rule(s) " + ", ".join(ids)
            if node.materialized == "incremental" and not ids:
                node.reason = "incremental materialization has never been proven on a live server"


# --------------------------------------------------------------------------- aggregation


def construct_frequency(proj: Project) -> List[dict]:
    """Every rule that fired, most frequent first, with its hits."""
    by_rule: Dict[Tuple[str, str], dict] = {}
    all_hits = [h for n in proj.nodes + proj.macros for h in n.hits] + proj.project_config_hits
    for h in all_hits:
        key = (h.rule.id, h.rule.construct)
        row = by_rule.setdefault(key, {"rule": h.rule.id, "construct": h.rule.construct,
                                       "molinia": h.rule.molinia, "class": h.rule.klass,
                                       "why": h.rule.why, "basis": h.rule.basis,
                                       "precision": h.rule.precision, "counts": h.rule.weight,
                                       "n": 0, "hits": []})
        row["n"] += 1
        row["hits"].append({"file": h.path, "line": h.line, "match": h.text})
    return sorted(by_rule.values(), key=lambda r: (-r["n"], r["rule"]))


def test_frequency(proj: Project) -> List[Tuple[str, int]]:
    counts: Dict[str, int] = {}
    for t in proj.tests:
        counts[str(t["type"])] = counts.get(str(t["type"]), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def materialization_frequency(proj: Project) -> List[Tuple[str, int]]:
    counts: Dict[str, int] = {}
    for n in proj.models:
        counts[n.materialized or "view"] = counts.get(n.materialized or "view", 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
