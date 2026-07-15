"""Auth: local user/password (JWT cookie sessions) and optional OIDC (Authentik etc.)."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from starlette.config import Config

from .config import Settings, get_settings
from .users import UserStore

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=False)


# --- Sessions -----------------------------------------------------------------

def create_session_token(user_id: str, username: str, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.session_ttl_hours)).timestamp()),
        "jti": secrets.token_urlsafe(8),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_session_token(token: str, settings: Settings) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None


# --- Request helpers ----------------------------------------------------------

SESSION_COOKIE = "md_session"


def get_user_store(settings: Settings = Depends(get_settings)) -> UserStore:
    return UserStore(settings.users_file)


def get_current_user_optional(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = None,
    settings: Settings = Depends(get_settings),
    store: UserStore = Depends(get_user_store),
) -> Optional[dict]:
    """Return the user dict if a valid session is present, else None."""
    token = None
    if creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
    elif SESSION_COOKIE in request.cookies:
        token = request.cookies[SESSION_COOKIE]

    if not token:
        return None

    payload = decode_session_token(token, settings)
    if not payload:
        return None

    for u in store.list_users():
        if u["id"] == payload.get("sub"):
            return u
    return None


def get_current_user(user=Depends(get_current_user_optional)) -> dict:
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# --- OIDC client (Authentik-compatible) ---------------------------------------

def get_oauth_client(settings: Settings) -> Optional[OAuth]:
    if not settings.oidc_configured:
        return None
    config = Config(environ={
        "OIDC_ISSUER": settings.oidc_issuer or "",
        "OIDC_CLIENT_ID": settings.oidc_client_id or "",
        "OIDC_CLIENT_SECRET": settings.oidc_client_secret or "",
    })
    oauth = OAuth(config)
    oauth.register(
        name="oidc",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        server_metadata_url=f"{settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": settings.oidc_scopes},
    )
    return oauth
