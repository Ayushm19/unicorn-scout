"""Gemini embeddings over REST. Used once at ingest (documents) and per request (the query)."""
import logging

import httpx

from app import config

log = logging.getLogger("app.embeddings")

_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{config.EMBEDDING_MODEL}:batchEmbedContents"
_BATCH = 100  # API max per batch call


def _post(texts: list[str], task_type: str) -> list[list[float]]:
    body = {"requests": [{
        "model": f"models/{config.EMBEDDING_MODEL}",
        "content": {"parts": [{"text": t}]},
        "taskType": task_type,
        "outputDimensionality": config.EMBEDDING_DIM,
    } for t in texts]}
    last_error: Exception | None = None
    for i, key in enumerate(config.GEMINI_API_KEYS):
        try:
            r = httpx.post(_URL, json=body, headers={"x-goog-api-key": key}, timeout=30)
            r.raise_for_status()
            return [e["values"] for e in r.json()["embeddings"]]
        except httpx.HTTPError as e:
            last_error = e
            log.warning("embedding call failed, rotating key", extra={"ctx": {"key_index": i, "error": str(e)[:200]}})
    raise RuntimeError(f"All Gemini keys failed for embeddings: {last_error}")


def embed_documents(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        out += _post(texts[i:i + _BATCH], "RETRIEVAL_DOCUMENT")
    return out


def embed_query(text: str) -> list[float]:
    return _post([text], "RETRIEVAL_QUERY")[0]
