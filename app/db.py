"""Postgres (Neon) access: one shared connection, schema, and all SQL in one place."""
import json
from functools import lru_cache

import psycopg
from psycopg.rows import dict_row

from app import config

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS companies (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    rank INT,
    sector TEXT,
    sector_group TEXT,
    city TEXT,
    country TEXT,
    stage TEXT,
    founded_year INT,
    description TEXT,
    unicorn_info TEXT,
    investors TEXT[],
    valuation TEXT,
    annual_revenue TEXT,
    total_funding TEXT,
    employee_count TEXT,
    latest_funding_round TEXT,
    time_to_unicorn TEXT,
    profile_url TEXT,
    embedding vector({config.EMBEDDING_DIM})
);

-- One row per user turn. Doubles as conversation history and as the analytics log.
CREATE TABLE IF NOT EXISTS chat_logs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id UUID NOT NULL,
    user_query TEXT NOT NULL,
    sanitized_query TEXT,
    response TEXT,
    intent TEXT,
    outcome TEXT NOT NULL,          -- answered | clarification | blocked | rejected | off_topic | greeting | error
    filters JSONB,
    retrieved_ids INT[] NOT NULL DEFAULT '{{}}',
    prompt_versions JSONB,          -- e.g. {{"router": 3, "answer": 2}}
    provider TEXT,
    model TEXT,
    used_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    latency_ms INT,
    trace_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS chat_logs_session_idx ON chat_logs (session_id, created_at);
-- v2: token usage, per-stage timings and the context-window breakdown shown in the UI.
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS prompt_tokens INT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS completion_tokens INT;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS stage_ms JSONB;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS context JSONB;
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS clarifying_options TEXT[];
ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS stage_detail JSONB;   -- what each stage found, for the UI timeline

-- v3: accounts and chat ownership. Guests get a row too (google_sub NULL) so their chats
-- survive reloads; signing in moves a guest's chats to the Google account.
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    google_sub TEXT UNIQUE,
    email TEXT,
    name TEXT,
    picture TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ          -- soft delete: the chat disappears, its logs stay for analytics
);
CREATE INDEX IF NOT EXISTS chat_sessions_user_idx ON chat_sessions (user_id, updated_at DESC) WHERE deleted_at IS NULL;

