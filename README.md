# Unicorn Scout: a chatbot for Indian unicorn startups

A Python GenAI chatbot that answers questions about the 118 Indian unicorns in the
[Tracxn / Kaggle dataset (May 2025)](https://www.kaggle.com/datasets/srisankargiri/list-of-118-unicorn-startups-in-indiamay-2025).
It keeps conversation context, asks a clarifying question when a request is vague, streams its work to a
web UI as it happens, and logs every turn with the prompt versions used.

**Stack (all free tiers):** FastAPI · Groq (`gpt-oss-120b`) with Gemini (`gemini-3.1-flash-lite`, then `gemini-3.5-flash-lite`) as fallback ·
Gemini embeddings (`gemini-embedding-001`, 768-d) · Neon Postgres + pgvector · Langfuse (tracing, prompt
management, scores) · Llama Prompt Guard 2 · Vercel.

## Requirements checklist

| Assignment requirement | Where it lives | How it was verified |
|---|---|---|
| Python chatbot over the static dataset only | `app/`, `data/tracxn.csv`; no web search anywhere | Answers cite only retrieved rows; "Kolkata" returns "none found" |
| Factual questions ("What does Razorpay do?") | `company_lookup` intent → name match → fact card + summary | Sample §1 |
| Filtering questions ("List fintech unicorns") | `filter_list` intent → exact SQL on sector group, city, country, stage, years, investor | Sample §1 (28 fintechs, 13 in Bengaluru, 6 since 2016) |
| Subjective questions via knowledge matching | `recommendation` intent → pgvector similarity on company descriptions → 2-4 picks with reasons | Sample §2, §3 (AI partnership) |
| Conversation context across turns, GenAI stack | Last 3 turns + previous answer's company ids rebuilt from `chat_logs`; LLM router resolves "it/these/those" | "Who are its investors?", "Which of these…", "Which of those…" |
| Clarify instead of guessing | Router `clarify` intent with a question and 2-4 clickable options; the reply is merged with the original ask | Sample §2 |
| Input trimming and normalization | `sanitize.py`: NFKC, control/zero-width chars stripped, whitespace collapsed, 500-char limit | `tests/test_sanitize.py` |
| Malformed or irrelevant input | Empty/garbage → `rejected`; unrelated → `off_topic` | Sample §3 |
| Prompt-injection protection | Regex heuristics → Llama Prompt Guard 2 classifier → prompts that treat input as data; parameterized SQL; escaped HTML | Sample §3 (blocked; disguised attack ignored) |
| Logging: queries, responses, timestamps, session ids, structured | `chat_logs` (one row per turn) + `app_logs` + JSON stdout | Neon console; `/api/sessions/{id}` |
| Metrics: queries, clarifications, errors/fallbacks, latency | `/api/metrics` (also by prompt version), UI Context panel, Langfuse scores | Metrics endpoint; Langfuse dashboard |
| GenAI observability (e.g. Langfuse) | Trace per turn: guardrail → router → retriever → answer, sessions, linked prompt versions, `outcome` score | Langfuse traces and sessions |
| Clean, modular code | One module per concern (table below) | n/a |
| README: setup, run, architecture, assumptions, limitations | This file | n/a |
| Sample interactions | [SAMPLE_INTERACTIONS.md](SAMPLE_INTERACTIONS.md), captured from a real run | n/a |

## Setup

Requires Python 3.11+ (developed on 3.12).

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows; use `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt pytest
```

Fill in `.env` (never committed):

| Variable | Where to get it |
|---|---|
| `DATABASE_URL` | Neon console → project → Connect → **pooled** connection string |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Langfuse → project → Settings → API Keys |
| `GROQ_API_KEYS` | console.groq.com/keys; comma-separate several keys to rotate on rate limits |
| `GEMINI_API_KEYS` | aistudio.google.com/apikey; comma-separated as above |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google Cloud → Google Auth Platform → Clients → Web application. Authorized redirect URI: `http://localhost:8000/auth/callback` (add `https://<your-domain>/auth/callback` when deployed). Optional: without them the app runs guest-only |
| `SESSION_SECRET` | Any long random string; signs the login cookie. Rotating it signs everyone out |

Load the data and prompts once:

```bash
python -m scripts.ingest                  # clean CSV -> embed -> upsert into Neon (idempotent; also migrates the schema)
python -m scripts.push_prompts "why"      # publish changed prompts to Langfuse as a new "production" version
```

## Run

```bash
uvicorn app.main:app --reload       # landing page at http://localhost:8000, chat at /chat
pytest -q                           # unit tests (no network needed)
```

**Deploy:** `vercel deploy`, then add the same env vars in Vercel → Project → Settings → Environment
Variables. Vercel detects the FastAPI `app` in `app/main.py`, serves `public/` from its CDN, and
`vercel.json` (`cleanUrls`) maps `/chat` to `chat.html`.

### API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat/stream` | Server-sent events: `session`, `stage` (each pipeline step with timing and detail), `companies`, `token` (answer text), `result` |
| `POST` | `/api/chat` | Same turn as one JSON response: reply, outcome, intent, filters, companies, tokens, stage timings, context breakdown, prompt versions, trace URL |
| `GET` | `/api/sessions` | The current user's chats (id, title, last activity, turn count) for the sidebar |
| `GET` | `/api/sessions/{id}` | Every turn of a chat with its company cards, so the UI can rebuild itself after a reload. 404 unless you own it |
| `DELETE` | `/api/sessions/{id}` | Soft-deletes a chat you own (hidden from you, logs kept for analytics) |
| `GET` | `/auth/login`, `/auth/callback` | Google sign-in (authorization-code flow with a CSRF `state`) |
| `POST` | `/auth/logout` | Clears the session cookie |
| `GET` | `/api/me` | The current user (or guest) and whether Google sign-in is configured |
| `GET` | `/api/metrics` | Query counts, clarifications, blocked, off-topic, errors, fallbacks, tokens, avg/p95 latency, and the same split by prompt version |
| `GET` | `/api/health` | Database connectivity check |

Chat endpoints identify the caller by the `scout_session` cookie, so API clients must keep cookies between
requests (e.g. one `httpx.Client`). The first message from a cookie-less client creates a guest account.

### Accounts, chats and reloads

- **Who you are.** A signed, HttpOnly cookie holds only your user id. Everyone starts as a guest (a `users` row
  with no Google id, created on the first message). Signing in with Google upserts your account by Google's
  stable `sub` id and moves that browser's guest chats into it, so nothing typed before signing in is lost. Chats
  are never moved out of a signed-in account.
- **Where chats live.** `chat_sessions` (id, owner, title, timestamps, soft delete) plus one `chat_logs` row per turn.
  Every read and write checks ownership in SQL; someone else's chat id returns 404, never its contents.
- **What happens on a hard reload.** The open chat is in the URL (`/chat?c=<id>`). On load the page asks
  `/api/sessions/<id>` for the turns, then rebuilds the messages, step timelines, tables, fact cards and the Context
  panel (from the stored token counts, stage timings, stage details and context breakdown), and scrolls to the start of
  the latest question. Nothing is kept only in the browser, so the same chat opens identically on another device after
  signing in. Plain `/chat` (e.g. from the home page) always starts a new chat; a bad or foreign `?c=` falls back to a
  new chat with a notice.
- **Instant switching.** After the sidebar loads, the three most recent chats are fetched in the background and cached,
  so opening them renders immediately; a chat's cache entry is dropped when a new turn is added to it.
- **What the model remembers.** Independently of the UI, every turn rebuilds the model's context on the server from
  the last 3 turns of *that chat* in `chat_logs`, plus the companies from its latest answer ("these"). "New chat"
  just starts a new chat id: the model starts with an empty memory, and the old chat stays in the sidebar.
- **Langfuse** receives the opaque user id (never the email), so the Users view groups traces per person.

## The web app

- **Landing page** (`public/index.html`): what the bot does, a replay of a real turn, the pipeline step by step,
  the data-quality fixes, and the stack. The telemetry strip shows live query counts from `/api/metrics`.
- **Chat** (`public/chat.html`), built so the user is never left staring at a spinner:
  - **Live pipeline.** Each stage appears as it runs ("Screening for prompt injection · score 0.0004 · 139 ms"),
    then collapses to "Worked for 2.4 s · 6 steps".
  - **Generative UI.** The answer's shape depends on the intent: a sortable table for lists, fact cards (side by
    side for comparisons) for lookups, candidate chips with the picks highlighted for recommendations, and
    clickable option chips for clarifications. Answer text streams token by token.
  - **Context panel**, modelled on Claude's: how much of the model's context window the last call used, split
    into instructions / company index / conversation memory / retrieved companies / question, plus the
    turn's intent, filters, model, prompt versions, a stage waterfall, the working set that "these" refers to,
    session totals, and a link to the Langfuse trace.
  - **Sidebar** of past chats grouped by Today / Yesterday / Previous 7 and 30 days / Older, two-step delete,
    and a Google sign-in / account box.
  - Follow-up suggestions computed from the results, conversation restore after reload, keyboard shortcuts,
    light mode by default with a dark toggle, mobile layout, and reduced-motion support.

