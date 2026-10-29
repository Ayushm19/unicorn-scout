"""Environment-driven settings (loaded from .env in app/__init__.py). Secrets never live in code."""
import os


def _keys(name: str) -> list[str]:
    return [k.strip() for k in os.getenv(name, "").split(",") if k.strip()]


DATABASE_URL = os.getenv("DATABASE_URL", "")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
ON_VERCEL = bool(os.getenv("VERCEL"))  # set by Vercel; enables Secure cookies and https redirect URIs
GROQ_API_KEYS = _keys("GROQ_API_KEYS")
GEMINI_API_KEYS = _keys("GEMINI_API_KEYS")

# Providers are tried in order; each provider rotates through its keys before
# falling back to the next provider. Both expose OpenAI-compatible endpoints.
LLM_PROVIDERS = [
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "keys": GROQ_API_KEYS,
        "model": "openai/gpt-oss-120b",
        "context_window": 131_072,
        "extra": {"reasoning_effort": "low"},
    },
    {
        "name": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "keys": GEMINI_API_KEYS,
        "model": "gemini-3.5-flash-lite",
        "context_window": 1_048_576,
        "extra": {"reasoning_effort": "low"},  # default thinking made free-tier calls ~3x slower
    },
]

PROMPT_GUARD_MODEL = "meta-llama/llama-prompt-guard-2-86m"
PROMPT_GUARD_THRESHOLD = 0.8

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 768

MAX_QUERY_CHARS = 500
HISTORY_TURNS = 3  # prior user/bot exchanges sent to the LLM; Groq free tier is 8K tokens/min