-- General application logs (warnings, errors, lifecycle events).
CREATE TABLE IF NOT EXISTS app_logs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    context JSONB
);
"""

_conn: psycopg.Connection | None = None


def conn() -> psycopg.Connection:
    # One shared autocommit connection per function instance. Move to psycopg_pool if
    # concurrent traffic per instance grows.
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)
    return _conn


def init_schema() -> None:
    conn().execute(SCHEMA)


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


# --- companies ---------------------------------------------------------------

_COMPANY_COLS = ["name", "rank", "sector", "sector_group", "city", "country", "stage", "founded_year",
                 "description", "unicorn_info", "investors", "valuation", "annual_revenue", "total_funding",
                 "employee_count", "latest_funding_round", "time_to_unicorn", "profile_url"]


def upsert_companies(companies: list[dict], embeddings: list[list[float]]) -> None:
    cols = ", ".join(_COMPANY_COLS)
    params = ", ".join(f"%({c})s" for c in _COMPANY_COLS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _COMPANY_COLS[1:])
    sql = (f"INSERT INTO companies ({cols}, embedding) VALUES ({params}, %(embedding)s::vector) "
           f"ON CONFLICT (name) DO UPDATE SET {updates}, embedding = EXCLUDED.embedding")
    with conn().cursor() as cur:
        cur.executemany(sql, [{**c, "embedding": _vec(e)} for c, e in zip(companies, embeddings)])


_PUBLIC_COLS = "id, " + ", ".join(_COMPANY_COLS)


def find_companies_by_name(names: list[str]) -> list[dict]:
    if not names:
        return []
    patterns = [n.strip() for n in names if n.strip()]
    rows = conn().execute(f"SELECT {_PUBLIC_COLS} FROM companies WHERE lower(name) = ANY(%s) ORDER BY rank DESC",
                          ([p.lower() for p in patterns],)).fetchall()
    # Substring fallback only for names with no exact hit (and 4+ chars), so "CRED" never drags in "InCred".
    found = {r["name"].lower() for r in rows}
    missing = [f"%{p}%" for p in patterns if p.lower() not in found and len(p) >= 4]
    if missing:
        rows += conn().execute(f"SELECT {_PUBLIC_COLS} FROM companies WHERE name ILIKE ANY(%s) ORDER BY rank DESC",
                               (missing,)).fetchall()
    return rows[:5]


def search_companies(filters: dict, query_vec: list[float] | None, scope_ids: list[int] | None,
                     limit: int) -> list[dict]:
    """Structured filters + optional semantic ranking in one query."""
    where, params = [], {"limit": limit}
    as_list = lambda v: [v] if isinstance(v, str) else v  # a filter is one value or a list meaning "any of"
    for col in ("sector_group", "city", "country", "stage"):
        if filters.get(col):
            where.append(f"lower({col}) = ANY(%({col})s)")
            params[col] = [v.lower() for v in as_list(filters[col])]
    for col in ("country", "city"):
        if filters.get(f"exclude_{col}"):
            where.append(f"lower({col}) <> ALL(%(exclude_{col})s)")
            params[f"exclude_{col}"] = [v.lower() for v in filters[f"exclude_{col}"]]
    if filters.get("sector"):
        where.append("sector ILIKE ANY(%(sector)s)")
        params["sector"] = [f"%{s}%" for s in as_list(filters["sector"])]
    if filters.get("founded_from"):
        where.append("founded_year >= %(founded_from)s")
        params["founded_from"] = filters["founded_from"]
    if filters.get("founded_to"):
        where.append("founded_year <= %(founded_to)s")
        params["founded_to"] = filters["founded_to"]
    if filters.get("investor"):
        where.append("EXISTS (SELECT 1 FROM unnest(investors) i WHERE i ILIKE %(investor)s)")
        params["investor"] = f"%{filters['investor']}%"
    if scope_ids is not None:
        where.append("id = ANY(%(scope_ids)s)")
        params["scope_ids"] = scope_ids

    select, order = _PUBLIC_COLS, "rank ASC"
    if query_vec is not None:
        params["qv"] = _vec(query_vec)
        select += ", 1 - (embedding <=> %(qv)s::vector) AS similarity"
        order = "embedding <=> %(qv)s::vector"
    sql = f"SELECT {select} FROM companies"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT %(limit)s"
    return conn().execute(sql, params).fetchall()


@lru_cache(maxsize=1)
def company_names() -> str:
    """All company names, comma-separated, for the router prompt. The dataset is static, so cache it."""
    return ", ".join(r["name"] for r in conn().execute("SELECT name FROM companies ORDER BY name").fetchall())


# --- chat logs / history -----------------------------------------------------

_JSON_COLS = ("filters", "prompt_versions", "stage_ms", "stage_detail", "context")


def insert_chat_log(row: dict) -> None:
    row = {**row, **{k: json.dumps(row[k]) for k in _JSON_COLS if row.get(k) is not None}}
    cols = ", ".join(row)
    params = ", ".join(f"%({k})s" for k in row)
    conn().execute(f"INSERT INTO chat_logs ({cols}) VALUES ({params})", row)


def recent_turns(session_id: str, n: int) -> list[dict]:
    """Last n turns that reached the assistant (blocked/rejected input is excluded from context)."""
    rows = conn().execute(
        """SELECT user_query, sanitized_query, response, outcome, retrieved_ids, created_at FROM chat_logs
           WHERE session_id = %s AND outcome NOT IN ('blocked', 'rejected', 'error')
           ORDER BY created_at DESC LIMIT %s""",
        (session_id, n),
    ).fetchall()
    return list(reversed(rows))


def session_history(session_id: str) -> list[dict]:
    return conn().execute(
        """SELECT user_query, response, outcome, intent, filters, retrieved_ids, prompt_versions, provider, model,
                  used_fallback, latency_ms, prompt_tokens, completion_tokens, stage_ms, stage_detail, context,
                  clarifying_options, trace_id, created_at
           FROM chat_logs WHERE session_id = %s ORDER BY created_at""",
        (session_id,),
    ).fetchall()


def companies_by_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    # Keep the order the answer showed them in, so "the first one" means the first row of that table.
    return conn().execute(f"SELECT {_PUBLIC_COLS} FROM companies WHERE id = ANY(%s) ORDER BY array_position(%s, id)",
                          (ids, ids)).fetchall()


def metrics() -> dict:
    return conn().execute(
        """SELECT count(*) AS total_queries,
                  count(DISTINCT session_id) AS sessions,
                  count(*) FILTER (WHERE outcome = 'answered') AS answered,
                  count(*) FILTER (WHERE outcome = 'clarification') AS clarifications,
                  count(*) FILTER (WHERE outcome IN ('blocked', 'rejected')) AS blocked_or_rejected,
                  count(*) FILTER (WHERE outcome = 'off_topic') AS off_topic,
                  count(*) FILTER (WHERE outcome = 'error') AS errors,
                  count(*) FILTER (WHERE used_fallback) AS llm_fallbacks,
                  coalesce(sum(prompt_tokens + completion_tokens), 0) AS tokens,
                  round(avg(latency_ms)) AS avg_latency_ms,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_latency_ms
           FROM chat_logs"""
    ).fetchone()


def metrics_by_prompt_version() -> list[dict]:
    return conn().execute(
        """SELECT prompt_versions, count(*) AS queries,
                  round(avg(latency_ms)) AS avg_latency_ms,
                  count(*) FILTER (WHERE outcome = 'clarification') AS clarifications,
                  count(*) FILTER (WHERE outcome = 'error') AS errors
           FROM chat_logs GROUP BY prompt_versions ORDER BY queries DESC"""
    ).fetchall()


# --- users and chat sessions -------------------------------------------------

_USER_COLS = "id::text AS id, email, name, picture, google_sub IS NOT NULL AS signed_in"


def get_user(user_id: str) -> dict | None:
    return conn().execute(f"UPDATE users SET last_seen_at = now() WHERE id = %s RETURNING {_USER_COLS}",
                          (user_id,)).fetchone()


def upsert_google_user(sub: str, email: str, name: str | None, picture: str | None) -> dict:
    return conn().execute(
        f"""INSERT INTO users (google_sub, email, name, picture) VALUES (%s, %s, %s, %s)
            ON CONFLICT (google_sub) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name,
                picture = EXCLUDED.picture, last_seen_at = now()
            RETURNING {_USER_COLS}""",
        (sub, email, name, picture),
    ).fetchone()


def adopt_guest_chats(guest_id: str, user_id: str) -> int:
    """Move a guest's chats to the account they just signed in with. Never moves another account's chats."""
    return conn().execute(
        """UPDATE chat_sessions SET user_id = %s
           WHERE user_id = %s AND EXISTS (SELECT 1 FROM users WHERE id = %s AND google_sub IS NULL)""",
        (user_id, guest_id, guest_id),
    ).rowcount


def claim_session(session_id: str, user_id: str, title: str) -> bool:
    """Create the chat for this user, or touch it if they already own it. False means someone else owns it."""
    row = conn().execute(
        """INSERT INTO chat_sessions (id, user_id, title) VALUES (%s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET updated_at = now()
               WHERE chat_sessions.user_id = EXCLUDED.user_id AND chat_sessions.deleted_at IS NULL
           RETURNING id""",
        (session_id, user_id, title),
    ).fetchone()
    return row is not None


def owns_session(session_id: str, user_id: str) -> bool:
    return conn().execute(
        "SELECT 1 FROM chat_sessions WHERE id = %s AND user_id = %s AND deleted_at IS NULL", (session_id, user_id),
    ).fetchone() is not None


def list_sessions(user_id: str) -> list[dict]:
    return conn().execute(
        """SELECT s.id::text AS id, s.title, s.updated_at,
                  (SELECT count(*) FROM chat_logs l WHERE l.session_id = s.id) AS turns
           FROM chat_sessions s WHERE s.user_id = %s AND s.deleted_at IS NULL
           ORDER BY s.updated_at DESC LIMIT 200""",
        (user_id,),
    ).fetchall()


def delete_session(session_id: str, user_id: str) -> bool:
    return conn().execute(
        "UPDATE chat_sessions SET deleted_at = now() WHERE id = %s AND user_id = %s AND deleted_at IS NULL",
        (session_id, user_id),
    ).rowcount == 1


# --- app logs ----------------------------------------------------------------

def insert_app_log(level: str, logger: str, message: str, context: dict | None) -> None:
    conn().execute(
        "INSERT INTO app_logs (level, logger, message, context) VALUES (%s, %s, %s, %s)",
        (level, logger, message, json.dumps(context, default=str) if context else None),
    )
