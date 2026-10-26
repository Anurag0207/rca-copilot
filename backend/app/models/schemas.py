"""
Pydantic API schemas and internal dataclasses for the NL-to-Data Insight Platform.

SECTION A — Internal dataclasses (not exposed via API):
  ColumnMeta, TableMeta, SchemaContext, ParsedIntent, QueryPlan,
  ClarificationRequest, DownloadTokenMeta, ConversationTurn, SessionState

SECTION B — Pydantic API schemas (request/response bodies):
  RegisterRequest, UserResponse, TokenResponse, CreateSessionRequest,
  SessionSummary, SessionResponse, SessionDetailResponse,
  MessageRequest, MessageResponse
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


# ─────────────────────────────────────────────────────────────────────────────
# SECTION A — Enums
# ─────────────────────────────────────────────────────────────────────────────


class OutputFormat(str, Enum):
    """Output file format requested by the user (or inferred from NL prompt)."""

    CHART = "chart"
    PDF = "pdf"
    PPT = "ppt"


class ChartType(str, Enum):
    """Visual chart type inferred from the NL prompt."""

    BAR = "bar"
    LINE = "line"
    PIE = "pie"
    TABLE = "table"


class MessageRole(str, Enum):
    """Conversation turn author."""

    USER = "user"
    ASSISTANT = "assistant"


class AuditStatus(str, Enum):
    """Outcome of a single pipeline run recorded in audit_logs."""

    SUCCESS = "success"
    CLARIFICATION = "clarification"
    ERROR = "error"


class RequestType(str, Enum):
    """
    Determines which pipeline branch handles the prompt.

    DATA_QUERY      — requires SQL/aggregation and chart/PDF/PPT output
    STRUCTURE_QUERY — explicit diagram request; output is a Mermaid PNG
    DESCRIBE_QUERY  — conversational question about what the data contains;
                      answered in plain text, no file output
    """

    DATA_QUERY = "data_query"
    STRUCTURE_QUERY = "structure_query"
    DESCRIBE_QUERY = "describe_query"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION A — Internal Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ColumnMeta:
    """Metadata for a single warehouse table column."""

    name: str
    data_type: str
    nullable: bool
    description: str | None = None
    sample_values: list[Any] = field(default_factory=list)  # up to 5 distinct non-null values
    null_pct: float = 0.0                                   # fraction of sampled docs where this field is null


@dataclass
class TableMeta:
    """Metadata for a single warehouse table."""

    name: str
    database: str
    columns: list[ColumnMeta] = field(default_factory=list)
    description: str | None = None
    row_count_estimate: int | None = None

    @property
    def qualified_name(self) -> str:
        """Return fully-qualified ``database.table`` identifier."""
        return f"{self.database}.{self.name}"


@dataclass
class SchemaContext:
    """
    Full schema snapshot for a warehouse connection.

    Keyed by fully-qualified table name (``database.table``).
    Cached in Redis under ``schema:{connection_id}`` with a 5-minute TTL.
    """

    tables: dict[str, TableMeta]    # key: "database.table"
    fetched_at: datetime
    connector_type: str


@dataclass
class ParsedIntent:
    """
    Structured intent extracted from a user's natural-language prompt by the LLM.

    When ``needs_clarification`` is True, no query should be generated.
    The ``clarification_question`` should be surfaced directly to the user.

    ``request_type`` drives pipeline branching:
      DATA_QUERY      → SQL generation + chart/PDF/PPT output
      STRUCTURE_QUERY → Mermaid diagram generation (no SQL)
    """

    entities: list[str]                     # business entities / table hints
    metrics: list[str]                      # columns or aggregation expressions
    filters: dict[str, Any]                 # WHERE conditions {column: value}
    time_range: str | None                  # e.g. "last quarter", "2025-Q1"
    output_format: OutputFormat | None      # None → ask user to choose
    chart_type: ChartType | None
    needs_clarification: bool
    clarification_question: str | None
    request_type: RequestType = field(default=RequestType.DATA_QUERY)
    user_question: str = ""                 # raw user prompt — preserved for the query planner


@dataclass
class QueryPlan:
    """A concrete, executable query generated from a ParsedIntent."""

    query_string: str
    target_tables: list[str]
    estimated_complexity: Literal["low", "medium", "high"]
    parameters: dict[str, Any] = field(default_factory=dict)
    reasoning: str | None = None  # LLM's METRIC/DIMENSION/JOIN-path reasoning — surfaced to the user as a trace


@dataclass
class ClarificationRequest:
    """
    Returned by QueryPlanner when the intent cannot be mapped to the schema.

    ``options`` provides optional multiple-choice hints the UI can render as
    quick-reply buttons.
    """

    question: str
    options: list[str] | None = None


@dataclass
class DownloadTokenMeta:
    """Metadata for a single generated output file, stored in the session."""

    file_path: str
    output_format: OutputFormat
    filename: str
    expires_at: datetime


@dataclass
class ConversationTurn:
    """A single exchange in the multi-turn conversation."""

    role: MessageRole
    content: str
    timestamp: datetime
    query_plan: QueryPlan | None = None
    download_token: str | None = None
    model_used: str | None = None  # which model produced this turn (models are switchable per-message)


@dataclass
class SessionState:
    """
    Full in-memory representation of an active session.

    Serialized to JSON and stored in Redis under
    ``session:{user_id}:{session_id}`` with a 24-h sliding TTL.
    Never written to PostgreSQL (only metadata in session_records).
    """

    session_id: str
    user_id: str
    connection_id: str
    history: list[ConversationTurn] = field(default_factory=list)
    download_tokens: dict[str, DownloadTokenMeta] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION B — Pydantic API Schemas
# ─────────────────────────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    """POST /auth/register request body."""

    username: str = Field(..., min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(..., min_length=8, description="Plaintext; hashed before storage")


class UserResponse(BaseModel):
    """Public user representation returned by auth endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: EmailStr
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    """Successful authentication token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token lifetime in seconds")


class CreateSessionRequest(BaseModel):
    """POST /chat/sessions request body."""

    connection_id: str = Field(
        ...,
        description="MCP connector connection identifier, e.g. 'hive_prod'",
    )


class SessionSummary(BaseModel):
    """Lightweight session representation for list endpoints."""

    session_id: str
    connection_id: str
    created_at: datetime
    last_activity: datetime
    turn_count: int


class SessionResponse(BaseModel):
    """Response returned on session creation."""

    session_id: str
    connection_id: str
    created_at: datetime


class MessageResponse(BaseModel):
    """A single assistant message returned by the chat pipeline."""

    # model_used/model_id are domain fields (which LLM answered), not related
    # to BaseModel's own model_* methods — silence pydantic's namespace warning.
    model_config = ConfigDict(protected_namespaces=())

    role: MessageRole
    content: str
    needs_clarification: bool = False
    clarification_question: str | None = None
    download_token: str | None = None          # chart PNG compound token
    pdf_download_token: str | None = None      # PDF compound token (always generated)
    output_format: OutputFormat | None = None
    summary_stats: dict | None = None          # SummaryStats as dict, for future frontend use
    reasoning: str | None = None               # query planner's METRIC/DIMENSION/JOIN-path trace
    generated_query: str | None = None         # the executed SQL/aggregation pipeline
    target_tables: list[str] | None = None     # tables/collections the query touched
    model_used: str | None = None              # which model produced this turn
    timestamp: datetime


class SessionDetailResponse(BaseModel):
    """Full session detail including conversation history."""

    session_id: str
    connection_id: str
    history: list[MessageResponse]
    created_at: datetime
    last_activity: datetime


class MessageRequest(BaseModel):
    """POST /chat/sessions/{id}/messages request body."""

    model_config = ConfigDict(protected_namespaces=())

    content: str = Field(..., min_length=1, max_length=4096)
    model_id: str | None = Field(
        default=None,
        description="Model id to use for this message (from GET /chat/models). "
        "None falls back to the server-configured default (settings.OLLAMA_MODEL).",
    )
