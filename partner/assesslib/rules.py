"""The Snowflake-only construct table.

Every entry is taken from `agent/MIGRATION_RULES.md` and keeps that file's rule
id, so a scope report, a commit message and a review comment all cite the same
thing. `partner/tests/test_rules.py` fails if an id used here is not in the
rulebook — the list grows by growing the rulebook, never by inventing a rule.

`klass` is the class a hit gives the node it is in (classify.worst over all its
hits). The split is by *how a wrong port fails*:

  AUTOMATIC           there is one mechanical rewrite and a miss fails loudly
                      (binder error) or parity catches it in the obvious column
  AGENT_PLUS_REVIEW   a miss is silent: money scale, a JSON path that binds to
                      the wrong thing, NULL ordering, an index base, a regex
                      that quietly matches nothing
  REDESIGN            the rulebook says "report, don't hack": the value cannot
                      be reproduced, so a human re-agrees the semantics
  NOT_SUPPORTED_TODAY no target-side feature exists at all

`weight=False` marks a low-precision review hint: it is printed, but it does not
move a node into a more expensive class on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .classify import AGENT_PLUS_REVIEW, AUTOMATIC, INFERENCE, NOT_SUPPORTED_TODAY, REDESIGN, VERIFIED


@dataclass(frozen=True)
class Rule:
    id: str                    # MIGRATION_RULES id
    construct: str             # what the client wrote
    molinia: str               # what it becomes, or why it cannot
    klass: str
    pattern: str
    why: str = ""
    basis: str = VERIFIED
    weight: bool = True
    precision: str = "high"    # high | medium | low
    contexts: Tuple[str, ...] = ("sql",)   # sql | yaml | proc
    skip_jinja: bool = False   # ignore hits that fall inside {{ ... }} / {% ... %}
    needs_strings: bool = False  # match with string literals intact (comments are still masked)

    def regex(self) -> "re.Pattern[str]":
        return _compiled(self.pattern)


_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _compiled(pattern: str) -> "re.Pattern[str]":
    got = _CACHE.get(pattern)
    if got is None:
        got = _CACHE[pattern] = re.compile(pattern, re.IGNORECASE)
    return got


def _fn(*names: str) -> str:
    """A regex matching any of these function names followed by `(`."""
    return r"\b(?:" + "|".join(names) + r")\s*\("


# --------------------------------------------------------------------------- SQL rules

SQL_RULES: Sequence[Rule] = (
    # --- 3.1 conditionals and NULLs ----------------------------------------
    Rule("S1", "IFF(c, a, b)", "CASE WHEN c THEN a ELSE b END", AUTOMATIC, _fn("iff"),
         "one mechanical rewrite; DuckDB has no IFF, so a miss fails loudly"),
    Rule("S2", "NVL / IFNULL / ZEROIFNULL / NULLIFZERO / NVL2 / EQUAL_NULL",
         "COALESCE / COALESCE(x,0) / NULLIF(x,0) / CASE / IS NOT DISTINCT FROM", AUTOMATIC,
         _fn("nvl", "nvl2", "ifnull", "zeroifnull", "nullifzero", "equal_null"),
         "mechanical; DuckDB has none of these names"),
    Rule("S3", "DECODE(e, s1, r1, ..., d)", "CASE e WHEN s1 THEN r1 ... ELSE d END", AGENT_PLUS_REVIEW,
         _fn("decode"),
         "DECODE matches NULL to NULL and CASE does not; DuckDB's own decode() is a BLOB function"),
    Rule("S4", "GREATEST / LEAST", "the same plus an explicit NULL guard", AGENT_PLUS_REVIEW,
         _fn("greatest", "least", "greatest_ignore_nulls", "least_ignore_nulls"),
         "Snowflake returns NULL if any argument is NULL; DuckDB ignores NULLs — silent"),
    Rule("S5", "CONCAT / CONCAT_WS", "the `||` operator", AGENT_PLUS_REVIEW,
         _fn("concat", "concat_ws"),
         "Snowflake propagates NULL, DuckDB's concat skips it — silent"),

    # --- 3.2 numbers and money ---------------------------------------------
    Rule("N1", "NUMBER / NUMBER(p,s) type", "DECIMAL(p,s); never a DuckDB INT (32-bit)",
         AUTOMATIC, r"\bnumber\s*\(|::\s*number\b|\bnumber\b(?=\s*\)|\s*,|\s*$)",
         "mechanical type rewrite"),
    Rule("N1", "::INT / ::INTEGER / ::SMALLINT / CAST(x AS INT)",
         "DECIMAL(38,0), or BIGINT for ids and counts", AUTOMATIC,
         r"::\s*(?:int|integer|smallint|tinyint|byteint)\b"
         r"|\bas\s+(?:int|integer|smallint|tinyint|byteint)\s*\)",
         "every Snowflake INT is NUMBER(38,0); DuckDB INT is 32-bit, so a straight port works "
         "until a value passes 2^31 and then fails in production. `::bigint` is deliberately not "
         "matched: it is the rulebook's own target form for counts (N12, A7)",
         skip_jinja=True),
    Rule("N1", "bare ::DECIMAL / ::NUMERIC (no precision)", "DECIMAL(p,s), with p and s written out",
         AGENT_PLUS_REVIEW,
         r"::\s*(?:decimal|numeric)\b(?!\s*\()|\bas\s+(?:decimal|numeric)\s*\)",
         "DuckDB defaults a bare DECIMAL to DECIMAL(18,3), so money silently gains a third decimal "
         "and loses 20 digits of range — no error, just different numbers",
         skip_jinja=True),
    Rule("N2", "/ on NUMBER operands", "{{ molinia_round_div(a, b, S) }} (see rule N7)",
         AGENT_PLUS_REVIEW, r"(?<=[\w)\]])\s*/(?![/*])\s*(?=[\w(])",
         "DuckDB division always returns DOUBLE, so money drifts by a cent — silent",
         precision="medium", skip_jinja=True),
    Rule("N5", "ROUND(x, n, 'HALF_TO_EVEN')", "no portable form", REDESIGN,
         r"round\s*\([^;]{0,200}?half_to_even",
         "banker's rounding on a NUMBER cannot be reproduced; round_even() is not a substitute",
         needs_strings=True),
    Rule("N7", "DIV0 / DIV0NULL of two columns", "{{ molinia_round_div(a, b, S, div0=true) }}",
         AGENT_PLUS_REVIEW, _fn("div0", "div0null"),
         "the quotient scale has to be read off the Snowflake types or the cents drift"),
    Rule("N9", "AVG(money)", "round({{ molinia_round_div('sum(x)','count(x)', S) }}, 2)",
         AGENT_PLUS_REVIEW, _fn("avg"),
         "DuckDB avg(DECIMAL) returns DOUBLE"),
    Rule("N11", "TRY_TO_NUMBER / TO_NUMBER / TRY_TO_DECIMAL / TRY_TO_DOUBLE",
         "TRY_CAST(x AS DECIMAL(p,s)) / CAST", AUTOMATIC,
         _fn("try_to_number", "to_number", "try_to_decimal", "to_decimal", "try_to_double",
             "to_double"),
         "mechanical, but formatted input ('1,234.5') becomes NULL"),

    # --- 3.3 dates and times ------------------------------------------------
    Rule("D1", "TIMESTAMP_LTZ / TIMESTAMP_TZ", "TIMESTAMPTZ, which depends on a session time zone",
         AGENT_PLUS_REVIEW, r"\btimestamp_(?:ltz|tz)\b",
         "every reader has to agree on the session zone; flag it to the reviewer"),
    Rule("D1", "TIMESTAMP_NTZ", "TIMESTAMP", AUTOMATIC, r"\btimestamp_ntz\b",
         "mechanical; the Parquet unload is UTC-adjusted"),
    Rule("D2", "TO_VARCHAR(x, fmt) / TO_CHAR(x, fmt)", "strftime(x, fmt) with a rewritten format string",
         AGENT_PLUS_REVIEW, _fn("to_varchar", "to_char"),
         "the format string changes (YYYY-MM -> %Y-%m) and the default timestamp text differs"),
    Rule("D3", "DATEADD / TIMEADD / TIMESTAMPADD", "x + INTERVAL (n) part", AGENT_PLUS_REVIEW,
         _fn("dateadd", "timeadd", "timestampadd"),
         "DATE + INTERVAL returns a TIMESTAMP on DuckDB and a DATE on Snowflake — silent type change"),
    Rule("D4", "DATEDIFF / TIMEDIFF / TIMESTAMPDIFF", "datediff('part', a, b) with the part quoted",
         AGENT_PLUS_REVIEW, _fn("datediff", "timediff", "timestampdiff"),
         "an unquoted part is read as a column, and 'week' counts differently in the two engines"),
    Rule("D5", "DATE_TRUNC / TRUNC(date)", "CAST(date_trunc('part', x) AS DATE) when the input is a DATE",
         AGENT_PLUS_REVIEW, _fn("date_trunc", "trunc"),
         "DuckDB returns a TIMESTAMP even for DATE input"),
    Rule("D6", "DAYNAME / MONTHNAME / WEEK / WEEKOFYEAR / DAYOFWEEKISO",
         "strftime(x, '%a') / '%b' / isodow; WEEK depends on WEEK_OF_YEAR_POLICY",
         AGENT_PLUS_REVIEW, _fn("dayname", "monthname", "weekofyear", "week", "dayofweekiso"),
         "DuckDB returns full names, and the week policy is a Snowflake account setting"),
    Rule("D7", "DATE_PART(epoch_second, ts)", "trunc(epoch(ts))::bigint", AGENT_PLUS_REVIEW,
         _fn("date_part"),
         "bare epoch() returns a DOUBLE with the fraction"),
    Rule("D7", "TO_DATE / TO_TIMESTAMP / DATE_FROM_PARTS / LAST_DAY", "CAST / strptime / make_date",
         AUTOMATIC, _fn("to_date", "to_timestamp", "to_timestamp_ntz", "to_timestamp_ltz",
                        "to_timestamp_tz", "date_from_parts", "timestamp_from_parts", "last_day"),
         "mechanical"),
    Rule("D8", "CURRENT_DATE / CURRENT_TIMESTAMP / SYSDATE / GETDATE",
         "a dbt var such as as_of_date", REDESIGN,
         r"\b(?:current_date|current_timestamp|localtimestamp|sysdate\s*\(|getdate\s*\()",
         "a non-deterministic model cannot be proven by parity, so the grain has to be re-agreed"),
    Rule("H10", "RANDOM / UUID_STRING / RANDSTR / SEQ1..SEQ8 / <sequence>.NEXTVAL",
         "no portable form: the model has to be made deterministic first", REDESIGN,
         _fn("random", "uuid_string", "randstr", "seq1", "seq2", "seq4", "seq8")
         + r"|[A-Za-z0-9_$\"\]]\s*\.\s*nextval\b",
         "the rulebook forbids non-deterministic models (H10) and lists these among the values that "
         "cannot be reproduced (section 8): parity can never prove such a model, so the grain and "
         "the key strategy have to be re-agreed with the client before it is quoted. NEXTVAL is "
         "matched on any prefix (`seq.nextval`, `raw.seq.nextval`, `\"SEQ\".nextval`), because a "
         "sequence is normally referenced through its schema and a qualified one would otherwise "
         "score as a plain three-part name (P5, a cheaper class)",
         skip_jinja=True),
    Rule("D9", "CONVERT_TIMEZONE(from, to, ts)", "timezone(to, timezone(from, ts))", AGENT_PLUS_REVIEW,
         _fn("convert_timezone"),
         "needs the ICU extension, which has not been confirmed on Molinia: probe it first",
         basis=INFERENCE),

    # --- 3.4 semi-structured -----------------------------------------------
    Rule("J1", "VARIANT path v:key / v:a.b", "v ->> '$.key'", AGENT_PLUS_REVIEW,
         r"(?<![:\w])[A-Za-z_][A-Za-z0-9_$]*\s*:(?!:)\s*[A-Za-z_][A-Za-z0-9_$]*",
         "J2 landmine: DuckDB reads x:y as the alias syntax `y AS x`, so a path over a column that "
         "shares the key name silently returns the wrong column", skip_jinja=True),
    Rule("J3", "PARSE_JSON / OBJECT_CONSTRUCT / ARRAY_SIZE / GET_PATH / IS_NULL_VALUE",
         "CAST(s AS JSON) / json_object / json_array_length / ->>", AGENT_PLUS_REVIEW,
         _fn("parse_json", "try_parse_json", "object_construct", "object_construct_keep_null",
             "array_size", "get_path", "is_null_value", "array_construct", "object_keys"),
         "NULL handling differs in each of them"),
    Rule("J1", "VARIANT subscript v['key'] / t.v['key']", "v ->> '$.key'", AGENT_PLUS_REVIEW,
         r"(?<!\w)[A-Za-z_][A-Za-z0-9_$]*\s*\[\s*'",
         "DuckDB `v['key']` on a VARCHAR is string slicing, not a JSON path. A table-qualified "
         "column (`o.order_meta['k']`) is matched too: it is the common spelling in a join, and "
         "it is the subscript that is wrong, not the prefix. `arr[1]` does not match — the quote "
         "is what distinguishes a JSON key from a list index",
         skip_jinja=True, needs_strings=True),
    Rule("J3", "TYPEOF(v)", "no portable form", REDESIGN, _fn("typeof"),
         "json_type uses different names; only parity can prove a mapping"),

    # --- 3.5 LATERAL FLATTEN ------------------------------------------------
    Rule("F1", "LATERAL FLATTEN(input => v)", "unnest(json_extract(v,'$.x[*]')) WITH ORDINALITY",
         AGENT_PLUS_REVIEW, _fn("flatten"),
         "FLATTEN's index is 0-based and WITH ORDINALITY is 1-based — a silent off-by-one"),
    Rule("F3", "FLATTEN(RECURSIVE => TRUE) / f.path / f.this", "not covered by the rulebook", REDESIGN,
         r"recursive\s*=>|\bf\.(?:path|this)\b",
         "no rewrite exists yet: it has to be designed and added to the rulebook"),

    # --- 3.6 aggregates and windows ----------------------------------------
    Rule("A1", "LISTAGG(...) WITHIN GROUP (ORDER BY ...)",
         "coalesce(string_agg(x, ',' ORDER BY y), '')", AGENT_PLUS_REVIEW, _fn("listagg"),
         "DuckDB rejects WITHIN GROUP, and an all-NULL group gives NULL instead of ''"),
    Rule("A2", "RATIO_TO_REPORT(x) OVER (...)", "x / nullif(sum(x) over (...), 0), or the division macro",
         AGENT_PLUS_REVIEW, _fn("ratio_to_report"),
         "no DuckDB equivalent, and DuckDB x/0 returns inf rather than NULL"),
    Rule("A3", "ARRAY_AGG(x) WITHIN GROUP (ORDER BY x)",
         "CAST(to_json(array_agg(x ORDER BY x) FILTER (WHERE x IS NOT NULL)) AS VARCHAR)",
         AGENT_PLUS_REVIEW, _fn("array_agg"),
         "Snowflake drops NULLs, DuckDB keeps them, and the answer key holds JSON text"),
    Rule("A4", "ORDER BY ... DESC with no NULLS clause", "the same, plus an explicit NULLS FIRST",
         AGENT_PLUS_REVIEW, r"\bdesc\b(?!\s+nulls\b)",
         "Snowflake DESC defaults to NULLS FIRST and DuckDB puts NULLs last in both directions, so "
         "a window, QUALIFY, string_agg or final ORDER BY over a nullable column keeps different "
         "rows on the two engines — silently. Harmless on a NOT NULL column, which is why this is "
         "a hint: read each one, and write the NULLS clause wherever the column can be NULL",
         weight=False, precision="low", skip_jinja=True),
    Rule("A5", "LAST_VALUE / NTH_VALUE", "the same, with an explicit window frame", AGENT_PLUS_REVIEW,
         _fn("last_value", "nth_value"),
         "DuckDB's default frame ends at the current row — silent"),
    Rule("A6", "QUALIFY", "QUALIFY (works as written)", AGENT_PLUS_REVIEW, r"\bqualify\b",
         "works, but the ordering must be total or the two engines keep different rows"),
    Rule("A7", "COUNT_IF(c)", "count_if(c)::bigint", AUTOMATIC, _fn("count_if"),
         "returns HUGEINT on DuckDB, so it needs a cast"),
    Rule("A8", "BOOLAND_AGG / BOOLOR_AGG", "bool_and / bool_or", AUTOMATIC,
         _fn("booland_agg", "boolor_agg"), "mechanical"),
    Rule("A8", "APPROX_COUNT_DISTINCT / APPROX_PERCENTILE / HLL / ANY_VALUE / HASH_AGG",
         "the exact aggregate, or a re-agreed definition", REDESIGN,
         _fn("approx_count_distinct", "approx_percentile", "approx_top_k", "hll", "hll_estimate",
             "any_value", "hash_agg"),
         "estimates and arbitrary picks differ between engines, so parity cannot prove them"),

    # --- 3.7 strings, regex, identifiers ------------------------------------
    Rule("X1", "REGEXP_SUBSTR / REGEXP_LIKE / RLIKE / REGEXP_REPLACE",
         "regexp_extract / regexp_full_match / regexp_replace(..., 'g')", AGENT_PLUS_REVIEW,
         _fn("regexp_substr", "regexp_replace", "regexp_count", "regexp_instr", "regexp_like")
         + r"|\brlike\b",
         "anchoring, the replace-all flag and the backslash escaping all differ; a wrong pattern "
         "returns no error, just no match"),
    Rule("X2", "SPLIT / SPLIT_PART / SPLIT_TO_TABLE", "string_split / split_part / unnest(string_split)",
         AGENT_PLUS_REVIEW, _fn("split", "split_part", "split_to_table", "strtok_to_array"),
         "SPLIT_PART(s, d, 0) means 1 on Snowflake and '' on DuckDB"),
    Rule("X3", "CHARINDEX / STARTSWITH / ENDSWITH", "instr (arguments swap) / starts_with / ends_with",
         AUTOMATIC, _fn("charindex", "startswith", "endswith"),
         "mechanical. CONTAINS is deliberately not matched: it ports unchanged"),
    Rule("X3", "INITCAP", "no portable form", REDESIGN, _fn("initcap"),
         "DuckDB has no INITCAP; the casing rule has to be re-agreed"),
    Rule("X4", "HASH(...)", "no portable form (MD5 does match)", REDESIGN, _fn("hash"),
         "DuckDB's hash is a different algorithm, so any key or column built from it changes value"),

    # --- 3.8 DDL and syntax -------------------------------------------------
    Rule("Y1", "TOP n", "LIMIT n", AUTOMATIC, r"\bselect\s+top\s+\d", "mechanical"),
    Rule("Y1", "SAMPLE / TABLESAMPLE", "forbidden in models", REDESIGN,
         r"\b(?:tablesample|sample)\s*\(|\bsample\s+row|\bsample\s+\d",
         "non-deterministic, so parity cannot prove the model"),
    Rule("Y2", "GRANT / REVOKE", "RBAC in the Molinia console, never from SQL", NOT_SUPPORTED_TODAY,
         r"\b(?:grant|revoke)\s+(?:select|insert|update|delete|all|usage|ownership|monitor|modify)\b",
         "SQL GRANT does not exist; access control is recreated in the console by an admin"),
    Rule("Y2", "CLONE / UNDROP / AT(...) time travel", "no equivalent", NOT_SUPPORTED_TODAY,
         r"\b(?:undrop|clone)\b|\b(?:at|before)\s*\(\s*(?:timestamp|offset|statement)\s*=>",
         "no zero-copy clone, no undrop and no time travel"),
    Rule("Y2", "TRANSIENT / CLUSTER BY / COPY GRANTS",
         "delete the config (dbt-molinia ignores unknown configs)", AUTOMATIC,
         r"\btransient\b|\bcluster_by\b|\bcluster\s+by\b|\bcopy_grants\b|\bcopy\s+grants\b"
         r"|\bautomatic_clustering\b|\bsnowflake_warehouse\b|\btmp_relation_type\b",
         "removal only, for clarity"),
    Rule("Y2", "SECURE view / ALTER ... SET TAG", "no equivalent: redone as masking or RLS in the console",
         REDESIGN,
         r"\bsecure\s*=|\bsecure\s+view\b|\bcreate\s+secure\b|\b(?:un)?set\s+tag\b",
         "a SECURE view hides its definition and restricts what the reader may infer; there is no "
         "such object, so the protection has to be re-expressed as a console policy and re-tested"),
    Rule("Y3", "IDENTIFIER('...') / session variables (SET x = …, $x)", "a plain name / a dbt var",
         AGENT_PLUS_REVIEW,
         _fn("identifier") + r"|\bset\s+\w+\s*=\s*|(?<![\w$)\]])\$[A-Za-z_]\w*",
         "the indirection has to be resolved statically", skip_jinja=True),
    Rule("P5", "three-part name DB.SCHEMA.OBJECT", "source() / ref(); relations render two-part",
         AGENT_PLUS_REVIEW,
         r"(?<![\w.\"'{])[A-Za-z_][A-Za-z0-9_$]{1,}\.[A-Za-z_][A-Za-z0-9_$]{1,}\.[A-Za-z_][A-Za-z0-9_$]{1,}",
         "the org catalog is implicit, so a hard-coded database name cannot resolve",
         precision="medium", skip_jinja=True),

    # --- procedural SQL reaching a dbt project ------------------------------
    Rule("F-NOTPORT", "EXECUTE IMMEDIATE / DECLARE / CALL / LET",
         "no control flow: procedures are statement batches", REDESIGN,
         r"\bexecute\s+immediate\b|\bdeclare\b|\bcall\s+[A-Za-z_\"]|\blet\s+\w+\s*(?::=|=)",
         "Snowflake Scripting has no target-side equivalent"),
)

# Extra rules that only make sense inside a stored procedure or function body.
PROC_RULES: Sequence[Rule] = (
    Rule("F-NOTPORT", "BEGIN ... END block", "no control flow", REDESIGN,
         r"\bbegin\b", "Snowflake Scripting block", contexts=("proc",)),
    Rule("F-NOTPORT", "IF / FOR / WHILE / EXCEPTION", "no control flow", REDESIGN,
         r"\b(?:elseif|exception\s+when|for\s+\w+\s+in\b|while\s+.+\s+do\b|repeat\b)",
         "Snowflake Scripting control flow", contexts=("proc",)),
)

# --------------------------------------------------------------------------- YAML rules

YAML_RULES: Sequence[Rule] = (
    Rule("P2", "+transient / +copy_grants / +query_tag / cluster_by / +secure",
         "delete the config", AUTOMATIC,
         r"^\s*[+-]?\s*(?:transient|copy_grants|query_tag|cluster_by|secure|snowflake_warehouse"
         r"|automatic_clustering|tmp_relation_type)\s*:",
         "Snowflake-only model configs", contexts=("yaml",)),
    Rule("P2", "grants:", "RBAC in the console; dbt must not issue grants", NOT_SUPPORTED_TODAY,
         r"^\s*[+-]?\s*grants\s*:", "a grants: config makes dbt issue SQL GRANT statements",
         contexts=("yaml",)),
    Rule("P3", "source database:", "remove it; sources live in schema main", AGENT_PLUS_REVIEW,
         r"^\s*[+-]?\s*database\s*:",
         "ingest always lands in schema main, and relations render two-part",
         contexts=("yaml",)),
    Rule("P6", "materialized: incremental", "delete+insert through _dbt_internal — UNPROVEN live",
         AGENT_PLUS_REVIEW, r"^\s*[+-]?\s*materialized\s*:\s*['\"]?incremental",
         "implemented but never run against a live server: prove it twice with parity",
         contexts=("yaml",)),
    Rule("P6", "materialized: materialized_view / dynamic_table", "rebuild as a scheduled table model",
         REDESIGN, r"^\s*[+-]?\s*materialized\s*:\s*['\"]?(?:materialized_view|dynamic_table)",
         "no materialized view or dynamic table object exists", basis=INFERENCE, contexts=("yaml",)),
    Rule("P10", "multi-statement hook", "one statement per request", AGENT_PLUS_REVIEW,
         r"^\s*[+-]?\s*(?:pre_hook|post_hook|pre-hook|post-hook|on-run-start|on-run-end)\s*:",
         "dbt-molinia sends one statement per HTTP request, so a multi-statement hook fails",
         contexts=("yaml",)),
)

ALL_RULES: Tuple[Rule, ...] = tuple(SQL_RULES) + tuple(PROC_RULES) + tuple(YAML_RULES)


def rule_ids() -> List[str]:
    """Every distinct id this table cites (for the rulebook cross-check test)."""
    return sorted({r.id for r in ALL_RULES})


def rules_for(context: str) -> List[Rule]:
    if context == "proc":
        return [r for r in SQL_RULES] + [r for r in PROC_RULES]
    return [r for r in ALL_RULES if context in r.contexts]
