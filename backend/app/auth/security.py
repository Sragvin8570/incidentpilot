import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.models import User

_PASSWORD_ROUNDS = 600_000
_TOKEN_ISSUER = "incidentpilot"
_BEARER = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Passwords must contain at least 12 characters")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PASSWORD_ROUNDS)
    return "$".join(
        (
            "pbkdf2_sha256",
            str(_PASSWORD_ROUNDS),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, rounds_text, salt_text, digest_text = stored_hash.split("$")
        rounds = int(rounds_text)
        if algorithm != "pbkdf2_sha256" or rounds < 100_000:
            return False
        expected = _b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            _b64decode(salt_text),
            rounds,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def create_access_token(
    user: User,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_expiry_minutes),
        "iss": _TOKEN_ISSUER,
    }
    return jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def decode_access_token(token: str, settings: Settings) -> UUID:
    payload = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=["HS256"],
        issuer=_TOKEN_ISSUER,
        options={"require": ["sub", "iat", "exp", "iss"]},
    )
    return UUID(payload["sub"])


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_BEARER),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        user_id = decode_access_token(credentials.credentials, settings)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise _unauthorized() from None
    async with request.app.state.session_factory() as session:
        user = await session.scalar(
            select(User).where(User.id == user_id, User.is_active.is_(True))
        )
        if user is None:
            raise _unauthorized()
        session.expunge(user)
        return user


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
