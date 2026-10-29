"""One chat turn: sanitize -> guard -> route (LLM) -> retrieve (SQL + pgvector) -> answer (LLM) -> log.

Conversation state is rebuilt from `chat_logs` on every turn, so the app stays stateless
(required on serverless). "Which of these..." follow-ups resolve against the company ids
stored with the previous answer.
"""
import json
import logging
import time
from typing import Literal

from langfuse import get_client, propagate_attributes
from pydantic import BaseModel, ValidationError

from app import config, db, llm, prompts
from app.data import SECTOR_GROUPS, normalize_city
from app.embeddings import embed_query
from app.sanitize import REJECTION_MESSAGES, sanitize

log = logging.getLogger("app.chatbot")

GREETING = ("Hi! I can answer questions about 118 Indian unicorn startups (Tracxn, May 2025). Try:\n"
            "- *What does Razorpay do?*\n- *List fintech unicorns*\n- *Which of these are based in Bangalore?*\n"
            "- *Which company would be a good fit for a logistics collaboration?*")
OFF_TOPIC = ("I can only answer questions about the Indian unicorn startups in my dataset, such as what a "
             "company does, or listing companies by sector, city, stage or investor.")
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."
DEFAULT_CLARIFY = "Could you tell me a bit more about what you're looking for (e.g. sector, city, or the kind of collaboration)?"

_STAGE_ALIASES = {"ipo": "Public", "listed": "Public", "public": "Public", "acquired": "Acquired"}
_COUNTRY_ALIASES = {"us": "United States", "usa": "United States", "america": "United States",
                    "united states": "United States", "india": "India", "singapore": "Singapore"}


class Filters(BaseModel, extra="ignore"):
    sector_group: str | None = None
    sector: str | None = None
    city: str | None = None
    country: str | None = None
    stage: str | None = None
    founded_from: int | None = None  # inclusive
    founded_to: int | None = None  # inclusive
    investor: str | None = None


class Route(BaseModel, extra="ignore"):
    intent: Literal["company_lookup", "filter_list", "recommendation", "clarify", "greeting", "off_topic"]
    companies: list[str] | None = None
    filters: Filters | None = None
    use_previous_results: bool = False
    search_query: str | None = None
    clarifying_question: str | None = None


def normalize_filters(f: Filters | None) -> dict:
    """Map LLM-extracted values onto dataset spellings; drop anything we can't map."""
    if f is None:
        return {}
    out = f.model_dump(exclude_none=True)
    if "sector_group" in out:
        out["sector_group"] = next((g for g in SECTOR_GROUPS if g.lower() == out["sector_group"].lower()), None)
    if "city" in out:
        out["city"] = normalize_city(out["city"])
    if "country" in out:
        out["country"] = _COUNTRY_ALIASES.get(out["country"].lower(), out["country"])
    if "stage" in out:
        s = out["stage"].strip()
        out["stage"] = _STAGE_ALIASES.get(s.lower(), s.title())
    return {k: v for k, v in out.items() if v not in ("", None)}


def _history_text(turns: list[dict]) -> str:
    if not turns:
        return "(none)"
    return "\n".join(f"User: {t['sanitized_query'] or t['user_query']}\nAssistant: {(t['response'] or '')[:300]}"
                     for t in turns)


def _format_records(rows: list[dict]) -> str:
    if len(rows) > 6:  # compact one-liners keep long lists inside the free-tier token budget
        return "\n".join(f"- {r['name']} | {r['sector']} ({r['sector_group']}) | {r['city']}, {r['country']} | "
                         f"{r['stage']} | founded {r['founded_year']}" for r in rows)
    fields = ["sector", "sector_group", "city", "country", "founded_year", "stage", "description", "unicorn_info",
              "valuation", "annual_revenue", "total_funding", "employee_count", "latest_funding_round",
              "time_to_unicorn", "investors"]
    return "\n\n".join(f"### {r['name']}\n" + "\n".join(f"{k}: {r[k]}" for k in fields if r.get(k))
                       for r in rows)


def _retrieve(route: Route, question: str, prev_ids: list[int], filters: dict) -> list[dict]:
    lf = get_client()
    with lf.start_as_current_observation(name="retrieve", as_type="retriever",
                                         input={"intent": route.intent, "filters": filters,
                                                "search_query": route.search_query}) as span:
        scope = prev_ids if route.use_previous_results and prev_ids else None
        rows: list[dict] = []
        if route.intent == "company_lookup":
            rows = db.find_companies_by_name(route.companies or [])
        if route.intent == "filter_list" and filters:
            qv = embed_query(route.search_query) if route.search_query else None
            rows = db.search_companies(filters, qv, scope, limit=120)
        elif not rows:  # recommendation, unmatched lookup, or a list request with only a semantic need
            limit = 6 if route.intent == "recommendation" else 3 if route.intent == "company_lookup" else 10
            rows = db.search_companies(filters, embed_query(route.search_query or question), scope, limit=limit)
        span.update(output=[r["name"] for r in rows])
        return rows


