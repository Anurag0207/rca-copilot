"""
Authentication routes — /auth

Endpoints:
  POST /auth/register   — create a new user account
  POST /auth/token      — login and receive a JWT (OAuth2 password flow)
  POST /auth/refresh    — exchange a valid token for a fresh one

All other API routes require the JWT issued here as a Bearer token in the
Authorization header.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.config import Settings, get_settings
from app.models.db import User
from app.models.schemas import RegisterRequest, TokenResponse, UserResponse
from app.utils.security import (
    create_access_token,
    get_password_hash,
    verify_password,
)

router = APIRouter()


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Create a new user account.

    Raises:
        HTTPException(409): If username or email is already taken.
    """
    # Check username uniqueness
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already registered",
        )

    # Check email uniqueness
    existing_email = await db.execute(select(User).where(User.email == body.email))
    if existing_email.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=get_password_hash(body.password),
        roles=[],
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Login and receive a JWT access token",
)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """
    Authenticate with username + password (OAuth2 Password flow).

    Raises:
        HTTPException(401): If credentials are invalid.
    """
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()

    # Generic error message to avoid username enumeration
    invalid_creds = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect username or password",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if user is None or not verify_password(form.password, user.hashed_password):
        raise invalid_creds

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account",
        )

    token = create_access_token({"sub": str(user.id)}, settings=settings)
    return TokenResponse(
        access_token=token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh an access token",
)
async def refresh_token(
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """
    Issue a fresh JWT for an already-authenticated user.

    The existing token must be valid (not expired). Returns a new token with
    a full TTL reset.
    """
    token = create_access_token({"sub": str(current_user.id)}, settings=settings)
    return TokenResponse(
        access_token=token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
