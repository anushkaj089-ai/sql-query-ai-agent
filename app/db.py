"""SQLite access layer. The agent only ever gets a READ-ONLY connection."""
import os
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

from .seed import seed

BASE = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE / "data" / "company.db"))
SCHEMA_FILE = BASE / "schema.sql"
MAX_ROWS = int(os.environ.get("MAX_ROWS", "100"))
QUERY_TIMEOUT_SEC = float(os.environ.get("QUERY_TIMEOUT_SEC", "5"))

_init_lock = threading.Lock()


def init_db(force: bool = False) -> None:
    """Create and seed the database file if it does not exist yet."""
    with _init_lock:
        path = Path(DB_PATH)
        if path.exists() and not force:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA_FILE.read_text())
        seed(conn)
        conn.close()
        get_schema_info.cache_clear()


def get_ro_conn() -> sqlite3.Connection:
    init_db()
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


@lru_cache(maxsize=1)
def get_schema_info() -> dict:
    """{'tables': {Table: {'columns': [{name,type,pk}], 'fks': [...]}}, 'samples': {...}}"""
    conn = get_ro_conn()
    tables = {}
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in names:
        cols = [{"name": r[1], "type": r[2] or "TEXT", "pk": bool(r[5])}
                for r in conn.execute(f'PRAGMA table_info("{t}")')]
        fks = [{"from": r[3], "to_table": r[2], "to": r[4]}
               for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')]
        tables[t] = {"columns": cols, "fks": fks}
    # Low-cardinality text columns: show the real values so the LLM uses them exactly
    samples = {}
    for t, info in tables.items():
        for c in info["columns"]:
            if c["type"].upper() != "TEXT" or c["pk"]:
                continue
            vals = [r[0] for r in conn.execute(
                f'SELECT DISTINCT "{c["name"]}" FROM "{t}" WHERE "{c["name"]}" IS NOT NULL LIMIT 14')]
            if 0 < len(vals) <= 12:
                samples[f"{t}.{c['name']}"] = vals
    conn.close()
    return {"tables": tables, "samples": samples}


def get_schema_text() -> str:
    info = get_schema_info()
    lines = []
    for t, d in info["tables"].items():
        cols = ", ".join(f"{c['name']} {c['type']}{' PK' if c['pk'] else ''}" for c in d["columns"])
        lines.append(f"Table {t} ({cols})")
        for fk in d["fks"]:
            lines.append(f"  FK: {t}.{fk['from']} -> {fk['to_table']}.{fk['to']}")
    if info["samples"]:
        lines.append("Known values of categorical columns (use these exact spellings):")
        for k, v in info["samples"].items():
            lines.append(f"  {k}: " + ", ".join(repr(x) for x in v))
    lines.append("Date columns hold ISO text 'YYYY-MM-DD' (compare as strings, e.g. HireDate >= '2024-01-01').")
    return "\n".join(lines)


def execute_query(sql: str, limit: int = MAX_ROWS, timeout: float = QUERY_TIMEOUT_SEC) -> dict:
    """Run a validated SELECT with a row cap and a wall-clock timeout."""
    import time
    conn = get_ro_conn()
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = [list(r) for r in rows[:limit]]
        return {"columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated}
    finally:
        conn.close()


def explain_plan(sql: str) -> list:
    """SQLite's query plan, one string per step (used for cost hints)."""
    conn = get_ro_conn()
    try:
        return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql)]
    finally:
        conn.close()
