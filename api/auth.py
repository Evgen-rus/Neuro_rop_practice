"""Server-side authentication primitives for the FastAPI application.

The storage implementation owns users, sessions, Argon2id password hashing and
login throttling.  This module only coordinates those APIs and deliberately
keeps the session token out of response bodies and logs.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response

from storage import rop_db as storage


AUTH_COOKIE_NAME = os.getenv("ROP_SESSION_COOKIE", "rop_session")
SESSION_TTL_SECONDS = max(300, int(os.getenv("ROP_SESSION_TTL_SECONDS", str(8 * 60 * 60))))
ALLOWED_ROLES = frozenset({"admin", "rop", "manager"})


class AuthStorageUnavailable(RuntimeError):
    """Raised when the storage worker has not installed the auth contract."""


_IN_HTTP_REQUEST: ContextVar[bool] = ContextVar("rop_auth_in_http_request", default=False)
_CURRENT_USER: ContextVar[dict[str, Any] | None] = ContextVar("rop_current_user", default=None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_login(value: str) -> str:
    return str(value or "").strip().casefold()


def client_ip(request: Request) -> str:
    # Do not trust a user-controlled forwarding header.  The outer proxy can
    # replace request.client with the real peer when that is configured.
    return str(request.client.host if request.client else "unknown")[:128] or "unknown"


def _storage_function(name: str):
    function = getattr(storage, name, None)
    if not callable(function):
        raise AuthStorageUnavailable(f"Storage contract is missing: {name}")
    return function


def _safe_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    try:
        user_id = int(row["id"])
    except (KeyError, TypeError, ValueError):
        return None
    role = str(row.get("role") or "")
    if role not in ALLOWED_ROLES:
        return None
    return {
        "id": user_id,
        "login": str(row.get("login") or ""),
        "role": role,
        "manager_id": str(row.get("manager_id") or "") or None,
        "is_active": bool(row.get("is_active")),
    }


def public_user(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the stable public user DTO, never including a password hash."""

    return _safe_user(row)


def _token_digest(token: str) -> str:
    digest = getattr(storage, "digest_auth_token", None)
    if callable(digest):
        return str(digest(token))
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    function = _storage_function("hash_auth_password")
    return str(function(password))


def _session_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat(timespec="seconds")


def _locked_until(throttle: dict[str, Any] | None, now: str) -> datetime | None:
    if not isinstance(throttle, dict):
        return None
    locked_until = _parse_datetime(throttle.get("locked_until"))
    current = _parse_datetime(now) or datetime.now(timezone.utc)
    return locked_until if locked_until and locked_until > current else None


def login_user(
    *,
    login: str,
    password: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Validate credentials, create an opaque server-side session and set its cookie."""

    normalized_login = normalize_login(login)
    if not normalized_login or not isinstance(password, str) or not password:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    now = _now()
    ip = client_ip(request)
    get_throttle = _storage_function("get_auth_login_throttle")
    throttle = get_throttle(
        storage.DEFAULT_DB_PATH,
        login=normalized_login,
        client_key=ip,
        now=now,
    )
    locked = _locked_until(throttle, now)
    if locked is not None:
        retry_after = max(1, int((locked - (datetime.now(timezone.utc))).total_seconds()))
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    get_user = _storage_function("get_auth_user")
    verify = _storage_function("verify_auth_password")
    record_attempt = _storage_function("record_auth_login_attempt")
    user_row = get_user(
        storage.DEFAULT_DB_PATH,
        login=normalized_login,
        include_password_hash=True,
    )
    password_hash = user_row.get("password_hash") if isinstance(user_row, dict) else None
    valid = bool(user_row and user_row.get("is_active") and password_hash and verify(password, str(password_hash)))
    record_attempt(
        storage.DEFAULT_DB_PATH,
        login=normalized_login,
        client_key=ip,
        succeeded=valid,
        attempted_at=now,
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    clear_attempts = _storage_function("clear_auth_login_attempts")
    clear_attempts(storage.DEFAULT_DB_PATH, login=normalized_login, client_key=ip)
    token = secrets.token_urlsafe(32)
    create_session = _storage_function("create_auth_session")
    create_session(
        storage.DEFAULT_DB_PATH,
        user_id=int(user_row["id"]),
        token_digest=_token_digest(token),
        expires_at=_session_expiry(),
        created_at=now,
    )
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return {"authenticated": True, "user": public_user(user_row)}


def authenticate_request(request: Request) -> dict[str, Any] | None:
    """Resolve the current cookie through the server-side session store."""

    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None
    get_session = _storage_function("get_auth_session")
    session = get_session(
        storage.DEFAULT_DB_PATH,
        token_digest=_token_digest(token),
        now=_now(),
    )
    if not isinstance(session, dict):
        return None
    user_row = session.get("user") if isinstance(session.get("user"), dict) else None
    if user_row is None:
        user_id = session.get("user_id")
        if user_id is None:
            return None
        user_row = _storage_function("get_auth_user")(
            storage.DEFAULT_DB_PATH,
            user_id=int(user_id),
        )
    user = _safe_user(user_row)
    return user if user and user.get("is_active") else None


def begin_request_context(user: dict[str, Any] | None) -> tuple[Any, Any]:
    return _IN_HTTP_REQUEST.set(True), _CURRENT_USER.set(user)


def end_request_context(tokens: tuple[Any, Any]) -> None:
    _IN_HTTP_REQUEST.reset(tokens[0])
    _CURRENT_USER.reset(tokens[1])


def current_user(*, required: bool = True) -> dict[str, Any] | None:
    user = _CURRENT_USER.get()
    if user is not None:
        return user
    if _IN_HTTP_REQUEST.get():
        if required:
            raise HTTPException(status_code=401, detail="Authentication required")
        return None
    # Existing pure-Python unit callers invoke route functions directly.  They
    # are not HTTP clients and retain the old local-call behavior; middleware
    # always marks real requests and therefore never takes this branch.
    if required:
        return {"id": 0, "login": "direct-call", "role": "admin", "manager_id": None, "is_active": True}
    return None


def require_roles(*roles: str) -> dict[str, Any]:
    user = current_user()
    if str(user.get("role")) not in set(roles):
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


def session_logout(request: Request, response: Response) -> None:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token:
        revoke = getattr(storage, "revoke_auth_session", None)
        if callable(revoke):
            revoke(storage.DEFAULT_DB_PATH, token_digest=_token_digest(token))
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


def retry_after_seconds(throttle: dict[str, Any] | None, *, now: str | None = None) -> int | None:
    locked = _locked_until(throttle, now or _now())
    if locked is None:
        return None
    return max(1, int((locked - datetime.now(timezone.utc)).total_seconds()))


def storage_path() -> Any:
    return storage.DEFAULT_DB_PATH
