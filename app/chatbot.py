"""One chat turn: sanitize -> guard -> route (LLM) -> retrieve (SQL + pgvector) -> answer (LLM) -> log.

Conversation state is rebuilt from `chat_logs` on every turn, so the app stays stateless
(required on serverless). "Which of these..." follow-ups resolve against the company ids
stored with the previous answer.

`handle_turn` optionally takes an `emit(event, data)` callback. The streaming endpoint uses it to
push pipeline stages and answer tokens to the UI as they happen; the JSON endpoint ignores it.
"""
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from typing import Literal

from langfuse import get_client, propagate_attributes
from pydantic import BaseModel, ValidationError

from app import config, db, llm, prompts
from app.data import SECTOR_GROUPS, money_usd, normalize_city
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

_EXPLICIT_REF = re.compile(r"\b(these|those|them|they|above|previous|that list)\b", re.I)
_STAGE_ALIASES ={"ipo": "Public", "listed": "Public", "public": "Public", "acquired": "Acquired"}
_COUNTRY_ALIASES = {"us": "United States", "usa": "United States", "america": "United States",
                    "united states": "United States", "india": "India", "singapore": "Singapore"}
_CARD_FIELDS = ("id", "name", "sector", "sector_group", "city", "country", "stage", "founded_year", "description",
                "valuation", "total_funding", "annual_revenue", "employee_count", "investors", "profile_url")


class Filters(BaseModel, extra="ignore"):
    # Text filters take one value or a list meaning "any of" ("cybersecurity and API management ones").
    sector_group: list[str] | str | None = None
    sector: list[str] | str | None = None
    city: list[str] | str | None = None
    country: list[str] | str | None = None
    exclude_country: list[str] | str | None = None  # "outside India" -> ["India"]
    exclude_city: list[str] | str | None = None  # "not in Bangalore or Mumbai" -> ["Bangalore", "Mumbai"]
    stage: list[str] | str | None = None
    founded_from: int | None = None  # inclusive
    founded_to: int | None = None  # inclusive
    investor: str | None = None
    sort_by: Literal["valuation", "funding", "revenue", "newest", "oldest"] | None = None
    limit: int | None = None  # "top 5" -> 5


# sort_by -> (sort key, descending). Money fields sort by approximate USD; missing values go last.
_SORTS = {"valuation": (lambda r: money_usd(r["valuation"]), True),
          "funding": (lambda r: money_usd(r["total_funding"]), True),
          "revenue": (lambda r: money_usd(r["annual_revenue"]), True),
          "newest": (lambda r: r["founded_year"], True),
          "oldest": (lambda r: r["founded_year"], False)}


def sort_and_limit(rows: list[dict], filters: dict) -> list[dict]:
    if filters.get("sort_by") in _SORTS:
        key, desc = _SORTS[filters["sort_by"]]
        known = [r for r in rows if key(r) is not None]
        rows = sorted(known, key=key, reverse=desc) + [r for r in rows if key(r) is None]
    return rows[:filters["limit"]] if filters.get("limit") else rows


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
    group = lambda g: next((x for x in SECTOR_GROUPS if x.lower() == g.lower()), None)
    country = lambda c: _COUNTRY_ALIASES.get(c.lower(), c)
    stage = lambda s: _STAGE_ALIASES.get(s.lower(), s.title())
    norms = {"sector_group": group, "sector": str, "city": normalize_city, "country": country, "stage": stage,
             "exclude_city": normalize_city, "exclude_country": country}
    for key, norm in norms.items():
        if key in out:
            raw = [out[key]] if isinstance(out[key], str) else out[key]
            # No dataset value contains " or ", so "cybersecurity or api management" is two values.
            values = [norm(p.strip()) for v in raw for p in re.split(r"\s+or\s+", v, flags=re.I) if p.strip()]
            values = list(dict.fromkeys(v for v in values if v))
            # A single value stays a plain string; exclusions are always lists.
            if not values:
                out[key] = None
            elif len(values) == 1 and not key.startswith("exclude_"):
                out[key] = values[0]
            else:
                out[key] = values
    if out.get("limit") is not None and not 1 <= out["limit"] <= 118:
        del out["limit"]
    return {k: v for k, v in out.items() if v not in ("", None)}


def _history_text(turns: list[dict]) -> str:
    if not turns:
        return "(none)"
    return "\n".join(f"User: {t['sanitized_query'] or t['user_query']}\nAssistant: {(t['response'] or '')[:300]}"
                     for t in turns)


def _tally(rows: list[dict], key: str) -> str:
    return ", ".join(f"{k} {n}" for k, n in Counter(r[key] for r in rows).most_common(6))


