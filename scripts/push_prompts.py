"""Push the prompts in app/prompts.py to Langfuse as a new version labelled "production".

Langfuse only creates a new version when called, so run this after editing a prompt:
    python -m scripts.push_prompts "why this change"
"""
import sys

from langfuse import get_client

from app.prompts import PROMPTS


def main(message: str) -> None:
    lf = get_client()
    for name, messages in PROMPTS.items():
        current = lf.get_prompt(name, type="chat", fallback=messages, cache_ttl_seconds=0)
        same = [(m["role"], m["content"]) for m in current.prompt] == [(m["role"], m["content"]) for m in messages]
        if not getattr(current, "is_fallback", False) and same:
            print(f"{name}: unchanged (v{current.version})")
            continue
        p = lf.create_prompt(name=name, type="chat", prompt=messages, labels=["production"], commit_message=message)
        print(f"{name}: created v{p.version}")
    lf.flush()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "update prompts")
