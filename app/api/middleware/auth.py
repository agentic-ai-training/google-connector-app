import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from jose import JWTError, jwt
from oauthlib.oauth2 import OAuth2Error
from pydantic import BaseModel

from app.config.settings import get_public_url, get_settings
from app.db.connection import get_pool
from app.db.google_clients import SCOPES
from app.db.oauth_credentials import (
    delete_google_credentials,
    save_google_credentials,
)

router = APIRouter()
OAUTH_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email", *SCOPES]


class TokenRequest(BaseModel):
    email: str


def create_token(email: str):
    settings = get_settings()
    admins = {item.strip().lower() for item in settings.admin_emails.split(",")}
    return jwt.encode(
        {
            "sub": email,
            "admin": email.lower() in admins,
            "exp": datetime.now(timezone.utc) + timedelta(days=1),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _client_config():
    settings = get_settings()
    if settings.google_oauth_client_json:
        config = json.loads(settings.google_oauth_client_json)
    elif os.path.exists(settings.google_oauth_client_path):
        with open(settings.google_oauth_client_path, encoding="utf-8") as handle:
            config = json.load(handle)
    else:
        raise HTTPException(503, "Google OAuth is not configured")
    if "web" not in config:
        raise HTTPException(503, "A Google Web OAuth client is required")
    return config


def _callback_url():
    configured = get_settings().google_oauth_redirect_uri
    if configured:
        return configured
    public_url = get_public_url()
    if public_url:
        return f"{public_url}/auth/google/callback"
    return "http://localhost:8000/auth/google/callback"


def _safe_return_to(value: str | None):
    configured = get_settings().frontend_url.rstrip("/")
    candidate = (value or configured).rstrip("/")
    if candidate != configured:
        raise HTTPException(400, "Invalid OAuth return URL")
    return candidate


@router.get("/auth/google/login")
async def google_login(return_to: str | None = Query(default=None)):
    settings = get_settings()
    destination = _safe_return_to(return_to)
    state = jwt.encode(
        {
            "purpose": "google-oauth",
            "return_to": destination,
            "nonce": secrets.token_urlsafe(24),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    flow = Flow.from_client_config(
        _client_config(), scopes=OAUTH_SCOPES, redirect_uri=_callback_url()
    )
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
    )
    return RedirectResponse(authorization_url, status_code=302)


@router.get("/auth/google/callback")
async def google_callback(code: str, state: str):
    settings = get_settings()
    return_to = settings.frontend_url.rstrip("/")
    try:
        state_data = jwt.decode(
            state, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        if state_data.get("purpose") != "google-oauth":
            raise JWTError("invalid purpose")
        return_to = _safe_return_to(state_data.get("return_to"))
        config = _client_config()
        flow = Flow.from_client_config(
            config, scopes=OAUTH_SCOPES, redirect_uri=_callback_url()
        )
        flow.fetch_token(code=code)
        identity = id_token.verify_oauth2_token(
            flow.credentials.id_token,
            GoogleRequest(),
            audience=config["web"]["client_id"],
        )
        email = identity.get("email", "").lower()
        if not email or not identity.get("email_verified"):
            raise ValueError("Google email is not verified")
        await save_google_credentials(await get_pool(), email, flow.credentials)
        token = create_token(email)
        fragment = urlencode({"access_token": token})
        return RedirectResponse(f"{return_to}/#{fragment}", status_code=302)
    except OAuth2Error:
        fragment = urlencode({
            "oauth_error": "Authorization expired or was already used. Please sign in again."
        })
        return RedirectResponse(f"{return_to}/#{fragment}", status_code=302)
    except (JWTError, ValueError, KeyError) as exc:
        raise HTTPException(400, f"Google OAuth failed: {exc}") from exc


@router.post("/auth/token")
async def token(req: TokenRequest):
    if not get_settings().allow_dev_auth:
        raise HTTPException(404, "Passwordless development auth is disabled")
    return {"access_token": create_token(req.email), "token_type": "bearer"}


@router.get("/auth/me")
async def me(request: Request):
    pool = await get_pool()
    async with pool.acquire() as conn:
        connected = bool(await conn.fetchval(
            "SELECT 1 FROM google_oauth_credentials WHERE user_id=$1",
            request.state.user_id,
        ))
    return {"email": request.state.user_id, "google_connected": connected}


@router.delete("/auth/google")
async def disconnect_google(request: Request):
    await delete_google_credentials(await get_pool(), request.state.user_id)
    return {"status": "disconnected"}


async def auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    protected = request.url.path.startswith(
        ("/chat", "/feedback", "/history", "/admin", "/auth/me")
    ) or (request.url.path == "/auth/google" and request.method == "DELETE")
    if not protected:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    try:
        raw = header.split(" ", 1)[1] if header.lower().startswith("bearer ") else ""
        payload = jwt.decode(
            raw,
            get_settings().jwt_secret_key,
            algorithms=[get_settings().jwt_algorithm],
        )
        if request.url.path.startswith("/admin") and not payload.get("admin"):
            raise HTTPException(403, "Admin access required")
        request.state.user_id = payload["sub"]
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except (JWTError, KeyError, IndexError):
        return JSONResponse(
            {"detail": "Invalid or missing bearer token"}, status_code=401
        )
    return await call_next(request)