_SORT_FIELD = {"valuation": "valuation", "funding": "total_funding", "revenue": "annual_revenue"}
_SORT_LABELS = {"valuation": "valuation, highest first", "funding": "total funding, highest first",
                "revenue": "annual revenue, highest first", "newest": "founding year, newest first",
                "oldest": "founding year, oldest first"}


def _count_sentence(n: int, prev: int, *, scoped: bool, widened: bool) -> str:
    """Opening sentence of a list answer, so the count and scope are always right."""
    match = lambda k: "matches" if k == 1 else "match"
    prev_s = "the company" if prev == 1 else f"the {prev} companies"
    if widened:
        return f"None of {prev_s} in my previous answer match, so I searched all 118: {n} {match(n)}."
    if scoped:
        return (f"All {prev} companies in my previous answer match." if n == prev > 1
                else f"Of {prev_s} in my previous answer, {n} {match(n)}.")
    return f"{n} of the 118 unicorns {match(n)}."


def _ranked_items(rows: list[dict], sort_by: str) -> str:
    """The first five in ranking order with the value they are ranked by; "…" only when more follow."""
    field = _SORT_FIELD.get(sort_by)
    value = (lambda r: f"≈ {_usd(v)}" if (v := money_usd(r[field])) else "not reported") if field else (
        lambda r: str(r["founded_year"]))
    return ", ".join(f"**{r['name']}** ({value(r)})" for r in rows[:5]) + (", …" if len(rows) > 5 else ".")


def _ranked_sentence(rows: list[dict], sort_by: str, limited: bool, prev: int = 0) -> str:
    """'Top 5 by valuation: Flipkart ≈ $36.0B, ...' so the order and values never depend on the model.
    `prev` > 0 says the ranking was taken from the previous answer's companies."""
    items = _ranked_items(rows, sort_by)
    if not limited:
        return f"Ranked by {_SORT_LABELS[sort_by]}: {items}"
    head = {"newest": f"{len(rows)} newest", "oldest": f"{len(rows)} oldest"}.get(
        sort_by, f"top {len(rows)} by {sort_by.replace('funding', 'total funding')}")
    if prev:
        return f"Of the {prev} companies in my previous answer, {head}: {items}"
    return f"{head[0].upper()}{head[1:]}: {items}"


def _usd(x: float) -> str:
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.0f}M"


def _ranking(rows: list[dict], sort_by: str | None) -> str:
    """The order and the approximate USD values, computed in code: the model reorders lists and mis-converts ₹."""
    field = _SORT_FIELD.get(sort_by)
    if not field:
        return ""
    items = [f"{i}. {r['name']} ≈ {_usd(v)}" if (v := money_usd(r[field])) else f"{i}. {r['name']} (not reported)"
             for i, r in enumerate(rows, 1)]
    return f"Ranking by {sort_by} (approximate USD, keep this order): " + "; ".join(items[:10]) + "\n\n"


def _format_records(rows: list[dict], sort_by: str | None = None) -> str:
    return _ranking(rows, sort_by) + _records(rows, sort_by)


def _records(rows: list[dict], sort_by: str | None) -> str:
    if len(rows) > 6:  # compact one-liners keep long lists inside the free-tier token budget
        years = [r["founded_year"] for r in rows if r["founded_year"]]
        # Precomputed tallies: LLMs miscount long lists, so give them the arithmetic.
        totals = (f"Totals ({len(rows)} companies). By city: {_tally(rows, 'city')}. "
                  f"By country: {_tally(rows, 'country')}. "
                  f"By sector: {_tally(rows, 'sector')}. By stage: {_tally(rows, 'stage')}. "
                  f"By founding year: {_tally(rows, 'founded_year')}. Founded "
                  f"{min(years, default='?')}-{max(years, default='?')}.\n")
        extra = _SORT_FIELD.get(sort_by)  # a ranked list must show the value it is ranked by
        return totals + "\n".join(f"- {r['name']} | {r['sector']} ({r['sector_group']}) | {r['city']}, "
                                  f"{r['country']} | {r['stage']} | founded {r['founded_year']}"
                                  + (f" | {extra} {r[extra] or 'not reported'}" if extra else "") for r in rows)
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
def _stage(emit: Emit, rec: dict, stage_id: str, label: str):
    """Time a pipeline stage and report start/done to the UI. The `detail` dict filled inside the block is
    sent with 'done' and saved with the turn, so a reloaded chat shows the same timeline."""
    emit("stage", {"id": stage_id, "status": "start", "label": label})
    t0, detail = time.perf_counter(), {}
    yield detail
    rec["stage_ms"][stage_id] = int((time.perf_counter() - t0) * 1000)
    rec["stage_detail"][stage_id] = detail
    emit("stage", {"id": stage_id, "status": "done", "label": label, "ms": rec["stage_ms"][stage_id], **detail})


