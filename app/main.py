"""HTTP API. Vercel picks up `app` from app/main.py; public/ is served by Vercel's CDN."""
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import FileResponse
from langfuse import get_client
from pydantic import BaseModel, Field

from app import chatbot, config, db
from app.observability import setup_logging

setup_logging(to_db=bool(config.DATABASE_URL))
app = FastAPI(title="Indian Unicorn Chatbot")


class ChatIn(BaseModel):
    message: str = Field(max_length=4000)  # hard cap; sanitize() applies the friendlier 500-char limit
    session_id: UUID | None = None


@app.post("/api/chat")
def chat(body: ChatIn, background: BackgroundTasks) -> dict:
    out = chatbot.handle_turn(str(body.session_id or uuid4()), body.message)
    background.add_task(get_client().flush)  # ship Langfuse spans before the function is frozen
    return out


@app.get("/api/sessions/{session_id}")
def history(session_id: UUID) -> list[dict]:
    return [{"user": r["user_query"], "bot": r["response"], "outcome": r["outcome"], "at": r["created_at"]}
            for r in db.session_history(str(session_id))]


@app.get("/api/metrics")
def metrics() -> dict:
    return {**db.metrics(), "by_prompt_version": db.metrics_by_prompt_version()}


@app.get("/api/health")
def health() -> dict:
    db.conn().execute("SELECT 1")
    return {"ok": True}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:  # local dev only; on Vercel the CDN serves public/index.html first
    return FileResponse(Path(__file__).parent.parent / "public" / "index.html")
