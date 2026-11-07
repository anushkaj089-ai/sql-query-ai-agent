"""Deterministic SQL validation. No LLM is involved: this is the safety net.

Checks (in order):
  1. exactly one statement, and it parses
  2. read-only: the statement is a SELECT / set operation, no DML/DDL anywhere in the tree
  3. every table exists in the schema
  4. every column exists (aliases / CTEs / sub-queries resolved by sqlglot's qualifier)
  5. joins reference real columns; joins not on a declared foreign key produce a warning
  6. SQLite itself accepts the query (EXPLAIN QUERY PLAN)
"""
import re
import sqlite3
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from . import db

FORBIDDEN_NODES = tuple(
    getattr(exp, n) for n in
    ("Insert", "Update", "Delete", "Drop", "Alter", "AlterTable", "TruncateTable", "Create", "Merge",
     "Command", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback", "Set")
    if hasattr(exp, n)
)


@dataclass
class ValidationResult:
    valid: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self):
        return {"valid": self.valid, "errors": self.errors, "warnings": self.warnings}


# ---------- natural-language gates (run BEFORE any LLM call) ----------

_DESTRUCTIVE_PATTERNS = [
    r"\b(delete|drop|truncate|alter)\b",
    r"\binsert\s+(into|a|an|new|some|row|record)",
    r"\bupdate\s+(the\s+)?\w+(\s+\w+)?\s+(set|to)\b",
    r"\bupdate\b.*\bset\b",
    r"\b(remove|erase|wipe|destroy|purge)\s+(all|every|the|this|these|those|my|rows?|records?|tables?|data)\b",
    r"\b(change|modify|edit|overwrite)\s+(the\s+)?(salary|price|data|records?|rows?|table|schema|values?)\b",
    r"\bcreate\s+(a\s+)?(table|database|view|index|trigger)\b",
    r"\b(grant|revoke)\b",
]
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|system)?\s*(instructions|rules|prompts?|guidelines)",
    r"disregard\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier|system)?\s*(instructions|rules|prompts?)",
    r"(reveal|show|print|repeat|output)\s+(me\s+)?(your|the)\s+(system\s+)?(prompt|instructions)",
    r"\byou\s+are\s+now\b",
    r"\bact\s+as\s+(an?\s+)?(?!sql)",
    r"\bjailbreak\b|\bdeveloper\s+mode\b|\bDAN\s+mode\b",
    r"pretend\s+(that\s+)?(you|there)",
]


def is_destructive_request(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in _DESTRUCTIVE_PATTERNS)


def is_injection_attempt(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in _INJECTION_PATTERNS)


# ---------- SQL validation ----------

def _schema_maps():
    info = db.get_schema_info()["tables"]
    canon = {t.lower(): t for t in info}
    cols = {t.lower(): {c["name"].lower(): c["name"] for c in d["columns"]} for t, d in info.items()}
    fks = set()
    for t, d in info.items():
        for fk in d["fks"]:
            fks.add((t.lower(), fk["from"].lower(), fk["to_table"].lower(), fk["to"].lower()))
            fks.add((fk["to_table"].lower(), fk["to"].lower(), t.lower(), fk["from"].lower()))
    return info, canon, cols, fks


