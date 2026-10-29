# Unicorn Scout: a chatbot for Indian unicorn startups

A Python GenAI chatbot that answers questions about the 118 Indian unicorns in the
[Tracxn / Kaggle dataset (May 2025)](https://www.kaggle.com/datasets/srisankargiri/list-of-118-unicorn-startups-in-indiamay-2025).
It keeps conversation context, asks a clarifying question when a request is vague, and logs every
turn with the prompt versions used.

**Stack (all free tiers):** FastAPI · Groq (`gpt-oss-120b`) with Gemini (`gemini-3.5-flash-lite`) as fallback ·
Gemini embeddings (`gemini-embedding-001`, 768-d) · Neon Postgres + pgvector · Langfuse (tracing, prompt
management, scores) · Llama Prompt Guard 2 · Vercel.

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

Load the data and prompts once:

```bash
python -m scripts.ingest            # clean CSV -> embed -> upsert into Neon (idempotent)
python -m scripts.push_prompts "v1" # create the prompts in Langfuse, labelled "production"
```

## Run

```bash
uvicorn app.main:app --reload       # http://localhost:8000
pytest -q                           # unit tests (no network needed)
```

**Deploy:** `vercel deploy`, then add the same env vars in Vercel → Project → Settings → Environment
Variables. Vercel detects the FastAPI `app` in `app/main.py` and serves `public/` from its CDN.

### API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat` | `{"message": "...", "session_id": "<uuid or null>"}` → reply, outcome, companies, latency, trace id |
| `GET` | `/api/sessions/{id}` | Conversation history for a session |
| `GET` | `/api/metrics` | Query counts, clarifications, blocked, errors, fallbacks, latency, and the same split by prompt version |
| `GET` | `/api/health` | Database connectivity check |

## Architecture

```
Browser (public/index.html)
   │  POST /api/chat
   ▼
FastAPI (app/main.py)
   ▼
chatbot.handle_turn ── one Langfuse trace per turn, grouped by session_id
   1. sanitize        normalize, length/malformed checks, injection regexes      (sanitize.py)
   2. prompt guard    Llama Prompt Guard 2 classifier on Groq                    (llm.py)
   3. load context    last 3 turns + previous answer's company ids from chat_logs (db.py)
   4. route (LLM)     JSON: intent, filters, company names, follow-up flag,
                      search query, or a clarifying question                     (prompts.py "router")
   5. retrieve        SQL filters and/or pgvector similarity in one query        (db.py, embeddings.py)
   6. answer (LLM)    grounded only in retrieved records                         (prompts.py "answer")
   7. log             chat_logs row (+ app_logs, Langfuse spans and an outcome score)
```

| Module | Responsibility |
|---|---|
| `app/data.py` | CSV loading and cleaning, sector groups, city aliases |
| `app/db.py` | Schema and every SQL query (companies, chat_logs, app_logs, metrics) |
| `app/embeddings.py` | Gemini embedding calls (document and query task types) |
| `app/llm.py` | LLM calls with key rotation and provider fallback; prompt guard |
| `app/prompts.py` | Prompt definitions, fetched from Langfuse at runtime with a local fallback |
| `app/sanitize.py` | Input normalization and injection heuristics |
| `app/chatbot.py` | Orchestrates a turn |
| `app/observability.py` | JSON logs to stdout and to Postgres |
| `app/main.py` | HTTP routes |

### Design decisions

- **Router + retrieval + grounded answer, not "stuff the CSV into the prompt".** Groq's free tier allows
  8K tokens/minute, and 118 full records is roughly 12K tokens. More importantly, routing makes behaviour
  explicit and observable: every turn has an intent, filters and an outcome you can count.
- **Hybrid retrieval.** "List fintech unicorns in Bangalore" is a structured question, so it is answered with exact
  SQL filters (complete and correct lists). "Who could help with last-mile delivery?" is semantic, so it uses pgvector
  similarity over company descriptions. One query can combine both.
- **A derived `sector_group` column.** The dataset has 56 fine-grained sectors and no "FinTech" value. A
  hand-written 56 → 12 mapping (`data.py`) is deterministic and reviewable, unlike asking an LLM to guess
  at query time.
- **Conversation state lives in Postgres.** Serverless functions are stateless, so each turn rebuilds context
  from `chat_logs`: the last 3 exchanges for the LLM, plus the company ids from the previous answer so that
  "which of *these*…" filters exactly that set rather than re-searching.
- **Clarify instead of guess.** The router returns `intent: "clarify"` with a question grounded in the dataset's
  domains when a recommendation has no stated need ("best company to collaborate with?"). It is told not to
  clarify if earlier turns already supplied the detail, and to merge the user's answer with the original request.
- **Prompt versioning.** Prompts are managed in Langfuse (label `production`) and linked to each generation.
  Every `chat_logs` row stores `prompt_versions` (e.g. `{"router": 2, "answer": 1}`), so a prompt change can
  be compared in SQL (`/api/metrics` → `by_prompt_version`) or in Langfuse. The local copy in `prompts.py` is
  used if Langfuse is unreachable, and is recorded as `"fallback"`.
- **Defence in depth for prompt injection.** Regex heuristics (fast, deterministic), then a dedicated
  classifier, then prompts that treat the user message as data and only allow answers from retrieved records.
  SQL is fully parameterized; the UI escapes all HTML before rendering markdown.
- **Resilience.** Each provider rotates through its API keys on any API error, then falls back to the next
  provider. Fallbacks are logged and counted. Any unexpected failure becomes a friendly reply plus an `error` row,
  so the API never returns a stack trace.
- **Embeddings via API, not a local model.** A local ONNX/PyTorch model would bloat the Vercel bundle and slow
  cold starts. Companies are embedded once at ingest, and each request embeds only the query.

### Observability

| Where | What |
|---|---|
| `chat_logs` (Postgres) | One row per turn: session id, timestamp, raw and sanitized query, response, intent, outcome (`answered`, `clarification`, `blocked`, `rejected`, `off_topic`, `greeting`, `error`), filters, retrieved company ids, prompt versions, provider/model, fallback flag, latency, Langfuse trace id, error |
| `app_logs` (Postgres) + stdout JSON | Warnings, errors and per-turn summaries; stdout shows up in Vercel runtime logs |
| Langfuse | Trace per turn (guardrail → router generation → retriever → answer generation), grouped into sessions, with token usage, latency, linked prompt versions, and an `outcome` score |
| `/api/metrics` + Metrics button in the UI | Totals for queries, sessions, clarifications, blocked, off-topic, errors, LLM fallbacks, average and p95 latency |

## Data quality notes (handled in `app/data.py`)

- The file is Windows-1252 encoded, not UTF-8, and contains non-breaking spaces.
- The ₹ sign was lost upstream (`?569Cr`) and is restored.
- `location` has trailing spaces. Users' "Bangalore"/"Gurgaon" map to the dataset's "Bengaluru"/"Gurugram".
- `top_investors_1/2` contain sector names in some rows (shifted columns, e.g. Perfios, Krutrim); these are dropped from investors.
  Other columns shift too: Open's `total_funding_till_date` holds an employee count, and its valuation reads `$186M`. These values are shown as-is because the bot never "corrects" the source.
- Missing values: `round_led_by` (47 rows), `total_funding_till_date` (31), `employee_count` (15).
- 16 companies are headquartered outside India (14 US, 2 Singapore) but are on the Indian unicorn list.

## Assumptions and limitations

- Answers reflect the dataset only (May 2025). No live data, so funding, valuations or status may be outdated.
- "Good fit for collaboration" is matched on company descriptions and sectors only; there is no data on
  partnership programmes, APIs or strategy.
- Free-tier rate limits (Groq: about 8K tokens/min per key) cap throughput at a few turns per minute per key; the
  Gemini fallback and key rotation absorb bursts. The fallback is slower (about 5–10s per call on Gemini's free tier).
- Gemini's free embedding quota is 100 texts/minute per key. Ingesting 118 companies rotates to the second key,
  while queries use one embedding each.
- The prompt-guard classifier fails open if it is unavailable; the regex layer and grounded prompts still apply.
- On Gemini's free tier, prompts may be used by Google to improve its products. That is acceptable for this
  public dataset, but not for private user data.
- No authentication or per-user rate limiting. Session ids are random UUIDs held in the browser.

## With more time

- An evaluation set (questions → expected companies or outcomes) run as a Langfuse dataset experiment on every prompt change.
- Rate limiting per session/IP, and authentication for `/api/metrics`.
- Streaming responses, and a connection pool (`psycopg_pool`) for higher concurrency.
- Parse valuation and funding into numbers for "top 5 by valuation"-style questions.
- User feedback (thumbs up/down) sent as Langfuse scores.

## Sample interaction

See [SAMPLE_INTERACTIONS.md](SAMPLE_INTERACTIONS.md) (captured from a real run).
