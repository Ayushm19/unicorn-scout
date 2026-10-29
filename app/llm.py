"""LLM calls with key rotation and provider fallback, traced by Langfuse's OpenAI drop-in client."""
import logging
from collections.abc import Callable
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
    prompt_tokens: int = 0
    completion_tokens: int = 0
    context_window: int = 0


@lru_cache(maxsize=16)
def _client(base_url: str, key: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=key, max_retries=0, timeout=30)


def chat(messages: list[dict], *, name: str, json_mode: bool = False, prompt=None, max_tokens: int = 1000,
         temperature: float = 0.2, on_token: Callable[[str], None] | None = None) -> LLMResult:
    """Try each provider in order, rotating through its keys on any API error (429, 5xx, auth...).

    With on_token, the response is streamed and each text delta is passed to it. Failover only
    happens before the first token; a provider dying mid-stream raises.
    """
    for p_idx, p in enumerate(config.LLM_PROVIDERS):
        for k_idx, key in enumerate(p["keys"]):
            kwargs = {"model": p["model"], "messages": messages, "max_tokens": max_tokens,
                      "temperature": temperature, "name": name, **p["extra"]}
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            if prompt is not None and not getattr(prompt, "is_fallback", False):
                kwargs["langfuse_prompt"] = prompt  # links this generation to the prompt version in Langfuse
            if on_token:
                kwargs.update(stream=True, stream_options={"include_usage": True})
            try:
                resp = _client(p["base_url"], key).chat.completions.create(**kwargs)
                result = LLMResult("", p["name"], p["model"], p_idx > 0, context_window=p["context_window"])
                if not on_token:
                    result.text = resp.choices[0].message.content or ""
                    usage = resp.usage
                else:
                    parts, usage = [], None
                    for chunk in resp:
                        if chunk.choices and chunk.choices[0].delta.content:
                            parts.append(chunk.choices[0].delta.content)
                            on_token(parts[-1])
                        usage = getattr(chunk, "usage", None) or usage
                    result.text = "".join(parts)
                if usage:
                    result.prompt_tokens, result.completion_tokens = usage.prompt_tokens, usage.completion_tokens
                return result
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
