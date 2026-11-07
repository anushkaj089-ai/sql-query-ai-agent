"""FastAPI app: JSON + SSE endpoints and the static frontend."""
import json
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import db, graph
from .llm import LLMError

try:  # optional .env support for local runs
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("sql-agent")
@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(title="SQL Query AI Agent", version="1.0.0", lifespan=lifespan)
FRONTEND = Path(__file__).parent.parent / "frontend"


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=2000)
    dialect: Literal["sqlite", "postgres", "mysql"] = "sqlite"
    execute: bool = True


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_configured": bool(os.environ.get("LLM_API_KEY"))}


@app.get("/api/schema")
def schema():
    info = db.get_schema_info()
    return {"tables": info["tables"], "samples": info["samples"]}


@app.get("/api/history/{session_id}")
def history(session_id: str):
    return {"messages": graph.get_history(session_id)}


@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        return graph.run_turn(req.session_id, req.message, req.dialect, req.execute)
    except LLMError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        log.exception("chat failed")
        raise HTTPException(status_code=500, detail="Something went wrong while processing your request.")


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    def events():
        try:
            for kind, payload in graph.stream_turn(req.session_id, req.message, req.dialect, req.execute):
                yield f"data: {json.dumps({'type': kind, 'node' if kind == 'step' else 'response': payload})}\n\n"
        except LLMError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        except Exception:
            log.exception("stream failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Something went wrong while processing your request.'})}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