def _retrieve(route: Route, question: str, prev_ids: list[int], filters: dict) -> tuple[list[dict], str]:
    lf = get_client()
    with lf.start_as_current_observation(name="retrieve", as_type="retriever",
                                         input={"intent": route.intent, "filters": filters,
                                                "search_query": route.search_query}) as span:
        scope = prev_ids if route.use_previous_results and prev_ids else None
        rows, method = [], "name"
        if route.intent == "company_lookup":
            rows = db.find_companies_by_name(route.companies or [])
        # A list request is SQL unless it is purely a semantic need: "top 5 companies" has no filter but
        # must still rank all 118, and "outside India" must never become a similarity search.
        if route.intent == "filter_list" and (filters or not route.search_query):
            qv = embed_query(route.search_query) if route.search_query else None
            rows = sort_and_limit(db.search_companies(filters, qv, scope, limit=120), filters)
            method = "sql+vector" if qv else "sql"
            if not rows and filters.get("sector"):
                # No sector is named like that ("voice AI" vs "Speech and Voice Recognition"): rank by meaning.
                rest = {k: v for k, v in filters.items() if k != "sector"}
                sector = filters["sector"] if isinstance(filters["sector"], str) else " ".join(filters["sector"])
                rows = sort_and_limit(db.search_companies(rest, embed_query(sector), scope, limit=10), rest)
                method = "vector"
        elif not rows and not (route.intent == "company_lookup" and route.companies):
            # recommendation, a lookup with no name, or a list request with only a semantic need. A named company
            # that isn't in the dataset gets no similarity fallback, so it is never answered with a different company.
            limit = 6 if route.intent == "recommendation" else 3 if route.intent == "company_lookup" else 10
            qv = embed_query(route.search_query or question)
            # A need ("last-mile delivery") belongs to the similarity search, not to the exact `sector` filter.
            sem_filters = {k: v for k, v in filters.items() if k not in ("sector", "sort_by", "limit")}
            rows = db.search_companies(sem_filters, qv, scope, limit=limit)
            if not rows and sem_filters:  # filters too strict for a needs-based question: rank all candidates
                rows = db.search_companies({}, qv, scope, limit=limit)
            method = "sql+vector" if sem_filters else "vector"
        span.update(output=[r["name"] for r in rows])
        return rows, method


