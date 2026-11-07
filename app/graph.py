"""LangGraph workflow for the SQL agent.

 User Request -> Intent Detection -> Scope Validation -(reject / clarify / info)-> Return Response
                                          |
                                   Schema Retrieval -> SQL Generation | Analyse user SQL
                                          |
                                   SQL Validation <-> SQL Repair (max 2 retries)
                                          |
                                   SQL Optimization -> Execute Query -> Generate Explanation -> Return Response
"""
import operator
from typing import Annotated, Optional, TypedDict

import sqlglot
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import db, llm, prompts
from .validator import is_destructive_request, is_injection_attempt, validate_sql

MAX_REPAIR_RETRIES = 2
HISTORY_TURNS = 6
VALID_INTENTS = {"generate", "optimize", "debug", "schema_info", "sql_concept", "help", "destructive", "out_of_scope"}


class AgentState(TypedDict, total=False):
    # persisted across turns (thanks to the checkpointer)
    messages: Annotated[list, operator.add]
    last_sql: Optional[str]
    # per-turn values (reset by the caller on every request, see new_turn_input)
    user_input: str
    dialect: str
    execute: bool
    intent: str
    needs_clarification: bool
    clarification: Optional[str]
    schema_text: str
    sql: Optional[str]
    issue: Optional[str]
    suggestions: list
    warnings: list
    validation_errors: list
    retries: int
    plan: list
    results: Optional[dict]
    explanation: str
    status: str
    message: str
    response: dict


def new_turn_input(user_input: str, dialect: str = "sqlite", execute: bool = True) -> dict:
    return {
        "user_input": user_input, "dialect": dialect, "execute": execute,
        "intent": "", "needs_clarification": False, "clarification": None, "schema_text": "",
        "sql": None, "issue": None, "suggestions": [], "warnings": [], "validation_errors": [],
        "retries": 0, "plan": [], "results": None, "explanation": "", "status": "", "message": "",
        "response": {},
    }


# ------------------------------------------------------------------ helpers
def _history(state: AgentState) -> str:
    lines = []
    for m in (state.get("messages") or [])[-HISTORY_TURNS:]:
        if m["role"] == "user":
            lines.append(f"User: {m['content']}")
        else:
            sql = f"\n  SQL returned: {m['sql']}" if m.get("sql") else ""
            lines.append(f"Assistant: {m['content'][:300]}{sql}")
    return "\n".join(lines)


def _clean_sql(sql: Optional[str]) -> Optional[str]:
    if not sql or not isinstance(sql, str):
        return None
    sql = sql.strip().rstrip(";").strip()
    return sql or None


# ------------------------------------------------------------------ nodes
def intent_detection(state: AgentState) -> dict:
    text = state["user_input"]
    # deterministic gates first: cheap, and they cannot be talked out of it
    if is_injection_attempt(text):
        return {"intent": "injection"}
    if is_destructive_request(text):
        return {"intent": "destructive"}

    schema_text = db.get_schema_text()
    prompt = prompts.intent_user(text, _history(state), state.get("last_sql"), schema_text)
    data = None
    for _ in range(2):
        data = llm.parse_json(llm.chat(prompts.INTENT_SYSTEM, prompt))
        if data and data.get("intent") in VALID_INTENTS:
            break
        data = None
    if not data:  # model could not classify -> be safe, refuse politely
        return {"intent": "out_of_scope"}
    return {
        "intent": data["intent"],
        "needs_clarification": bool(data.get("needs_clarification")),
        "clarification": data.get("clarification_question"),
    }


def route_scope(state: AgentState) -> str:
    """Scope validation: decide where an in-scope / out-of-scope request goes."""
    intent = state["intent"]
    if intent == "injection":
        return "reject_injection"
    if intent == "destructive":
        return "reject_destructive"
    if intent == "out_of_scope":
        return "reject_out_of_scope"
    if intent in ("schema_info", "sql_concept", "help"):
        return "answer_info"
    if intent == "generate" and state.get("needs_clarification"):
        return "ask_clarification"
    return "schema_retrieval"


def reject_out_of_scope(state):
    return {"status": "refused", "message": prompts.OUT_OF_SCOPE_MESSAGE}


def reject_destructive(state):
    return {"status": "refused", "message": prompts.DESTRUCTIVE_MESSAGE}


def reject_injection(state):
    return {"status": "refused", "message": prompts.INJECTION_MESSAGE}


def ask_clarification(state):
    q = state.get("clarification") or "Could you tell me a bit more about what you'd like to see?"
    return {"status": "clarification", "message": q, "sql": None}


