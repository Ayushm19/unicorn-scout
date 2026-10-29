"""Google sign-in (OAuth 2.0 authorization-code flow) and the current user.

Identity lives in a signed, HttpOnly session cookie holding only the user id. Visitors chat as a
guest (a `users` row with no Google id, created on their first message); signing in moves the
guest's chats to their Google account so history follows them across devices.
"""
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import config, db

log = logging.getLogger("app.auth")
router = APIRouter()

_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"
_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


def current_user(request: Request, create: bool = False) -> dict | None:
    """The signed-in user or guest behind this request; with create=True, a new guest if there is none."""
    uid = request.session.get("uid")
    user = db.get_user(uid) if uid else None
    if user is None and create:
        user = db.create_guest()
        request.session["uid"] = user["id"]
    return user


def _redirect_uri(request: Request) -> str:
    uri = str(request.url_for("auth_callback"))
    return uri.replace("http://", "https://", 1) if config.ON_VERCEL else uri  # TLS ends at Vercel's edge


@router.get("/auth/login")
def login(request: Request) -> RedirectResponse:
    if not config.GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in is not configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state  # CSRF protection for the callback
    params = {"client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": _redirect_uri(request), "response_type": "code",
              "scope": "openid email profile", "state": state, "prompt": "select_account"}
    return RedirectResponse(f"{_AUTHORIZE}?{urlencode(params)}")


@router.get("/auth/callback", name="auth_callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None) -> RedirectResponse:
    expected = request.session.pop("oauth_state", None)
    if error or not code or not state or not expected or not secrets.compare_digest(state, expected):
        log.warning("google sign-in rejected", extra={"ctx": {"error": error, "state_ok": state == expected}})
        return RedirectResponse("/chat?auth=failed")
    try:
        tokens = httpx.post(_TOKEN, timeout=15, data={
            "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _redirect_uri(request), "grant_type": "authorization_code"})
        tokens.raise_for_status()
        # The access token came straight from Google over TLS, so its userinfo can be trusted as-is.
        info = httpx.get(_USERINFO, timeout=15,
                         headers={"Authorization": f"Bearer {tokens.json()['access_token']}"}).json()
    except (httpx.HTTPError, KeyError, ValueError):
        log.exception("google token exchange failed")
        return RedirectResponse("/chat?auth=failed")
    if not info.get("sub") or not info.get("email_verified"):
        return RedirectResponse("/chat?auth=failed")

    guest_id = request.session.get("uid")
    user = db.upsert_google_user(info["sub"], info["email"], info.get("name"), info.get("picture"))
    moved = db.adopt_guest_chats(guest_id, user["id"]) if guest_id and guest_id != user["id"] else 0
    request.session.clear()  # fresh session on privilege change
    request.session["uid"] = user["id"]
    log.info("signed in", extra={"ctx": {"user_id": user["id"], "chats_adopted": moved}})
    return RedirectResponse("/chat")


@router.post("/auth/logout")
def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}


@router.get("/api/me")
def me(request: Request) -> dict:
    user = current_user(request)
    return {"user": user and {k: user[k] for k in ("name", "email", "picture", "signed_in")},
            "google_enabled": bool(config.GOOGLE_CLIENT_ID)}
