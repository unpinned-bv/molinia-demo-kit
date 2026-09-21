"""SQL statement splitter shared by sf.py (Snowflake) and molinia.py (DuckDB).

Splits on `;` only outside of:
  * single-quoted strings  ('it''s', and Snowflake's backslash escape 'it\\'s')
  * double-quoted identifiers ("a;b", with "" as the escape)
  * dollar-quoted blocks   ($$ ... $$; DuckDB also allows $tag$ ... $tag$)
  * comments               (-- line, /* block */, and // line in Snowflake)

Statements that contain nothing but whitespace and comments are dropped, so a
trailing comment after the last `;` never becomes an empty statement (Snowflake
rejects those). Each returned statement is the original text, trimmed, without
its terminating `;`.

Not supported: Snowflake Scripting blocks written without $$ delimiters
(`BEGIN ... ; ... END;`). Wrap those in `EXECUTE IMMEDIATE $$ ... $$;`.
"""
from __future__ import annotations

import re
from typing import List

_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_statements(sql: str, dialect: str = "snowflake") -> List[str]:
    if dialect not in ("snowflake", "duckdb"):
        raise ValueError(f"unknown dialect {dialect!r}")
    slash_comments = dialect == "snowflake"
    backslash_escapes = dialect == "snowflake"
    tagged_dollar = dialect == "duckdb"

    out: List[str] = []
    buf: List[str] = []
    has_code = False
    i = 0
    n = len(sql)

    def flush() -> None:
        nonlocal buf, has_code
        text = "".join(buf).strip()
        if has_code and text:
            out.append(text)
        buf = []
        has_code = False

    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # line comments
        if (c == "-" and nxt == "-") or (slash_comments and c == "/" and nxt == "/"):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
            continue
        # block comment
        if c == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            j = n if j == -1 else j + 2
            buf.append(sql[i:j])
            i = j
            continue
        # single-quoted string
        if c == "'":
            j = i + 1
            while j < n:
                if backslash_escapes and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            has_code = True
            i = j
            continue
        # double-quoted identifier
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            has_code = True
            i = j
            continue
        # dollar-quoted block
        if c == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m and (m.group(1) is None or tagged_dollar):
                # A tagged $x$ must not be the tail of an identifier (DuckDB
                # positional params look like $1, which the regex never matches).
                prev = sql[i - 1] if i > 0 else ""
                if not (prev.isalnum() or prev == "_"):
                    tag = m.group(0)
                    end = sql.find(tag, m.end())
                    j = n if end == -1 else end + len(tag)
                    buf.append(sql[i:j])
                    has_code = True
                    i = j
                    continue
        if c == ";":
            flush()
            i += 1
            continue
        if not c.isspace():
            has_code = True
        buf.append(c)
        i += 1

    flush()
    return out


def strip_leading_comments(stmt: str) -> str:
    """The statement without leading comments/whitespace (for one-line display)."""
    s = stmt
    while True:
        s = s.lstrip()
        if s.startswith("--") or s.startswith("//"):
            nl = s.find("\n")
            s = "" if nl == -1 else s[nl + 1:]
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            s = "" if end == -1 else s[end + 2:]
            continue
        return s


def one_line(stmt: str, width: int = 72) -> str:
    s = " ".join(strip_leading_comments(stmt).split())
    return s if len(s) <= width else s[: width - 3] + "..."


# `with` is deliberately absent: DuckDB allows `WITH ... INSERT/UPDATE/DELETE`.
_READ_VERBS = ("select", "show", "describe", "desc", "explain", "summarize", "from", "values", "table")


def is_read_only(stmt: str) -> bool:
    """Heuristic used only to decide whether a request may be RETRIED after a
    gateway error (5xx / connection reset, when the statement may or may not
    have run). It is never used for authorization (the server decides)."""
    s = strip_leading_comments(stmt).lstrip("( \t\r\n").lower()
    first = re.split(r"\s", s, maxsplit=1)[0] if s else ""
    return first in _READ_VERBS