def validate_sql(sql: str, dialect: str = "sqlite", check_engine: bool = True) -> ValidationResult:
    errors, warnings = [], []
    if not sql or not sql.strip():
        return ValidationResult(False, ["The query is empty."])

    # 1. parse -----------------------------------------------------------
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except (ParseError, SqlglotError) as e:
        return ValidationResult(False, [f"Syntax error: {_clean_parse_error(e)}"])
    if not statements:
        return ValidationResult(False, ["The query is empty."])
    if len(statements) > 1:
        return ValidationResult(False, ["Only a single statement is allowed (found several separated by ';')."])
    tree = statements[0]

    # 2. read-only -------------------------------------------------------
    if not isinstance(tree, exp.Query):
        kind = type(tree).__name__.upper()
        return ValidationResult(False, [f"Only read-only SELECT queries are allowed (found {kind})."])
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return ValidationResult(False, [f"Forbidden operation in query: {type(node).__name__.upper()}."])

    # 3. tables ----------------------------------------------------------
    info, canon, cols, fks = _schema_maps()
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for tbl in tree.find_all(exp.Table):
        name = tbl.name.lower()
        if not name or name in cte_names:
            continue
        if name not in canon:
            close = _closest(name, list(canon.values()))
            hint = f" Did you mean '{close}'?" if close else ""
            errors.append(f"Table '{tbl.name}' does not exist.{hint}")
    if errors:
        return ValidationResult(False, errors)

    # 4. columns (sqlglot qualifier resolves aliases, CTEs, sub-queries) --
    schema = {t: {c["name"]: c["type"] for c in d["columns"]} for t, d in info.items()}
    try:
        qualified = qualify(tree.copy(), schema=schema, dialect=dialect,
                            validate_qualify_columns=True, identify=False, quote_identifiers=False)
    except OptimizeError as e:
        msg = str(e)
        m = re.search(r"Column '([^']+)'", msg) or re.search(r"Unknown column:\s*(\S+)", msg)
        if m:
            raw = m.group(1)
            col = raw.split(".")[-1]
            known = {c for cs in cols.values() for c in cs}
            if col.lower() in known:
                errors.append(f"Column '{raw}' could not be resolved: either the table alias is unknown "
                              f"or the column name is ambiguous. Qualify it with the right table alias.")
            else:
                errors.append(f"Column '{raw}' does not exist.{_column_hint(col, cols)}")
        else:
            errors.append(f"Could not resolve columns/tables: {msg}")
        return ValidationResult(False, errors)
    except SqlglotError as e:
        return ValidationResult(False, [f"Could not analyse the query: {e}"])

    # 5. joins -----------------------------------------------------------
    try:
        for scope in traverse_scope(qualified):
            if not isinstance(scope.expression, exp.Select):
                continue
            for join in scope.expression.args.get("joins") or []:
                on = join.args.get("on")
                kind = (join.args.get("kind") or "").upper()
                if on is None and not join.args.get("using") and kind != "CROSS" and not join.args.get("method"):
                    warnings.append("A JOIN has no ON condition (this produces a cross join).")
                    continue
                if on is None:
                    continue
                for eq in on.find_all(exp.EQ):
                    l, r = eq.left, eq.right
                    if not (isinstance(l, exp.Column) and isinstance(r, exp.Column)):
                        continue
                    lt, rt = _real_table(scope, l.table), _real_table(scope, r.table)
                    if not lt or not rt:
                        continue
                    key = (lt.lower(), l.name.lower(), rt.lower(), r.name.lower())
                    if key not in fks:
                        warnings.append(
                            f"Join {lt}.{l.name} = {rt}.{r.name} is not a declared foreign-key relationship.")
    except SqlglotError:
        pass  # join analysis is best-effort; engine check below is authoritative

    # 6. engine check ----------------------------------------------------
    if check_engine and dialect == "sqlite":
        try:
            db.explain_plan(sql)
        except sqlite3.Error as e:
            return ValidationResult(False, [f"SQLite rejected the query: {e}"], warnings)

    return ValidationResult(True, [], list(dict.fromkeys(warnings)))


def _clean_parse_error(e) -> str:
    msg = str(e).splitlines()[0]
    msg = re.sub(r"<Token token_type: TokenType\.\w+, text: ([^,]*),[^>]*>", r"'\1'", msg)
    return msg


def _real_table(scope, alias: str):
    src = scope.sources.get(alias)
    if isinstance(src, exp.Table):
        return src.name
    return None


def _closest(word: str, options: list):
    import difflib
    m = difflib.get_close_matches(word, [o.lower() for o in options], n=1, cutoff=0.6)
    if not m:
        return None
    return next(o for o in options if o.lower() == m[0])


def _column_hint(col: str, cols: dict) -> str:
    import difflib
    all_cols = {c: (t, orig) for t, cs in cols.items() for c, orig in cs.items()}
    m = difflib.get_close_matches(col.lower(), list(all_cols), n=1, cutoff=0.6)
    if m:
        t, orig = all_cols[m[0]]
        return f" Did you mean '{orig}' (in {t})?"
    return ""
