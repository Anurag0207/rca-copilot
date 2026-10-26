"""
JWT and password security helpers.

Provides:
  - create_access_token: signs a JWT with HS256
  - verify_token: decodes and validates a JWT, raises HTTP 401 on failure
  - get_password_hash: bcrypt-hashes a plaintext password
  - verify_password: verifies plaintext against a stored hash

All secrets are read from the Settings object — nothing is hardcoded here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import Settings, get_settings

# bcrypt is the only enabled scheme; deprecated="auto" handles hash upgrades
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ALGORITHM = "HS256"


def create_access_token(
    data: dict[str, Any],
    settings: Settings | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """
    Sign and return a JWT.

    Args:
        data: Payload dict. Must include ``sub`` (str user_id).
        settings: App settings; resolved from cache if omitted.
        expires_delta: Custom TTL; defaults to ACCESS_TOKEN_EXPIRE_MINUTES.

    Returns:
        Encoded JWT string.
    """
    _settings = settings or get_settings()
    to_encode = data.copy()

    expire = datetime.now(UTC) + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=_settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire, "jti": str(uuid4())})
    return jwt.encode(to_encode, _settings.SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    """
    Decode and validate a JWT.

    Raises:
        HTTPException(401): on any JWT validation failure.

    Returns:
        Decoded payload dict.
    """
    _settings = settings or get_settings()
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload: dict[str, Any] = jwt.decode(
            token, _settings.SECRET_KEY, algorithms=[ALGORITHM]
        )
        return payload
    except JWTError as exc:
        raise credentials_exception from exc


def get_password_hash(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Return True if *plain_password* matches *hashed_password*."""
    return pwd_context.verify(plain_password, hashed_password)
