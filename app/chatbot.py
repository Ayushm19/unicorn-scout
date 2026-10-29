"""One chat turn: sanitize -> guard -> route (LLM) -> retrieve (SQL + pgvector) -> answer (LLM) -> log.

Conversation state is rebuilt from `chat_logs` on every turn, so the app stays stateless
(required on serverless). "Which of these..." follow-ups resolve against the company ids
stored with the previous answer.

`handle_turn` optionally takes an `emit(event, data)` callback. The streaming endpoint uses it to
push pipeline stages and answer tokens to the UI as they happen; the JSON endpoint ignores it.
"""
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from typing import Literal

from langfuse import get_client, propagate_attributes
from pydantic import BaseModel, ValidationError

from app import config, db, llm, prompts
from app.data import SECTOR_GROUPS, normalize_city
from app.embeddings import embed_query
from app.sanitize import REJECTION_MESSAGES, sanitize

log = logging.getLogger("app.chatbot")
Emit = Callable[[str, dict], None]

GREETING = ("Hi! I can answer questions about 118 Indian unicorn startups (Tracxn, May 2025). Try:\n"
            "- *What does Razorpay do?*\n- *List fintech unicorns*\n- *Compare Zepto and Blinkit*\n"
            "- *Which company would be a good fit for a logistics collaboration?*")
OFF_TOPIC = ("I can only answer questions about the Indian unicorn startups in my dataset, such as what a "
             "company does, or listing companies by sector, city, stage or investor.")
ERROR_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."
DEFAULT_CLARIFY = "Could you tell me a bit more about what you're looking for (e.g. sector, city, or the kind of collaboration)?"

_STAGE_ALIASES = {"ipo": "Public", "listed": "Public", "public": "Public", "acquired": "Acquired"}
_COUNTRY_ALIASES = {"us": "United States", "usa": "United States", "america": "United States",
                    "united states": "United States", "india": "India", "singapore": "Singapore"}
_CARD_FIELDS = ("id", "name", "sector", "sector_group", "city", "country", "stage", "founded_year", "description",
                "valuation", "total_funding", "annual_revenue", "employee_count", "investors", "profile_url")


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
    clarifying_options: list[str] | None = None


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


def _tally(rows: list[dict], key: str) -> str:
    return ", ".join(f"{k} {n}" for k, n in Counter(r[key] for r in rows).most_common(6))


def _format_records(rows: list[dict]) -> str:
    if len(rows) > 6:  # compact one-liners keep long lists inside the free-tier token budget
        # Precomputed tallies: LLMs miscount long lists, so give them the arithmetic.
        totals = (f"Totals ({len(rows)} companies). By city: {_tally(rows, 'city')}. "
                  f"By sector: {_tally(rows, 'sector')}. By stage: {_tally(rows, 'stage')}. "
                  f"By founding year: {_tally(rows, 'founded_year')}.\n")
        return totals + "\n".join(f"- {r['name']} | {r['sector']} ({r['sector_group']}) | {r['city']}, "
                                  f"{r['country']} | {r['stage']} | founded {r['founded_year']}" for r in rows)
    fields = ["sector", "sector_group", "city", "country", "founded_year", "stage", "description", "unicorn_info",
              "valuation", "annual_revenue", "total_funding", "employee_count", "latest_funding_round",
              "time_to_unicorn", "investors"]
    return "\n\n".join(f"### {r['name']}\n" + "\n".join(f"{k}: {r[k]}" for k in fields if r.get(k))
                       for r in rows)


def company_card(r: dict) -> dict:
    return {k: r.get(k) for k in _CARD_FIELDS}