## Architecture

```
Browser (public/chat.html) ── POST /api/chat/stream (SSE) ──► FastAPI (app/main.py)
                                                                 │ worker thread
                                                                 ▼
chatbot.handle_turn(session, message, emit) ── one Langfuse trace per turn, grouped by session
   1. sanitize    normalize, length/malformed checks, injection regexes         (sanitize.py)
   2. guard       Llama Prompt Guard 2 classifier on Groq                       (llm.py)
   3. memory      last 3 turns + previous answer's company ids from chat_logs    (db.py)
   4. route       LLM → JSON: intent, filters, companies, follow-up flag,
                  search query, or clarifying question + options                (prompts.py "router")
   5. retrieve    SQL filters and/or pgvector similarity in one query            (db.py, embeddings.py)
   6. answer      streamed LLM answer grounded only in retrieved records         (prompts.py "answer")
   7. log         chat_logs row, app_logs, Langfuse spans and outcome score
   Each stage emits start/done events (with timings) that the UI renders live.
```

| Module | Responsibility |
|---|---|
| `app/data.py` | CSV loading and cleaning, sector groups, city aliases, money-field validation |
| `app/db.py` | Schema (with idempotent migrations) and every SQL query |
| `app/embeddings.py` | Gemini embedding calls (document and query task types) |
| `app/llm.py` | LLM calls with streaming, key rotation and provider fallback; prompt guard |
| `app/prompts.py` | Prompt definitions, fetched from Langfuse at runtime with a local fallback |
| `app/sanitize.py` | Input normalization and injection heuristics |
| `app/chatbot.py` | Orchestrates a turn and emits stage, company and token events |
| `app/observability.py` | JSON logs to stdout and to Postgres |
| `app/auth.py` | Google sign-in, the session cookie, and the current user (guest or signed in) |
| `app/main.py` | HTTP routes, ownership checks, the SSE relay and the static pages |
| `public/` | Landing page, chat app and shared design tokens (`base.css`) |

