"""
FastAPI shared dependency providers.

All injectable dependencies are defined here as async generator functions or
callables consumed via FastAPI's ``Depends()`` mechanism.

Dependencies:
  get_db()             — yields an async SQLAlchemy session
  get_redis()          — returns the shared async Redis client
  get_current_user()   — decodes JWT, returns the authenticated User
  get_llm_provider()   — returns the configured LLMProviderBase (OllamaProvider)
  get_intent_parser()  — IntentParser wired to the LLM provider
  get_query_planner()  — QueryPlanner wired to the LLM provider
  get_connector_registry() — returns the shared ConnectorRegistry (resolves connection_id -> connector)
  get_schema_manager() — SchemaManager wired to MCP + Redis
  get_session_manager() — SessionManager bound to Redis
"""

from __future__ import annotations

from typing import AsyncGenerator

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings, get_settings
from app.core.intent_parser import IntentParser
from app.core.query_planner import QueryPlanner
from app.core.schema_manager import SchemaManager
from app.connectors.registry import EvidenceRegistry
from app.core.session_manager import SessionManager
from app.llm.base import LLMProviderBase
from app.llm.ollama_provider import OllamaProvider
from app.mcp.registry import ConnectorRegistry
from app.models.db import User
from app.rca.engine import RCAEngine
from app.utils.security import verify_token

logger = structlog.get_logger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")

# ─────────────────────────────────────────────────────────────────────────────
# Module-level singletons (initialised on first use)
# ─────────────────────────────────────────────────────────────────────────────

_engine = None
_session_factory: "async_sessionmaker[AsyncSession] | None" = None
_redis_pool = None
_llm_provider: LLMProviderBase | None = None
_connector_registry: ConnectorRegistry | None = None
_evidence_registry: EvidenceRegistry | None = None
_rca_engine: RCAEngine | None = None


def _get_engine(settings: Settings):
    global _engine, _session_factory
    if _engine is None:
        _engine = create_async_engine(
            str(settings.DATABASE_URL),
            future=True,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────


async def get_db(
    settings: Settings = Depends(get_settings),
) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async SQLAlchemy session; always closed after the request."""
    _get_engine(settings)
    assert _session_factory is not None
    async with _session_factory() as session:
        yield session


# ─────────────────────────────────────────────────────────────────────────────
# Redis
# ─────────────────────────────────────────────────────────────────────────────


async def get_redis(
    settings: Settings = Depends(get_settings),
):
    """Return the shared async Redis client (connection pool managed by redis-py)."""
    global _redis_pool
    if _redis_pool is None:
        import redis.asyncio as aioredis
        _redis_pool = aioredis.from_url(
            str(settings.REDIS_URL),
            decode_responses=True,
        )
    return _redis_pool


# ─────────────────────────────────────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────────────────────────────────────


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """
    Validate the Bearer JWT and return the authenticated User.

    Raises:
        HTTPException(401): Token missing, malformed, or expired.
        HTTPException(401): user_id not found in DB.
        HTTPException(403): User account is inactive.
    """
    payload = verify_token(token, settings)
    user_id: str | None = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account",
        )
    return user


# ─────────────────────────────────────────────────────────────────────────────
# LLM provider (Ollama — single local model, no cloud)
# ─────────────────────────────────────────────────────────────────────────────


def get_llm_provider(
    settings: Settings = Depends(get_settings),
) -> LLMProviderBase:
    """
    Return the shared OllamaProvider instance (singleton).

    The provider connects to the local Ollama service at OLLAMA_BASE_URL
    using a single model (OLLAMA_MODEL, default qwen2.5:7b) for all tasks:
    intent parsing, HQL/SQL generation, and Mermaid diagram generation.
    """
    global _llm_provider
    if _llm_provider is None:
        _llm_provider = OllamaProvider(settings=settings)
    return _llm_provider


def get_intent_parser(
    llm: LLMProviderBase = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
) -> IntentParser:
    """Return an IntentParser wired to the local LLM provider."""
    return IntentParser(llm_provider=llm, settings=settings)


def get_query_planner(
    llm: LLMProviderBase = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
) -> QueryPlanner:
    """Return a QueryPlanner wired to the local LLM provider."""
    return QueryPlanner(llm_provider=llm, settings=settings)


# ─────────────────────────────────────────────────────────────────────────────
# MCP connector registry
# ─────────────────────────────────────────────────────────────────────────────


def get_connector_registry(
    settings: Settings = Depends(get_settings),
) -> ConnectorRegistry:
    """
    Return the shared ConnectorRegistry (singleton).

    The registry resolves a connection_id (from session state or the request
    body) to a live MCPConnectorBase, building and caching connectors lazily.
    Callers that need to actually talk to a warehouse call
    ``registry.get(connection_id)`` once the connection_id is known — this
    dependency alone does not pick a connector.

    Add new connector types by:
      1. Adding a class in app/mcp/{name}_connector.py
      2. Adding "{name}" to MCP_CONNECTOR_TYPE enum in config.py
      3. Adding a branch in ConnectorRegistry._build()
    """
    global _connector_registry
    if _connector_registry is None:
        _connector_registry = ConnectorRegistry(settings=settings)
    return _connector_registry


# ─────────────────────────────────────────────────────────────────────────────
# Schema manager & session manager
# ─────────────────────────────────────────────────────────────────────────────


def get_schema_manager(
    registry: ConnectorRegistry = Depends(get_connector_registry),
    redis=Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> SchemaManager:
    """Return a SchemaManager wired to the connector registry and Redis cache."""
    return SchemaManager(
        connector_registry=registry,
        redis_client=redis,
        cache_ttl_seconds=settings.SCHEMA_CACHE_TTL_SECONDS,
    )


def get_session_manager(
    redis=Depends(get_redis),
) -> SessionManager:
    """Return a SessionManager bound to the Redis client."""
    return SessionManager(redis_client=redis)


# ─────────────────────────────────────────────────────────────────────────────
# RCA Copilot
# ─────────────────────────────────────────────────────────────────────────────


def get_evidence_registry(settings: Settings = Depends(get_settings)) -> EvidenceRegistry:
    """Shared registry of the evidence connectors enabled via RCA_CONNECTORS."""
    global _evidence_registry
    if _evidence_registry is None:
        _evidence_registry = EvidenceRegistry.from_settings(settings)
    return _evidence_registry


def get_rca_engine(
    registry: EvidenceRegistry = Depends(get_evidence_registry),
    llm: LLMProviderBase = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
) -> RCAEngine:
    """Shared RCAEngine wired to the evidence registry and the local LLM."""
    global _rca_engine
    if _rca_engine is None:
        _rca_engine = RCAEngine(registry=registry, llm=llm, settings=settings)
    return _rca_engine


async def close_evidence_registry() -> None:
    """Called on app shutdown."""
    if _evidence_registry is not None:
        await _evidence_registry.close_all()