def _context(res: llm.LLMResult, parts: dict[str, str]) -> dict:
    """What filled the model's context window on the call that produced the reply.

    Section sizes are estimated from characters, then scaled to the provider's real prompt_tokens.
    """
    est = {k: max(1, len(v) // 4) for k, v in parts.items()}
    scale = res.prompt_tokens / sum(est.values()) if res.prompt_tokens else 1
    return {"model": res.model, "provider": res.provider, "window": res.context_window,
            "used": res.prompt_tokens or sum(est.values()), "output": res.completion_tokens,
            "parts": [{"label": k, "tokens": round(v * scale)} for k, v in est.items()]}


@contextmanager
def _stage(emit: Emit, timings: dict, stage_id: str, label: str):
    """Time a pipeline stage and report start/done to the UI; `detail` dict is sent with 'done'."""
    emit("stage", {"id": stage_id, "status": "start", "label": label})
    t0, detail = time.perf_counter(), {}
    yield detail
    timings[stage_id] = int((time.perf_counter() - t0) * 1000)
    emit("stage", {"id": stage_id, "status": "done", "label": label, "ms": timings[stage_id], **detail})


def _retrieve(route: Route, question: str, prev_ids: list[int], filters: dict) -> tuple[list[dict], str]:
    lf = get_client()
    with lf.start_as_current_observation(name="retrieve", as_type="retriever",
                                         input={"intent": route.intent, "filters": filters,
                                                "search_query": route.search_query}) as span:
        scope = prev_ids if route.use_previous_results and prev_ids else None
        rows, method = [], "name"
        if route.intent == "company_lookup":
            rows = db.find_companies_by_name(route.companies or [])
        if route.intent == "filter_list" and filters:
            qv = embed_query(route.search_query) if route.search_query else None
            rows, method = db.search_companies(filters, qv, scope, limit=120), "sql+vector" if qv else "sql"
        elif not rows:  # recommendation, unmatched lookup, or a list request with only a semantic need
            limit = 6 if route.intent == "recommendation" else 3 if route.intent == "company_lookup" else 10
            rows = db.search_companies(filters, embed_query(route.search_query or question), scope, limit=limit)
            method = "sql+vector" if filters else "vector"
        span.update(output=[r["name"] for r in rows])
        return rows, method


def _turn(session_id: str, raw: object, rec: dict, emit: Emit) -> dict:
    lf, timings = get_client(), rec["stage_ms"]

    with _stage(emit, timings, "sanitize", "Checking your message") as d:
        s = sanitize(raw)
        rec["sanitized_query"] = s.text
        d.update(ok=s.ok, reason=s.reason)
    if not s.ok:
        rec.update(outcome="blocked" if s.reason == "injection" else "rejected", intent=s.reason)
        return {"reply": REJECTION_MESSAGES[s.reason]}

    with _stage(emit, timings, "guard", "Screening for prompt injection") as d, \
            lf.start_as_current_observation(name="prompt-guard", as_type="guardrail", input=s.text) as g:
        score = llm.prompt_guard_score(s.text)
        g.update(output={"score": score})
        d.update(score=score)
    if score is not None and score >= config.PROMPT_GUARD_THRESHOLD:
        rec.update(outcome="blocked", intent="injection")
        return {"reply": REJECTION_MESSAGES["injection"]}

    with _stage(emit, timings, "memory", "Recalling the conversation") as d:
        turns = db.recent_turns(session_id, config.HISTORY_TURNS)
        # Working set for "these/those": the latest result list within the remembered turns.
        prev_ids = next((t["retrieved_ids"] for t in reversed(turns) if t["retrieved_ids"]), [])
        prev_names = [r["name"] for r in db.companies_by_ids(prev_ids)]
        history = _history_text(turns)
        d.update(turns=len(turns), working_set=len(prev_ids))

    with _stage(emit, timings, "route", "Understanding the question") as d:
        router_prompt = prompts.get_prompt("router")
        rec["prompt_versions"] = {"router": prompts.version_of(router_prompt)}
        names = db.company_names()
        messages = router_prompt.compile(sector_groups=", ".join(SECTOR_GROUPS), company_names=names,
                                         history=history, previous_results=", ".join(prev_names) or "(none)",
                                         question=s.text)
        res = llm.chat(messages, name="router", json_mode=True, prompt=router_prompt, max_tokens=1000,
                       temperature=0)
        rec.update(provider=res.provider, model=res.model, used_fallback=res.used_fallback,
                   prompt_tokens=res.prompt_tokens, completion_tokens=res.completion_tokens)
        rec["context"] = _context(res, {"Instructions": messages[0]["content"].replace(names, ""),
                                        "Company index": names, "Conversation memory": history,
                                        "Your question": s.text})
        try:
            route = Route.model_validate(json.loads(res.text))
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning("router output invalid, falling back to semantic search",
                        extra={"ctx": {"raw": res.text[:500], "error": str(e)[:300]}})
            route = Route(intent="recommendation", search_query=s.text)
        filters = normalize_filters(route.filters)
        rec.update(intent=route.intent, filters=filters or None)
        d.update(intent=route.intent, filters=filters, companies=route.companies or [],
                 follow_up=route.use_previous_results and bool(prev_ids), provider=res.provider)

    if route.intent == "greeting":
        rec["outcome"] = "greeting"
        return {"reply": GREETING}
    if route.intent == "off_topic":
        rec["outcome"] = "off_topic"
        return {"reply": OFF_TOPIC}
    if route.intent == "clarify" or (route.intent == "recommendation" and not (route.search_query or filters)):
        rec.update(outcome="clarification", clarifying_options=(route.clarifying_options or [])[:4])
        return {"reply": route.clarifying_question or DEFAULT_CLARIFY}

    with _stage(emit, timings, "retrieve", "Searching 118 companies") as d:
        rows, method = _retrieve(route, s.text, prev_ids, filters)
        rec["retrieved_ids"] = [r["id"] for r in rows]
        d.update(count=len(rows), method=method, scoped=bool(route.use_previous_results and prev_ids))
    cards = [company_card(r) for r in rows]
    emit("companies", {"intent": route.intent, "filters": filters, "companies": cards})
    if not rows:
        rec["outcome"] = "answered"
        return {"reply": "I couldn't find any companies in the dataset matching that. "
                         "Try a broader sector or a different city.", "companies": []}

    with _stage(emit, timings, "answer", "Writing the answer") as d:
        answer_prompt = prompts.get_prompt("answer")
        rec["prompt_versions"]["answer"] = prompts.version_of(answer_prompt)
        records = _format_records(rows)
        messages = answer_prompt.compile(history=history, intent=route.intent, count=str(len(rows)),
                                         records=records, question=s.text)
        res = llm.chat(messages, name="answer", prompt=answer_prompt, max_tokens=1500,
                       on_token=lambda t: emit("token", {"text": t}))
        rec.update(provider=res.provider, model=res.model, used_fallback=rec["used_fallback"] or res.used_fallback,
                   prompt_tokens=rec["prompt_tokens"] + res.prompt_tokens,
                   completion_tokens=rec["completion_tokens"] + res.completion_tokens, outcome="answered")
        rec["context"] = _context(res, {"Instructions": messages[0]["content"], "Conversation memory": history,
                                        "Retrieved companies": records, "Your question": s.text})
        d.update(tokens=res.completion_tokens, provider=res.provider)
    return {"reply": res.text.strip(), "companies": cards}


def handle_turn(session_id: str, message: object, emit: Emit | None = None) -> dict:
    """Run one turn end to end. Never raises: failures become a logged 'error' outcome."""
    emit = emit or (lambda event, data: None)
    t0 = time.perf_counter()
    lf = get_client()
    rec = {"session_id": session_id, "user_query": str(message)[:2000], "outcome": "error", "retrieved_ids": [],
           "used_fallback": False, "prompt_tokens": 0, "completion_tokens": 0, "stage_ms": {}}
    with lf.start_as_current_observation(name="chat-turn", as_type="chain", input=rec["user_query"]) as root, \
            propagate_attributes(session_id=session_id, trace_name="chat-turn"):
        try:
            result = _turn(session_id, message, rec, emit)
        except Exception as e:
            log.exception("chat turn failed", extra={"ctx": {"session_id": session_id}})
            rec.update(outcome="error", error=repr(e)[:1000])
            result = {"reply": ERROR_REPLY}
        rec["response"] = result["reply"]
        rec["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        root.update(output=result["reply"], metadata={k: rec.get(k) for k in
                                                      ("outcome", "intent", "filters", "prompt_versions",
                                                       "provider", "used_fallback", "latency_ms", "stage_ms")})
        lf.score_current_trace(name="outcome", value=rec["outcome"], data_type="CATEGORICAL")
        rec["trace_id"] = lf.get_current_trace_id()

    try:
        db.insert_chat_log(rec)
    except Exception:
        log.exception("failed to write chat log", extra={"ctx": {"session_id": session_id}})
    log.info("chat turn", extra={"ctx": {k: rec.get(k) for k in
                                         ("session_id", "outcome", "intent", "latency_ms", "provider",
                                          "used_fallback", "trace_id")}})
    return {"reply": result["reply"], "companies": result.get("companies", []), "session_id": session_id,
            **{k: rec.get(k) for k in ("outcome", "intent", "filters", "clarifying_options", "latency_ms",
                                       "stage_ms", "prompt_tokens", "completion_tokens", "context",
                                       "prompt_versions", "provider", "model", "used_fallback", "trace_id")},
            "trace_url": lf.get_trace_url(trace_id=rec["trace_id"]) if rec.get("trace_id") else None}