### Design decisions

- **Router + retrieval + grounded answer, not "stuff the CSV into the prompt".** Groq's free tier allows
  8K tokens/minute, and 118 full records is roughly 12K tokens. More importantly, routing makes behaviour
  explicit and observable: every turn has an intent, filters and an outcome you can count.
- **Hybrid retrieval.** "Fintech unicorns in Bangalore" is a structured question, so it is answered with exact
  SQL filters (complete and correct lists). "Who could help with last-mile delivery?" is semantic, so it uses
  pgvector similarity over company descriptions. One query can combine both.
- **A derived `sector_group` column.** The dataset has 56 fine-grained sectors and no "FinTech" value. A
  hand-written 56 → 12 mapping (`data.py`) is deterministic and reviewable, unlike asking an LLM to guess
  at query time.
- **Precomputed tallies.** For long lists, the answer prompt gets counts by city, sector, stage and founding
  year. The model kept miscounting ("Bengaluru hosts over half" for 13 of 28) until it was handed the arithmetic.
- **The UI renders the records, the LLM explains them.** Tables and fact cards show the data, so the answer
  prompt (v3+) writes observations instead of re-listing rows. That is shorter, cheaper, and harder to get wrong.
- **Conversation state lives in Postgres.** Serverless functions are stateless, so each turn rebuilds context
  from `chat_logs`: the last 3 exchanges for the LLM, plus the company ids from the latest answer within them,
  so that "which of *these*…" filters exactly that set rather than re-searching.
- **Clarify instead of guess.** The router returns `intent: "clarify"` with a question and clickable options when
  a recommendation has no stated need. It is told not to clarify if earlier turns already supplied the detail,
  and to merge the user's answer with the original request. The company list is in the router prompt, so an
  exact name like "Open" is never treated as ambiguous.
- **Prompt versioning.** Prompts are managed in Langfuse (label `production`) and linked to each generation.
  Every `chat_logs` row stores `prompt_versions` (e.g. `{"router": 4, "answer": 4}`), so a prompt change can be
  compared in SQL (`/api/metrics` → `by_prompt_version`) or in Langfuse. Four versions were shipped while testing,
  each fixing an observed failure (see the commit history). The local copy in `prompts.py` is used if Langfuse is
  unreachable and is recorded as `"fallback"`.
- **Streaming without losing trace context.** The SSE endpoint runs the turn in a worker thread and relays queued
  events, so the Langfuse/OpenTelemetry context stays on one thread instead of hopping between generator steps.
- **Defence in depth for prompt injection.** Regex heuristics (fast, deterministic), then a dedicated classifier,
  then prompts that treat the user message as data and allow answers only from retrieved records. SQL is fully
  parameterized; the UI escapes all HTML before rendering its small markdown subset.
