"""
Application configuration for the NL-to-Data Insight Platform.

All runtime settings are loaded from environment variables (or a .env file in
development). Secrets are never hardcoded — they must be supplied via the
environment. Use `get_settings()` as a FastAPI dependency to access config
throughout the application.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Pydantic-settings model that reads from environment variables.

    Validation is performed at startup; missing required fields raise
    a `ValidationError` that prevents the application from starting with
    an incomplete configuration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: str = "INFO"

    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(
        ...,
        description="HS256 JWT signing key — generate with: openssl rand -hex 32",
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8 hours

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = Field(
        default=["http://localhost:5173"],
        description="Comma-separated list of allowed CORS origins",
    )

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: PostgresDsn = Field(
        ...,
        description="Async PostgreSQL DSN using asyncpg driver, "
        "e.g. postgresql+asyncpg://user:pass@host:5432/dbname",
    )

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: RedisDsn = Field(
        ...,
        description="Redis connection URL, e.g. redis://localhost:6379/0",
    )
    SESSION_TTL_SECONDS: int = 86400       # 24 hours — sliding window
    SCHEMA_CACHE_TTL_SECONDS: int = 300    # 5 minutes

    # ── Ollama (single local LLM for all tasks) ───────────────────────────────
    OLLAMA_BASE_URL: str = Field(
        default="http://ollama:11434",
        description="Base URL of the Ollama service (no /v1 suffix)",
    )
    OLLAMA_MODEL: str = Field(
        default="qwen2.5:7b",
        description="Ollama model for intent parsing, HQL generation, and Mermaid diagram generation",
    )
    OLLAMA_MAX_TOKENS: int = 2048
    OLLAMA_TIMEOUT_SECONDS: float = 60.0   # local inference is slower than cloud

    # ── MCP Connector ─────────────────────────────────────────────────────────
    MCP_CONNECTOR_TYPE: Literal["hive", "mongodb"] = Field(
        default="mongodb",
        description="Active MCP connector type. Add new values when implementing new connectors.",
    )
    MCP_DEFAULT_CONNECTION_ID: str = "default"

    # ── MongoDB Connector (active when MCP_CONNECTOR_TYPE == "mongodb") ───────
    MONGODB_URI: str = Field(
        default="mongodb://localhost:27017",
        description="MongoDB connection URI, e.g. mongodb://user:pass@host:27017. "
        "Used as the fallback URI for MCP_DEFAULT_CONNECTION_ID when that id "
        "is not present in MONGO_CONNECTIONS.",
    )
    MONGODB_DATABASE: str = Field(
        default="ecommerce_db",
        description="Fallback MongoDB database for MCP_DEFAULT_CONNECTION_ID "
        "(see MONGODB_URI). Superseded by MONGO_CONNECTIONS when set.",
    )
    MONGO_CONNECTIONS: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Named MongoDB connections, keyed by connection_id, e.g. "
        '\'{"olist": {"uri": "mongodb://mongodb:27017", "database": "ecommerce_db"}, '
        '"retail_wh": {"uri": "mongodb://mongodb:27017", "database": "retail_warehouse_db"}}\'. '
        "Lets a single deployment serve multiple selectable data sources — the "
        "frontend passes connection_id when creating a chat session, and "
        "ConnectorRegistry looks it up here. JSON-decoded automatically from "
        "the env var by pydantic-settings.",
    )

    # ── Hive Connector (active when MCP_CONNECTOR_TYPE == "hive") ─────────────
    HIVE_HOST: str = "localhost"
    HIVE_PORT: int = 10000
    HIVE_DATABASE: str = "default"
    HIVE_AUTH_MECHANISM: Literal["PLAIN", "KERBEROS", "LDAP"] = "PLAIN"
    HIVE_USERNAME: str = ""
    HIVE_PASSWORD: str = ""

    # ── RCA Copilot ───────────────────────────────────────────────────────────
    RCA_CONNECTORS: list[dict[str, Any]] = Field(
        default_factory=list,
        description='Enabled evidence sources as JSON, e.g. [{"name": "logs", "type": '
        '"elasticsearch", "options": {"index": "app-logs-*"}}]. "type" selects a '
        "registered connector class; \"options\" are connector-specific overrides.",
    )
    RCA_CONNECTOR_TIMEOUT_SECONDS: float = 60.0   # per connector: planning + fetch
    RCA_BASELINE_MINUTES: int = 60                # baseline window before the incident start
    RCA_ZSCORE_THRESHOLD: float = 3.0
    RCA_MAX_RECORDS_PER_SOURCE: int = 2000
    RCA_RANKER_TIMEOUT_SECONDS: float = 90.0      # per ranker LLM call; one retry on timeout
    RCA_CHANGE_LOOKBACK_MINUTES: int = 15         # a change counts as "followed by an anomaly" within this

    # ── Elasticsearch connector ───────────────────────────────────────────────
    ES_URL: str = "http://localhost:9200"
    ES_INDEX: str = "app-logs-*"
    ES_USERNAME: str = ""
    ES_PASSWORD: str = ""
    ES_API_KEY: str = ""
    ES_VERIFY_CERTS: bool = True

    # ── Output Files ──────────────────────────────────────────────────────────
    OUTPUT_TEMP_DIR: str = "/tmp/nl_insight_outputs"
    OUTPUT_FILE_TTL_SECONDS: int = 3600   # 1 hour

    # ── Query Execution ───────────────────────────────────────────────────────
    QUERY_TIMEOUT_SECONDS: int = 25


@lru_cache
def get_settings() -> Settings:
    """
    Return the cached Settings singleton.

    Use as a FastAPI dependency:
        settings: Settings = Depends(get_settings)

    The @lru_cache ensures the .env file is read only once per process.
    """
    return Settings()
