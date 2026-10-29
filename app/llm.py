"""LLM calls with key rotation and provider fallback, traced by Langfuse's OpenAI drop-in client."""
import logging
from dataclasses import dataclass
from functools import lru_cache

import openai
from langfuse.openai import OpenAI

from app import config

log = logging.getLogger("app.llm")


class LLMUnavailable(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    used_fallback: bool  # True when a provider other than the primary answered


@lru_cache(maxsize=16)
def _client(base_url: str, key: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=key, max_retries=0, timeout=30)


def chat(messages: list[dict], *, name: str, json_mode: bool = False, prompt=None,
         max_tokens: int = 1000, temperature: float = 0.2) -> LLMResult:
    """Try each provider in order, rotating through its keys on any API error (429, 5xx, auth...)."""
    for p_idx, p in enumerate(config.LLM_PROVIDERS):
        for k_idx, key in enumerate(p["keys"]):
            kwargs = {"model": p["model"], "messages": messages, "max_tokens": max_tokens,
                      "temperature": temperature, "name": name, **p["extra"]}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if prompt is not None and not getattr(prompt, "is_fallback", False):
                kwargs["langfuse_prompt"] = prompt  # links this generation to the prompt version in Langfuse
            try:
                resp = _client(p["base_url"], key).chat.completions.create(**kwargs)
                return LLMResult(resp.choices[0].message.content or "", p["name"], p["model"], p_idx > 0)
            except openai.APIError as e:
                log.warning("llm call failed, trying next key/provider", extra={"ctx": {
                    "call": name, "provider": p["name"], "key_index": k_idx,
                    "status": getattr(e, "status_code", None), "error": str(e)[:300]}})
    raise LLMUnavailable(f"All LLM providers failed for '{name}'")


def prompt_guard_score(text: str) -> float | None:
    """Probability (0-1) that text is a prompt attack, from Llama Prompt Guard 2 on Groq.

    Returns None when the classifier is unavailable; callers fail open because the regex
    layer and the grounded answer prompt still apply.
    """
    for key in config.GROQ_API_KEYS:
        try:
            resp = _client("https://api.groq.com/openai/v1", key).chat.completions.create(
                model=config.PROMPT_GUARD_MODEL, messages=[{"role": "user", "content": text}], name="prompt-guard")
            return float(resp.choices[0].message.content.strip())
        except (openai.APIError, ValueError) as e:
            log.warning("prompt guard failed", extra={"ctx": {"error": str(e)[:300]}})
    return None