def _turn(session_id: str, raw: object, rec: dict) -> dict:
    lf = get_client()
    s = sanitize(raw)
    rec["sanitized_query"] = s.text
    if not s.ok:
        rec.update(outcome="blocked" if s.reason == "injection" else "rejected", intent=s.reason)
        return {"reply": REJECTION_MESSAGES[s.reason]}

    with lf.start_as_current_observation(name="prompt-guard", as_type="guardrail", input=s.text) as g:
        score = llm.prompt_guard_score(s.text)
        g.update(output={"score": score})
    if score is not None and score >= config.PROMPT_GUARD_THRESHOLD:
        rec.update(outcome="blocked", intent="injection")
        return {"reply": REJECTION_MESSAGES["injection"]}

    turns = db.recent_turns(session_id, config.HISTORY_TURNS)
    prev_ids = db.last_result_ids(session_id)
    prev_names = [r["name"] for r in db.search_companies({}, None, prev_ids, limit=120)] if prev_ids else []
    history = _history_text(turns)

    router_prompt = prompts.get_prompt("router")
    rec["prompt_versions"] = {"router": prompts.version_of(router_prompt)}
    res = llm.chat(router_prompt.compile(sector_groups=", ".join(SECTOR_GROUPS), company_names=db.company_names(),
                                         history=history,
                                         previous_results=", ".join(prev_names) or "(none)", question=s.text),
                   name="router", json_mode=True, prompt=router_prompt, max_tokens=1000, temperature=0)
    rec.update(provider=res.provider, model=res.model, used_fallback=res.used_fallback)
    try:
        route = Route.model_validate(json.loads(res.text))
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning("router output invalid, falling back to semantic search",
                    extra={"ctx": {"raw": res.text[:500], "error": str(e)[:300]}})
        route = Route(intent="recommendation", search_query=s.text)
    filters = normalize_filters(route.filters)
    rec.update(intent=route.intent, filters=filters or None)

    if route.intent == "greeting":
        rec["outcome"] = "greeting"
        return {"reply": GREETING}
    if route.intent == "off_topic":
        rec["outcome"] = "off_topic"
        return {"reply": OFF_TOPIC}
    if route.intent == "clarify" or (route.intent == "recommendation" and not (route.search_query or filters)):
        rec["outcome"] = "clarification"
        return {"reply": route.clarifying_question or DEFAULT_CLARIFY}

    rows = _retrieve(route, s.text, prev_ids, filters)
    rec["retrieved_ids"] = [r["id"] for r in rows]
    if not rows:
        rec["outcome"] = "answered"
        return {"reply": "I couldn't find any companies in the dataset matching that. "
                         "Try a broader sector or a different city."}

    answer_prompt = prompts.get_prompt("answer")
    rec["prompt_versions"]["answer"] = prompts.version_of(answer_prompt)
    res = llm.chat(answer_prompt.compile(history=history, intent=route.intent, count=str(len(rows)),
                                         records=_format_records(rows), question=s.text),
                   name="answer", prompt=answer_prompt, max_tokens=1500)
    rec.update(provider=res.provider, model=res.model, used_fallback=rec["used_fallback"] or res.used_fallback,
               outcome="answered")
    return {"reply": res.text.strip(), "companies": [
        {"name": r["name"], "sector": r["sector"], "city": r["city"], "stage": r["stage"]} for r in rows]}


def handle_turn(session_id: str, message: object) -> dict:
    """Run one turn end to end. Never raises: failures become a logged 'error' outcome."""
    t0 = time.perf_counter()
    lf = get_client()
    rec = {"session_id": session_id, "user_query": str(message)[:2000], "outcome": "error",
           "retrieved_ids": [], "used_fallback": False}
    with lf.start_as_current_observation(name="chat-turn", as_type="chain", input=rec["user_query"]) as root, \
            propagate_attributes(session_id=session_id, trace_name="chat-turn"):
        try:
            result = _turn(session_id, message, rec)
        except Exception as e:
            log.exception("chat turn failed", extra={"ctx": {"session_id": session_id}})
            rec.update(outcome="error", error=repr(e)[:1000])
            result = {"reply": ERROR_REPLY}
        rec["response"] = result["reply"]
        rec["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        root.update(output=result["reply"], metadata={k: rec.get(k) for k in
                                                      ("outcome", "intent", "filters", "prompt_versions",
                                                       "provider", "used_fallback", "latency_ms")})
        lf.score_current_trace(name="outcome", value=rec["outcome"], data_type="CATEGORICAL")
        rec["trace_id"] = lf.get_current_trace_id()

    try:
        db.insert_chat_log(rec)
    except Exception:
        log.exception("failed to write chat log", extra={"ctx": {"session_id": session_id}})
    log.info("chat turn", extra={"ctx": {k: rec.get(k) for k in
                                         ("session_id", "outcome", "intent", "latency_ms", "provider",
                                          "used_fallback", "trace_id")}})
    return {**result, "session_id": session_id, "outcome": rec["outcome"], "latency_ms": rec["latency_ms"],
            "trace_id": rec["trace_id"]}
