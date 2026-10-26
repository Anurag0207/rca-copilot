"""
FastAPI application entry point for the NL-to-Data Insight Platform.

Creates the application via the factory function ``create_app()``. The module-level
``app`` variable is the ASGI entry point used by uvicorn:

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Lifespan events:
  startup  — create DB tables (dev), init Redis pool
  shutdown — close Redis pool gracefully
"""

from __future__ import annotations

import sys

# Force UTF-8 on stdout/stderr before any other module can log anything.
#
# structlog's default (unconfigured) logger writes via print() to sys.stdout.
# On Windows, sys.stdout defaults to the console's codepage (cp1252 in most
# locales) regardless of the source string's actual encoding — and cp1252
# cannot represent most Unicode characters (arrows, checkmarks, em dashes,
# etc.), which local LLMs routinely produce in free-text fields like a query
# plan's "reasoning". Printing one of those crashes the *process*, not just
# the log call: UnicodeEncodeError propagates out of print() uncaught. UTF-8
# can encode any valid Unicode string, so this removes the failure mode at
# its source. This must NOT be "fixed" by sanitizing/stripping what gets
# logged — the content is legitimate model output; the encoding was wrong.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import close_evidence_registry
from app.api.routes import auth, chat, output, rca
from app.config import get_settings

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manage application startup and shutdown lifecycle.

    Startup order:
      1. Validate configuration (pydantic-settings handles this at import)
      2. Create database tables (development mode only)
      3. Initialise Redis connection pool

    Shutdown:
      - Close Redis pool
    """
    settings = get_settings()
    logger.info(
        "Starting NL-to-Data Insight Platform",
        environment=settings.ENVIRONMENT,
        ollama_model=settings.OLLAMA_MODEL,
        connector=settings.MCP_CONNECTOR_TYPE,
    )

    # Create DB tables in development (use Alembic in production)
    if settings.ENVIRONMENT == "development":
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.db import create_db_tables

        engine = create_async_engine(str(settings.DATABASE_URL), future=True)
        try:
            await create_db_tables(engine)
            logger.info("Database tables ready")
        except Exception as exc:
            logger.error("DB table creation failed", error=str(exc))
        finally:
            await engine.dispose()

    # Initialise Redis pool (shared by deps.get_redis via global in deps.py)
    import redis.asyncio as aioredis
    redis_pool = aioredis.from_url(str(settings.REDIS_URL), decode_responses=True)
    app.state.redis = redis_pool

    yield  # application is running

    logger.info("Shutting down NL-to-Data Insight Platform")
    await redis_pool.aclose()
    await close_evidence_registry()


def create_app() -> FastAPI:
    """
    Application factory — creates, configures, and returns the FastAPI instance.

    Separating construction into a factory enables clean unit testing by
    calling create_app() with overridden dependencies.
    """
    settings = get_settings()

    app = FastAPI(
        title="NL-to-Data Insight Platform",
        description=(
            "Query your data warehouse in plain English and download results "
            "as chart images, PDF reports, or PowerPoint decks. "
            "Powered entirely by local models — Ollama (qwen2.5:7b) + "
            "mermaid-cli for structural diagram generation."
        ),
        version="0.2.0",
        docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
        redoc_url="/redoc" if settings.ENVIRONMENT != "production" else None,
        lifespan=lifespan,
    )

    # ── Middleware ─────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(chat.router, prefix="/chat", tags=["chat"])
    app.include_router(output.router, prefix="/output", tags=["output"])
    app.include_router(rca.router, prefix="/rca", tags=["rca"])

    # ── Health probe ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["health"], summary="Liveness probe")
    async def health_check() -> dict[str, str]:
        """Returns 200 OK when the application process is running."""
        return {"status": "ok"}

    return app


# ASGI entry point
app = create_app()