- **Resilience.** The chain is Groq `gpt-oss-120b` → Gemini `gemini-3.1-flash-lite` → Gemini `gemini-3.5-flash-lite`.
  Each provider rotates through its API keys on any API error, including a per-call timeout (12 s for Gemini, whose
  free tier occasionally stalls for 20 s+), then falls back to the next. Measured with Groq fully down, turns completed
  in 3–10 s on the fallback. Fallbacks and rotations are logged and counted. Prompt Guard fails open; if every
  provider is down the user gets a friendly reply and an `error` row, never a stack trace.
- **Unknown URLs.** Page paths that don't exist get a branded 404 page; API and auth routes keep JSON errors (404, or
  422 for malformed ids and bodies). Only files directly inside `public/` are served, so `/.env` or `/app.py` are 404.
- **Embeddings via API, not a local model.** A local ONNX/PyTorch model would bloat the Vercel bundle and slow
  cold starts. Companies are embedded once at ingest, and each request embeds only the query.

### Observability

| Where | What |
|---|---|
| `chat_logs` (Postgres) | One row per turn: session id, timestamp, raw and sanitized query, response, intent, outcome (`answered`, `clarification`, `blocked`, `rejected`, `off_topic`, `greeting`, `error`), filters, retrieved company ids, clarifying options, prompt versions, provider/model, fallback flag, prompt/completion tokens, per-stage timings, context breakdown, latency, Langfuse trace id, error |
| `app_logs` (Postgres) + stdout JSON | Warnings (key rotations, rate limits, fallbacks, invalid router output), errors and per-turn summaries; stdout shows up in Vercel runtime logs |
| Langfuse | Trace per turn (guardrail → router generation → retriever → streamed answer generation), sessions, token usage, time to first token, linked prompt versions, and an `outcome` score |
| `/api/metrics`, the landing telemetry strip, the chat Context panel | Live totals and per-turn detail |

## Data quality notes (handled in `app/data.py`)

- The file is Windows-1252 encoded, not UTF-8, and contains non-breaking spaces.
- The ₹ sign was lost upstream (`?569Cr`) and is restored.
- `location` has trailing spaces. Users' "Bangalore"/"Gurgaon" map to the dataset's "Bengaluru"/"Gurugram".
- Some columns are shifted:
  - `top_investors_1/2` hold sector names in some rows (e.g. Perfios, Krutrim); these are dropped from investors.
  - `total_funding_till_date` or `valuation` hold employee counts for 6 companies (boAt, OneCard, Physics Wallah, Open, FirstCry, Policybazaar). A money field must carry a currency, otherwise it is dropped. LivSpace's `1.33B SGD` is kept.
- Some values are kept as-is because the bot never "corrects" the source. For example, Open's valuation reads `$186M` despite its unicorn status.
- Missing values: `round_led_by` (47 rows), `total_funding_till_date` (31 + 4 dropped), `employee_count` (15).
- 16 companies are headquartered outside India (14 US, 2 Singapore) but are on the Indian unicorn list.

## Assumptions and limitations

- Answers reflect the dataset only (May 2025). No live data, so funding, valuations or status may be outdated.
- "Good fit for collaboration" is matched on company descriptions and sectors only; there is no data on
  partnership programmes, APIs or strategy.
- Free-tier rate limits (Groq: about 8K tokens/min per key) cap throughput at a few turns per minute per key; key
  rotation and the Gemini fallback absorb bursts. A turn on the fallback typically takes 3–10 s.
- Gemini's free embedding quota is 100 texts/minute per key. Ingesting 118 companies rotates to the second key,
  while queries use one embedding each.
- The prompt-guard classifier fails open if it is unavailable; the regex layer and grounded prompts still apply.
- On Gemini's free tier, prompts may be used by Google to improve its products. That is acceptable for this
  public dataset, but not for private user data.
- The context-window breakdown estimates each section's share from its character count, scaled to the provider's
  real prompt-token total.
- The Google OAuth app is in "Testing" mode, so only accounts listed as test users can sign in until it is published.
  Guests can always chat. Before deploying, add the production redirect URI to the OAuth client.
- No per-user rate limiting yet.
- Stopping a streaming answer in the UI closes the stream; the server still finishes and logs the turn.

## With more time

- An evaluation set (questions → expected companies or outcomes) run as a Langfuse dataset experiment on every prompt change.
- Rate limiting per session/IP, and authentication for `/api/metrics`.
- A connection pool (`psycopg_pool`) for higher concurrency per function instance.
- Parse valuation and funding into numbers for "top 5 by valuation"-style questions.
- User feedback (thumbs up/down) sent as Langfuse scores.