def _turn(session_id: str, raw: object, rec: dict, emit: Emit) -> dict:
    lf = get_client()

    with _stage(emit, rec, "sanitize", "Checking your message") as d:
        s = sanitize(raw)
        rec["sanitized_query"] = s.text
        d.update(ok=s.ok, reason=s.reason)
    if not s.ok:
        rec.update(outcome="blocked" if s.reason == "injection" else "rejected", intent=s.reason)
        return {"reply": REJECTION_MESSAGES[s.reason]}

    with _stage(emit, rec, "guard", "Screening for prompt injection") as d, \
            lf.start_as_current_observation(name="prompt-guard", as_type="guardrail", input=s.text) as g:
        score = llm.prompt_guard_score(s.text)
        g.update(output={"score": score})
        d.update(score=score)
    if score is not None and score >= config.PROMPT_GUARD_THRESHOLD:
        rec.update(outcome="blocked", intent="injection")
        return {"reply": REJECTION_MESSAGES["injection"]}

    with _stage(emit, rec, "memory", "Recalling the conversation") as d:
        turns = db.recent_turns(session_id, config.HISTORY_TURNS)
        # Working set for "these/those": the latest result list within the remembered turns.
        prev_ids = next((t["retrieved_ids"] for t in reversed(turns) if t["retrieved_ids"]), [])
        # Answering a clarifying question completes that question; it doesn't narrow older results
        # ("top 5" -> "Highest revenue" must rank all 118), unless the clarified question said "these".
        if turns and turns[-1]["outcome"] == "clarification" and not _EXPLICIT_REF.search(
                turns[-1]["sanitized_query"] or turns[-1]["user_query"]):
            prev_ids = []
        prev_names = [r["name"] for r in db.companies_by_ids(prev_ids)]
        history = _history_text(turns)
        d.update(turns=len(turns), working_set=len(prev_ids))

    with _stage(emit, rec, "route", "Understanding the question") as d:
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

    with _stage(emit, rec, "retrieve", "Searching 118 companies") as d:
        rows, method = _retrieve(route, s.text, prev_ids, filters)
        # "What are out of India?" right after a list is read as a follow-up. If none of the previous companies
        # match and the user didn't explicitly say "these", search all 118 instead of answering "none".
        widened = False
        if not rows and route.use_previous_results and prev_ids and not _EXPLICIT_REF.search(s.text):
            route.use_previous_results, widened = False, True
            rows, method = _retrieve(route, s.text, prev_ids, filters)
        rec["retrieved_ids"] = [r["id"] for r in rows]
        d.update(count=len(rows), method=method, scoped=bool(route.use_previous_results and prev_ids))
    cards = [company_card(r) for r in rows]
    emit("companies", {"intent": route.intent, "filters": filters, "companies": cards})
    if not rows:
        rec["outcome"] = "answered"
        if route.intent == "company_lookup" and route.companies:
            names = " and ".join(route.companies)
            return {"reply": f"{names} {'is' if len(route.companies) == 1 else 'are'} not among the 118 unicorns in "
                             "my dataset (Tracxn, May 2025), so I can't answer that.", "companies": []}
        if route.use_previous_results and prev_ids:
            return {"reply": "None of the companies in my previous answer match that. "
                             "Ask again without \"these\" to search all 118.", "companies": []}
        return {"reply": "I couldn't find any companies in the dataset matching that. "
                         "Try a broader sector or a different city.", "companies": []}

    with _stage(emit, rec, "answer", "Writing the answer") as d:
        answer_prompt = prompts.get_prompt("answer")
        rec["prompt_versions"]["answer"] = prompts.version_of(answer_prompt)
        records = _format_records(rows, filters.get("sort_by"))
        request = route.intent
        if filters.get("sort_by"):
            request += f", ranked by {_SORT_LABELS[filters['sort_by']]}"
        lead = ""
        if route.intent == "filter_list":
            # The count sentence is built here, not by the model, which miscounts long lists and blurs the scope.
            # Narrowing a previous answer that already held all 118 isn't worth mentioning.
            scoped = route.use_previous_results and 0 < len(prev_ids) < 118
            sort_by = filters.get("sort_by")
            attribute_filters = {k: v for k, v in filters.items() if k not in ("sort_by", "limit")}
            if method == "vector":  # a list by meaning, not by attributes: a top-N, not a full count
                lead = f"These are the {len(rows)} closest matches by description."
            elif sort_by and filters.get("limit") and not widened:
                lead = _ranked_sentence(rows, sort_by, limited=True, prev=len(prev_ids) if scoped else 0)
            elif sort_by and not (attribute_filters or scoped or widened):
                lead = f"All {len(rows)} unicorns, ranked by {_SORT_LABELS[sort_by]}: {_ranked_items(rows, sort_by)}"
            else:
                lead = (_count_sentence(len(rows), len(prev_ids), scoped=scoped, widened=widened)
                        if attribute_filters or scoped or widened else f"The dataset has {len(rows)} unicorns.")
                if sort_by:
                    lead += " " + _ranked_sentence(rows, sort_by, limited=False)
            request += ", count and ranking stated by the interface"
            emit("token", {"text": lead + " "})
        messages = answer_prompt.compile(history=history, intent=request, count=str(len(rows)),
                                         records=records, question=s.text)
        res = llm.chat(messages, name="answer", prompt=answer_prompt, max_tokens=1500,
                       on_token=lambda t: emit("token", {"text": t}))
        if lead:
            res.text = f"{lead} {res.text.strip()}"
        rec.update(provider=res.provider, model=res.model, used_fallback=rec["used_fallback"] or res.used_fallback,
                   prompt_tokens=rec["prompt_tokens"] + res.prompt_tokens,
                   completion_tokens=rec["completion_tokens"] + res.completion_tokens, outcome="answered")
        rec["context"] = _context(res, {"Instructions": messages[0]["content"], "Conversation memory": history,
                                        "Retrieved companies": records, "Your question": s.text})
        d.update(tokens=res.completion_tokens, provider=res.provider)
    return {"reply": res.text.strip(), "companies": cards}


def handle_turn(session_id: str, message: object, emit: Emit | None = None, user_id: str | None = None) -> dict:
    """Run one turn end to end. Never raises: failures become a logged 'error' outcome."""
    emit = emit or (lambda event, data: None)
    t0 = time.perf_counter()
    lf = get_client()
    rec = {"session_id": session_id, "user_query": str(message)[:2000], "outcome": "error", "retrieved_ids": [],
           "used_fallback": False, "prompt_tokens": 0, "completion_tokens": 0, "stage_ms": {}, "stage_detail": {}}
    with lf.start_as_current_observation(name="chat-turn", as_type="chain", input=rec["user_query"]) as root, \
            propagate_attributes(session_id=session_id, user_id=user_id, trace_name="chat-turn"):
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
                                       "stage_ms", "stage_detail", "prompt_tokens", "completion_tokens", "context",
                                       "prompt_versions", "provider", "model", "used_fallback", "trace_id")},
            "trace_url": lf.get_trace_url(trace_id=rec["trace_id"]) if rec.get("trace_id") else None}
