"""HTTP API. Vercel picks up `app` from app/main.py; public/ is served by Vercel's CDN."""
import json
import logging
import secrets
import threading
from pathlib import Path
from queue import Queue
from uuid import UUID, uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, StreamingResponse
from langfuse import get_client
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import auth, chatbot, config, db
from app.observability import setup_logging

setup_logging(to_db=bool(config.DATABASE_URL))
if not config.SESSION_SECRET:
    logging.getLogger("app.main").warning("SESSION_SECRET not set; sessions reset whenever the server restarts")
app = FastAPI(title="Unicorn Scout")
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET or secrets.token_urlsafe(32),
                   session_cookie="scout_session", max_age=60 * 60 * 24 * 30, same_site="lax",
                   https_only=config.ON_VERCEL)
app.include_router(auth.router)
PUBLIC = Path(__file__).parent.parent / "public"


class ChatIn(BaseModel):
    message: str = Field(max_length=4000)  # hard cap; sanitize() applies the friendlier 500-char limit
    session_id: UUID | None = None


def _start_turn(request: Request, body: ChatIn) -> tuple[str, str]:
    """Resolve the user (creating a guest if needed) and claim the chat for them. 404 if it's someone else's."""
    user = auth.current_user(request, create=True)
    session_id = str(body.session_id or uuid4())
    title = " ".join(body.message.split())[:80] or "New chat"
    if not db.claim_session(session_id, user["id"], title):
        raise HTTPException(404, "Chat not found")
    return session_id, user["id"]


@app.post("/api/chat")
def chat(request: Request, body: ChatIn, background: BackgroundTasks) -> dict:
    """Plain JSON: one request, one complete answer."""
    session_id, user_id = _start_turn(request, body)
    out = chatbot.handle_turn(session_id, body.message, user_id=user_id)
    background.add_task(get_client().flush)  # ship Langfuse spans before the function is frozen
    return out


@app.post("/api/chat/stream")
def chat_stream(request: Request, body: ChatIn) -> StreamingResponse:
    """Server-sent events: `stage` (pipeline progress), `companies`, `token` (answer text), then `result`.

    The turn runs in a worker thread so its Langfuse/OTel context stays on one thread; the
    response generator only relays queued events.
    """
    session_id, user_id = _start_turn(request, body)
    events: Queue = Queue()

    def work() -> None:
        try:
            events.put(("result", chatbot.handle_turn(session_id, body.message, lambda e, d: events.put((e, d)),
                                                      user_id=user_id)))
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


@app.get("/api/sessions")
def sessions(request: Request) -> list[dict]:
    """The current user's chats, newest first, for the sidebar."""
    user = auth.current_user(request)
    return db.list_sessions(user["id"]) if user else []


@app.delete("/api/sessions/{session_id}")
def delete_session(request: Request, session_id: UUID) -> dict:
    user = auth.current_user(request)
    if not user or not db.delete_session(str(session_id), user["id"]):
        raise HTTPException(404, "Chat not found")
    return {"ok": True}


@app.get("/api/sessions/{session_id}")
def history(request: Request, session_id: UUID) -> list[dict]:
    """Every turn of a session with its company cards, so the UI can rebuild itself after a reload."""
    user = auth.current_user(request)
    if not user or not db.owns_session(str(session_id), user["id"]):
        raise HTTPException(404, "Chat not found")
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


# Pages and assets. On Vercel the CDN serves public/ first (cleanUrls maps /chat to chat.html); these
# routes cover local dev. no-cache = always revalidate, so an edit or deploy shows on the next load.
_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", include_in_schema=False)
def landing() -> FileResponse:
    return FileResponse(PUBLIC / "index.html", headers=_NO_CACHE)


@app.get("/chat", include_in_schema=False)
def chat_page() -> FileResponse:
    return FileResponse(PUBLIC / "chat.html", headers=_NO_CACHE)


@app.get("/{name}.{ext}", include_in_schema=False)
def static_file(name: str, ext: str) -> FileResponse:
    path = (PUBLIC / f"{name}.{ext}").resolve()
    if path.parent != PUBLIC.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers=_NO_CACHE)


@app.exception_handler(StarletteHTTPException)
async def page_not_found(request: Request, exc: StarletteHTTPException):
    """Unknown page URLs get a real 404 page; API and auth routes keep their JSON errors."""
    if exc.status_code == 404 and not request.url.path.startswith(("/api/", "/auth/")):
        return FileResponse(PUBLIC / "404.html", status_code=404, headers=_NO_CACHE)
    return await http_exception_handler(request, exc)
