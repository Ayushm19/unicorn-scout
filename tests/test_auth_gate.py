"""Chatting requires sign-in. These run without a database: the gate rejects before any DB call."""
from fastapi.testclient import TestClient

from app.auth import _safe_next
from app.main import app

client = TestClient(app)


def test_chat_endpoints_reject_anonymous_users():
    for path in ("/api/chat", "/api/chat/stream"):
        r = client.post(path, json={"message": "What does Razorpay do?"})
        assert r.status_code == 401, path
        assert "Sign in" in r.json()["detail"]


def test_anonymous_users_see_no_chats():
    assert client.get("/api/sessions").json() == []
    assert client.get("/api/me").json()["user"] is None


def test_login_next_only_allows_same_site_paths():
    assert _safe_next("/chat?q=List%20fintech%20unicorns") == "/chat?q=List%20fintech%20unicorns"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        assert _safe_next(bad) == "/chat", bad
