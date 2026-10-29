"""HTTP API. Vercel picks up `app` from app/main.py; public/ is served by Vercel's CDN."""
import json
import threading
from pathlib import Path
from queue import Queue
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from langfuse import get_client
from pydantic import BaseModel, Field

from app import chatbot, config, db
from app.observability import setup_logging

setup_logging(to_db=bool(config.DATABASE_URL))
app = FastAPI(title="Unicorn Scout")
PUBLIC = Path(__file__).parent.parent / "public"


class ChatIn(BaseModel):
    message: str = Field(max_length=4000)  # hard cap; sanitize() applies the friendlier 500-char limit
    session_id: UUID | None = None


@app.post("/api/chat")
def chat(body: ChatIn, background: BackgroundTasks) -> dict:
    """Plain JSON: one request, one complete answer."""
    out = chatbot.handle_turn(str(body.session_id or uuid4()), body.message)
    background.add_task(get_client().flush)  # ship Langfuse spans before the function is frozen
    return out


@app.post("/api/chat/stream")
def chat_stream(body: ChatIn) -> StreamingResponse:
    """Server-sent events: `stage` (pipeline progress), `companies`, `token` (answer text), then `result`.

    The turn runs in a worker thread so its Langfuse/OTel context stays on one thread; the
    response generator only relays queued events.
    """
    session_id = str(body.session_id or uuid4())
    events: Queue = Queue()

    def work() -> None:
        try:
            events.put(("result", chatbot.handle_turn(session_id, body.message, lambda e, d: events.put((e, d)))))
        finally:
            events.put(None)
            get_client().flush()

    threading.Thread(target=work, daemon=True).start()

    def relay():
        yield f"event: session\ndata: {json.dumps({'session_id': session_id})}\n\n"
        while (item := events.get()) is not None:
            yield f"event: {item[0]}\ndata: {json.dumps(item[1], default=str)}\n\n"

    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/sessions/{session_id}")
def history(session_id: UUID) -> list[dict]:
    """Every turn of a session with its company cards, so the UI can rebuild itself after a reload."""
    turns = db.session_history(str(session_id))
    cards = {c["id"]: chatbot.company_card(c)
             for c in db.companies_by_ids(sorted({i for t in turns for i in t["retrieved_ids"]}))}
    lf = get_client()
    return [{**t, "companies": [cards[i] for i in t["retrieved_ids"] if i in cards],
             "trace_url": lf.get_trace_url(trace_id=t["trace_id"]) if t["trace_id"] else None} for t in turns]


@app.get("/api/metrics")
def metrics() -> dict:
    return {**db.metrics(), "by_prompt_version": db.metrics_by_prompt_version()}


@app.get("/api/health")
def health() -> dict:
    db.conn().execute("SELECT 1")
    return {"ok": True}


# Local dev only: on Vercel the CDN serves public/ first (cleanUrls maps /chat to chat.html).
@app.get("/", include_in_schema=False)
def landing() -> FileResponse:
    return FileResponse(PUBLIC / "index.html")


@app.get("/chat", include_in_schema=False)
def chat_page() -> FileResponse:
    return FileResponse(PUBLIC / "chat.html")


@app.get("/{name}.{ext}", include_in_schema=False)
def static_file(name: str, ext: str) -> FileResponse:
    path = (PUBLIC / f"{name}.{ext}").resolve()
    if path.parent != PUBLIC.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