def answer_info(state):
    text = llm.chat(prompts.INFO_SYSTEM,
                    prompts.info_user(state["user_input"], db.get_schema_text(), _history(state)))
    return {"status": "info", "message": text.strip()}


def schema_retrieval(state):
    return {"schema_text": db.get_schema_text()}


def route_after_schema(state):
    return "sql_generation" if state["intent"] == "generate" else "analyze_user_sql"


def sql_generation(state):
    raw = llm.chat(prompts.GENERATE_SYSTEM,
                   prompts.generate_user(state["user_input"], state["schema_text"],
                                         _history(state), state.get("last_sql")))
    data = llm.parse_json(raw) or {}
    sql = _clean_sql(data.get("sql"))
    if not sql:
        return {"sql": None, "clarification": data.get("clarification")
                or "I couldn't map that request to the database. Could you rephrase it?"}
    return {"sql": sql}


def analyze_user_sql(state):
    mode = "optimize" if state["intent"] == "optimize" else "debug"
    raw = llm.chat(prompts.ANALYZE_SYSTEM,
                   prompts.analyze_user(mode, state["user_input"], state["schema_text"], _history(state)))
    data = llm.parse_json(raw) or {}
    sql = _clean_sql(data.get("sql"))
    if not sql:
        return {"sql": None, "clarification": data.get("clarification")
                or "Please paste the SQL query you'd like me to look at."}
    return {"sql": sql, "issue": data.get("issue"),
            "suggestions": [s for s in (data.get("suggestions") or []) if isinstance(s, str)][:6]}


def route_after_sql(state):
    return "ask_clarification" if not state.get("sql") else "sql_validation"


def sql_validation(state):
    res = validate_sql(state["sql"], dialect="sqlite")
    return {"validation_errors": res.errors, "warnings": res.warnings}


def route_after_validation(state):
    if not state["validation_errors"]:
        return "sql_optimization" if state["intent"] == "generate" else "execute_query"
    if state["retries"] < MAX_REPAIR_RETRIES:
        return "sql_repair"
    return "validation_failed"


def sql_repair(state):
    raw = llm.chat(prompts.REPAIR_SYSTEM,
                   prompts.repair_user(state["sql"], state["validation_errors"],
                                       state["schema_text"], state["user_input"]))
    data = llm.parse_json(raw) or {}
    fixed = _clean_sql(data.get("sql")) or state["sql"]
    return {"sql": fixed, "retries": state["retries"] + 1}


def validation_failed(state):
    errs = "; ".join(state["validation_errors"])
    return {"status": "error", "sql": None,
            "message": ("I couldn't produce a query that passes validation, so I'm not going to show you an "
                        f"unverified one. Last problem found: {errs} Try rephrasing, or ask what tables and columns exist.")}


def sql_optimization(state):
    """LLM review of a valid query. Its answer is only accepted if it also passes validation."""
    sql = state["sql"]
    try:
        plan = db.explain_plan(sql)
    except Exception:
        plan = []
    data = llm.parse_json(llm.chat(prompts.OPTIMIZE_SYSTEM,
                                   prompts.optimize_user(sql, state["schema_text"], plan))) or {}
    candidate = _clean_sql(data.get("sql"))
    out = {"suggestions": [s for s in (data.get("suggestions") or []) if isinstance(s, str)][:4]}
    if candidate and candidate != sql:
        res = validate_sql(candidate, dialect="sqlite")
        if res.valid:
            out.update({"sql": candidate, "warnings": list(dict.fromkeys(state["warnings"] + res.warnings))})
    return out


def execute_query(state):
    sql = state["sql"]
    out: dict = {}
    try:
        plan = db.explain_plan(sql)
        out["plan"] = plan
        scans = [p for p in plan if p.startswith("SCAN") and "USING" not in p]
        if scans and state["intent"] == "generate":
            out["suggestions"] = state["suggestions"] + [
                "Query plan: " + "; ".join(scans[:3]) + " (full table scan, fine for small tables)."][:5]
    except Exception:
        pass
    if state.get("execute", True):
        try:
            out["results"] = db.execute_query(sql)
        except Exception as e:
            out["results"] = None
            out["warnings"] = state["warnings"] + [f"The query is valid but could not be executed: {e}"]
    return out


def generate_explanation(state):
    text = llm.chat(prompts.EXPLAIN_SYSTEM, prompts.explain_user(state["sql"])).strip()
    return {"explanation": text, "status": "ok"}


def _display_sql(sql: str, dialect: str) -> str:
    try:
        return sqlglot.transpile(sql, read="sqlite", write=dialect, pretty=len(sql) > 80)[0]
    except Exception:
        return sql


