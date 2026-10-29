"""Structured logging: JSON lines to stdout (Vercel runtime logs) and a copy in Postgres `app_logs`.

Usage: log.info("message", extra={"ctx": {...}}). Per-turn chat records go to `chat_logs` (see db.py);
LLM traces, prompt versions, and scores go to Langfuse.
"""
import json
import logging
import sys
from datetime import datetime, timezone

from app import db


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **getattr(record, "ctx", {}),
            **({"exc": self.formatException(record.exc_info)} if record.exc_info else {}),
        }, default=str)


class DbHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            ctx = dict(getattr(record, "ctx", {}))
            if record.exc_info:
                ctx["exc"] = logging.Formatter().formatException(record.exc_info)
            db.insert_app_log(record.levelname, record.name, record.getMessage(), ctx or None)
        except Exception:
            self.handleError(record)  # never let logging break a request


def setup_logging(to_db: bool = True) -> None:
    logger = logging.getLogger("app")
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonFormatter())
    logger.addHandler(stdout)
    if to_db:
        logger.addHandler(DbHandler(level=logging.INFO))
