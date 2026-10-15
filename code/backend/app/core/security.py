"""Password hashing and JWT primitives.

bcrypt is used directly instead of ``passlib``: passlib 1.7.4 reads the removed
``bcrypt.__about__`` attribute and fails against bcrypt >= 4.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.exceptions import AuthenticationError

#: bcrypt hashes at most 72 bytes and raises rather than truncating.
MAX_PASSWORD_BYTES = 72

#: Cost factor; ~250 ms per hash on a modern core.
DEFAULT_ROUNDS = 12

#: Token kind marker, so a future refresh token cannot be used as an access token.
ACCESS_TOKEN_TYPE = "access"


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    """Return a salted bcrypt hash of ``password``."""
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=DEFAULT_ROUNDS)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check ``password`` against a stored hash.

    A malformed or legacy hash is reported as a failed verification rather than
    raising, so corrupt rows cannot turn a login attempt into a 500.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


@lru_cache
def dummy_password_hash() -> str:
    """A valid hash to compare against when the email is unknown.

    Without this, a login for a non-existent account returns noticeably faster
    than one for an existing account, letting an attacker enumerate registered
    emails by timing. Computed once per process, on first use.
    """
    return hash_password("timing-equalisation-placeholder")


# ----------------------------------------------------------------- tokens
def create_access_token(
    subject: uuid.UUID | str,
    role: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Sign a short-lived access token for ``subject`` (a user id)."""
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": str(subject),
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a token and return its claims.

    ``algorithms`` is pinned to the configured algorithm (prevents the classic
    ``alg: none`` / algorithm-confusion bypass) and ``exp``/``sub`` are required
    rather than merely validated-if-present.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Authentication token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid authentication token.") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise AuthenticationError("Invalid authentication token.")
    return payload


def access_token_ttl_seconds() -> int:
    """Lifetime of a freshly issued access token, for the login response."""
    return settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