def return_response(state):
    status = state.get("status") or "ok"
    sql = state.get("sql") if status == "ok" else None
    dialect = state.get("dialect", "sqlite")
    shown = _display_sql(sql, dialect) if sql else None

    if status == "ok":
        intent = state["intent"]
        lead = {"generate": "Here's the query.", "optimize": "Here's the optimised query.",
                "debug": state.get("issue") or "Here's the checked query."}.get(intent, "Here's the query.")
        message = f"{lead}\n\n{state['explanation']}" if intent != "debug" else \
            f"{state.get('issue') or ''}\n\n{state['explanation']}".strip()
    else:
        message = state.get("message", "")

    response = {
        "status": status, "intent": state.get("intent"), "message": message,
        "sql": shown, "sql_sqlite": sql, "dialect": dialect,
        "explanation": state.get("explanation") or None,
        "issue": state.get("issue"),
        "suggestions": state.get("suggestions") or [],
        "warnings": state.get("warnings") or [],
        "plan": state.get("plan") or [],
        "results": state.get("results"),
    }
    new_msgs = [{"role": "user", "content": state["user_input"]},
                {"role": "assistant", "content": message, "sql": sql}]
    update = {"response": response, "messages": new_msgs}
    if sql:
        update["last_sql"] = sql
    return update


# ------------------------------------------------------------------ graph
def build_graph(checkpointer=None):
    g = StateGraph(AgentState)
    for name, fn in [
        ("intent_detection", intent_detection), ("reject_out_of_scope", reject_out_of_scope),
        ("reject_destructive", reject_destructive), ("reject_injection", reject_injection),
        ("ask_clarification", ask_clarification), ("answer_info", answer_info),
        ("schema_retrieval", schema_retrieval), ("sql_generation", sql_generation),
        ("analyze_user_sql", analyze_user_sql), ("sql_validation", sql_validation),
        ("sql_repair", sql_repair), ("validation_failed", validation_failed),
        ("sql_optimization", sql_optimization), ("execute_query", execute_query),
        ("generate_explanation", generate_explanation), ("return_response", return_response),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "intent_detection")
    g.add_conditional_edges("intent_detection", route_scope, {
        "reject_out_of_scope": "reject_out_of_scope", "reject_destructive": "reject_destructive",
        "reject_injection": "reject_injection", "ask_clarification": "ask_clarification",
        "answer_info": "answer_info", "schema_retrieval": "schema_retrieval"})
    g.add_conditional_edges("schema_retrieval", route_after_schema,
                            {"sql_generation": "sql_generation", "analyze_user_sql": "analyze_user_sql"})
    g.add_conditional_edges("sql_generation", route_after_sql,
                            {"ask_clarification": "ask_clarification", "sql_validation": "sql_validation"})
    g.add_conditional_edges("analyze_user_sql", route_after_sql,
                            {"ask_clarification": "ask_clarification", "sql_validation": "sql_validation"})
    g.add_conditional_edges("sql_validation", route_after_validation, {
        "sql_optimization": "sql_optimization", "execute_query": "execute_query",
        "sql_repair": "sql_repair", "validation_failed": "validation_failed"})
    g.add_edge("sql_repair", "sql_validation")
    g.add_edge("sql_optimization", "execute_query")
    g.add_edge("execute_query", "generate_explanation")
    for terminal in ("reject_out_of_scope", "reject_destructive", "reject_injection", "ask_clarification",
                     "answer_info", "validation_failed", "generate_explanation"):
        g.add_edge(terminal, "return_response")
    g.add_edge("return_response", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def run_turn(session_id: str, message: str, dialect: str = "sqlite", execute: bool = True) -> dict:
    cfg = {"configurable": {"thread_id": session_id}}
    out = get_graph().invoke(new_turn_input(message, dialect, execute), cfg)
    return out["response"]


def stream_turn(session_id: str, message: str, dialect: str = "sqlite", execute: bool = True):
    """Yields ('step', node_name) for progress and finally ('final', response_dict)."""
    cfg = {"configurable": {"thread_id": session_id}}
    final = None
    for update in get_graph().stream(new_turn_input(message, dialect, execute), cfg, stream_mode="updates"):
        for node, val in update.items():
            if node == "return_response":
                final = val["response"]
            yield "step", node
    yield "final", final


def get_history(session_id: str) -> list:
    snap = get_graph().get_state({"configurable": {"thread_id": session_id}})
    return (snap.values or {}).get("messages", []) if snap else []
