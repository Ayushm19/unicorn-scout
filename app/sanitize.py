"""Input sanitization: normalize, reject malformed input, and flag prompt-injection attempts.

Two layers: cheap regex heuristics here (always on), then Meta's Llama Prompt Guard 2 classifier
(see llm.prompt_guard_score) for attacks the patterns miss.
"""
import re
import unicodedata
from dataclasses import dataclass

from app import config

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿]")

INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your|system)\b.{0,20}"
    r"\b(instructions?|prompts?|rules?|context|guidelines?)\b",
    r"\b(reveal|show|print|repeat|leak|output)\b.{0,30}\b(system|hidden|initial|original)\s+(prompt|instructions?|message)",
    r"\byou\s+are\s+now\b|\bact\s+as\s+(an?\s+)?(unrestricted|unfiltered|jailbroken|dan)\b",
    r"\b(developer|god|jailbreak|dan)\s+mode\b",
    r"\bdo\s+anything\s+now\b",
    r"<\s*/?\s*(system|assistant|instructions?)\s*>|\[/?(INST|SYS)\]|<\|im_(start|end)\|>",
    r"<\s*script\b|javascript:",
)]


@dataclass
class Sanitized:
    ok: bool
    text: str
    reason: str | None = None  # empty | too_long | malformed | injection


def sanitize(raw: object) -> Sanitized:
    if not isinstance(raw, str):
        return Sanitized(False, "", "malformed")
    text = unicodedata.normalize("NFKC", raw)
    text = _CONTROL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return Sanitized(False, "", "empty")
    if len(text) > config.MAX_QUERY_CHARS:
        return Sanitized(False, text[:config.MAX_QUERY_CHARS], "too_long")
    if not re.search(r"\w", text) or re.fullmatch(r"(.)\1{9,}", text):
        return Sanitized(False, text, "malformed")
    if any(p.search(text) for p in INJECTION_PATTERNS):
        return Sanitized(False, text, "injection")
    return Sanitized(True, text)


REJECTION_MESSAGES = {
    "empty": "Please type a question about Indian unicorn startups.",
    "too_long": f"That message is too long. Please keep questions under {config.MAX_QUERY_CHARS} characters.",
    "malformed": "I couldn't understand that. Try something like \"What does Razorpay do?\"",
    "injection": "I can only help with questions about Indian unicorn startups in my dataset.",
}
