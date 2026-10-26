"""
Chat routes — /chat

Endpoints:
  GET    /chat/connections                       — list connection_id values available for new sessions
  GET    /chat/providers                         — list available LLM providers (for the model picker)
  GET    /chat/models                            — list models available from the active LLM provider
  POST   /chat/sessions                          — create a new conversation session
  GET    /chat/sessions                          — list sessions for the current user
  GET    /chat/sessions/{session_id}             — get full session history
  POST   /chat/sessions/{session_id}/messages    — submit a NL prompt (pipeline entry point)
  DELETE /chat/sessions/{session_id}             — close and delete a session

The POST /messages endpoint routes to one of three pipelines based on request_type
(see Step 5/6 in send_message — intent parsing happens once, shared by all three):

DATA_QUERY pipeline:
  1.  Load session history from Redis
  2.  Get/refresh warehouse schema via SchemaManager
  3.  Parse intent (request_type=DATA_QUERY) via IntentParser
  4.  If needs_clarification → return clarification immediately
  5.  Generate HQL/SQL via QueryPlanner
  6.  If planner needs clarification → return with question
  7.  Validate query is read-only
  8.  Execute query via MCPConnector (forwarding user identity for RBAC)
  9.  Render matplotlib chart (always; reused in PDF/PPT)
  10. Generate output file: chart PNG / PDF / PPT
  11. Register compound download token in session
  12. Append both turns to session history
  13. Write AuditLog row to PostgreSQL
  14. Return MessageResponse with download_token

  Steps 5, 8, and 7 (planning, execution, and read-only validation) each run
  inside a shared 3-attempt retry loop (_run_data_pipeline) — any failure at
  those steps, plus the previous pipeline itself, is fed back to the LLM via
  QueryPlanner.plan_with_feedback() as raw evidence (row counts, the actual
  DB error, etc.), never a pre-solved diagnosis — the model has to work out
  the root cause itself against the schema, the same way it reasons through
  METRIC/DIMENSION/JOIN on a first attempt. Only exhausting all 3 attempts
  returns a graceful failure MessageResponse.

STRUCTURE_QUERY pipeline:
  1.  Load session history from Redis
  2.  Get/refresh warehouse schema via SchemaManager
  3.  Parse intent (request_type=STRUCTURE_QUERY) via IntentParser
  4.  If needs_clarification → return clarification immediately
  5.  Generate Mermaid diagram PNG via MermaidGenerator (LLM → mmdc → PNG)
  6.  Register compound download token in session
  7.  Append both turns to session history
  8.  Write AuditLog row to PostgreSQL
  9.  Return MessageResponse with download_token (PNG via chart format)

DESCRIBE_QUERY pipeline:
  1.  Load session history from Redis
  2.  Get/refresh warehouse schema via SchemaManager
  3.  Parse intent (request_type=DESCRIBE_QUERY) via IntentParser
  4.  If needs_clarification → return clarification immediately
  5.  One LLM call answers in plain prose, grounded only in the schema text —
      no query is ever executed
  6.  Append both turns to session history
  7.  Write AuditLog row to PostgreSQL
  8.  Return MessageResponse with no download_token

All three pipelines share one invariant: an LLM-side failure always resolves
to a normal assistant MessageResponse explaining what went wrong, never a
raw HTTPException — the frontend drops the user's own message from the
thread on any non-404 error response, so silently 5xx-ing mid-conversation
is treated as a bug, not an acceptable failure mode.

Download token format: ``{session_id}:{uuid4}``
  - The session_id prefix lets /output look up the session directly.
  - The uuid4 suffix is keyed in session.download_tokens.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_connector_registry,
    get_current_user,
    get_db,
    get_llm_provider,
    get_schema_manager,
    get_session_manager,
)
from app.config import Settings, get_settings
from app.core.intent_parser import IntentParser
from app.core.query_planner import QueryPlanner
from app.core.insight import InsightGenerator
from app.core.result_validator import validate_result
from app.core.stats import compute_stats
from app.core.schema_manager import SchemaManager
from app.core.session_manager import SessionManager
from app.llm.base import LLMProviderBase, ScopedLLMProvider
from app.mcp.base import MCPConnectorBase, UserIdentity
from app.mcp.registry import ConnectorRegistry
from app.models.db import AuditLog, SessionRecord, User
from app.models.schemas import (
    AuditStatus,
    ClarificationRequest,
    ConversationTurn,
    CreateSessionRequest,
    DownloadTokenMeta,
    MessageRequest,
    MessageResponse,
    MessageRole,
    OutputFormat,
    QueryPlan,
    RequestType,
    SessionDetailResponse,
    SessionResponse,
    SessionSummary,
)
from app.output.chart import ChartRenderer
from app.output.mermaid_gen import MermaidGenerator
from app.output.pdf_gen import PDFGenerator
from app.output.ppt_gen import PPTGenerator

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Connections
# ─────────────────────────────────────────────────────────────────────────────


@router.get(
    "/connections",
    response_model=list[str],
    summary="List available data source connection ids",
)
async def list_connections(
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> list[str]:
    """
    Return the connection_id values that POST /chat/sessions will accept.

    Sourced from settings.MONGO_CONNECTIONS; falls back to a single-item list
    with MCP_DEFAULT_CONNECTION_ID when no named connections are configured.
    Mirrors ConnectorRegistry's own resolution order exactly, so this list
    never advertises an id the registry would then reject.
    """
    if settings.MONGO_CONNECTIONS:
        return sorted(settings.MONGO_CONNECTIONS)
    return [settings.MCP_DEFAULT_CONNECTION_ID]


# ─────────────────────────────────────────────────────────────────────────────
# LLM providers & models — powers the chat UI's model picker
# ─────────────────────────────────────────────────────────────────────────────

# Static provider registry. Only one entry exists today (Ollama, local-only).
# Add a new dict here when a second LLMProviderBase implementation is wired
# into get_llm_provider() in api/deps.py — the frontend picker is fully
# data-driven and needs no changes to support additional providers.
_AVAILABLE_PROVIDERS: list[dict[str, str]] = [
    {"id": "ollama", "name": "Ollama (local)"},
]


@router.get(
    "/providers",
    response_model=list[dict[str, str]],
    summary="List available LLM providers",
)
async def list_providers(
    current_user: User = Depends(get_current_user),
) -> list[dict[str, str]]:
    """Return the LLM providers the chat UI can offer in its model picker."""
    return _AVAILABLE_PROVIDERS


@router.get(
    "/models",
    response_model=list[str],
    summary="List models available from the active LLM provider",
)
async def list_models(
    current_user: User = Depends(get_current_user),
    llm_provider: LLMProviderBase = Depends(get_llm_provider),
) -> list[str]:
    """
    Return model ids the user can pick from (e.g. Ollama's GET /v1/models).

    Raises:
        HTTPException(503): If the provider cannot be reached.
    """
    try:
        return await llm_provider.list_models()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=f"Could not list models: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Session lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new conversation session",
)
async def create_session(
    body: CreateSessionRequest,
    current_user: User = Depends(get_current_user),
    session_mgr: SessionManager = Depends(get_session_manager),
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    """Create a new Redis session and a corresponding session_records row in PostgreSQL."""
    state = await session_mgr.create_session(str(current_user.id), body.connection_id)
    record = SessionRecord(
        user_id=current_user.id,
        redis_session_id=state.session_id,
        connection_id=body.connection_id,
    )
    db.add(record)
    await db.commit()
    return SessionResponse(
        session_id=state.session_id,
        connection_id=body.connection_id,
        created_at=state.created_at,
    )


@router.get(
    "/sessions",
    response_model=list[SessionSummary],
    summary="List all sessions for the current user",
)
async def list_sessions(
    current_user: User = Depends(get_current_user),
    session_mgr: SessionManager = Depends(get_session_manager),
    db: AsyncSession = Depends(get_db),
) -> list[SessionSummary]:
    """Return only live (Redis-confirmed) sessions for the authenticated user.

    For each PostgreSQL session record, checks Redis for existence.
    Sessions missing from Redis (expired or cleared on restart) are marked
    closed in PostgreSQL and excluded from the response.
    """
    from sqlalchemy import select, update

    result = await db.execute(
        select(SessionRecord)
        .where(SessionRecord.user_id == current_user.id, SessionRecord.closed_at.is_(None))
        .order_by(SessionRecord.last_activity.desc())
    )
    records = result.scalars().all()

    live: list[SessionSummary] = []
    dead_ids: list[str] = []
    user_id = str(current_user.id)

    for r in records:
        if await session_mgr.session_exists(user_id, r.redis_session_id):
            live.append(
                SessionSummary(
                    session_id=r.redis_session_id,
                    connection_id=r.connection_id,
                    created_at=r.created_at,
                    last_activity=r.last_activity,
                    turn_count=r.turn_count,
                )
            )
        else:
            dead_ids.append(r.redis_session_id)

    if dead_ids:
        await db.execute(
            update(SessionRecord)
            .where(SessionRecord.redis_session_id.in_(dead_ids))
            .values(closed_at=datetime.now(UTC))
        )
        await db.commit()
        logger.info("Auto-closed expired sessions", count=len(dead_ids), session_ids=dead_ids)

    return live


@router.get(
    "/sessions/{session_id}",
    response_model=SessionDetailResponse,
    summary="Get full session history",
)
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session_mgr: SessionManager = Depends(get_session_manager),
    db: AsyncSession = Depends(get_db),
) -> SessionDetailResponse:
    """
    Load the full conversation history for *session_id* from Redis.

    Raises:
        HTTPException(404): If the session does not exist or has expired.
    """
    from sqlalchemy import update

    state = await session_mgr.get_session(str(current_user.id), session_id)
    if state is None:
        # Redis TTL expired — clean up the stale PostgreSQL record so it
        # doesn't reappear in the sessions list after the next login.
        await db.execute(
            update(SessionRecord)
            .where(
                SessionRecord.redis_session_id == session_id,
                SessionRecord.closed_at.is_(None),
            )
            .values(closed_at=datetime.now(UTC))
        )
        await db.commit()
        raise HTTPException(status_code=404, detail="Session not found or expired")
    return SessionDetailResponse(
        session_id=state.session_id,
        connection_id=state.connection_id,
        history=[_turn_to_response(t) for t in state.history],
        created_at=state.created_at,
        last_activity=state.last_activity,
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    summary="Close and delete a session",
)
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    session_mgr: SessionManager = Depends(get_session_manager),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete session from Redis and mark it closed in PostgreSQL."""
    await session_mgr.delete_session(str(current_user.id), session_id)
    from sqlalchemy import update
    await db.execute(
        update(SessionRecord)
        .where(SessionRecord.redis_session_id == session_id)
        .values(closed_at=datetime.now(UTC))
    )
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Core NL pipeline endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageResponse,
    summary="Submit a natural-language prompt and run the full pipeline",
)
async def send_message(
    session_id: str,
    body: MessageRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    session_mgr: SessionManager = Depends(get_session_manager),
    schema_mgr: SchemaManager = Depends(get_schema_manager),
    connector_registry: ConnectorRegistry = Depends(get_connector_registry),
    llm_provider: LLMProviderBase = Depends(get_llm_provider),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MessageResponse:
    """
    Route NL prompt to either the DATA_QUERY or STRUCTURE_QUERY pipeline.

    DATA_QUERY    → SQL + chart/PDF/PPT output
    STRUCTURE_QUERY → Mermaid diagram PNG (LLM → mmdc → PNG)

    If body.model_id is set, every LLM call made while handling this message
    (intent parsing, query planning, insight generation, diagram generation)
    uses that model instead of the server-configured default — the model
    picker in the UI is per-message, like switching models mid-conversation.

    Returns:
        MessageResponse — assistant reply + compound download_token for the output file.
    """
    start_time = time.monotonic()
    user_id = str(current_user.id)
    audit_status = AuditStatus.ERROR
    generated_query: str | None = None
    output_format_val: str | None = None

    model_used = body.model_id or settings.OLLAMA_MODEL
    effective_llm: LLMProviderBase = (
        ScopedLLMProvider(llm_provider, body.model_id) if body.model_id else llm_provider
    )
    intent_parser = IntentParser(llm_provider=effective_llm, settings=settings)
    query_planner = QueryPlanner(llm_provider=effective_llm, settings=settings)

    # ── Step 1: Load session ──────────────────────────────────────────────────
    state = await session_mgr.get_session(user_id, session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    # Resolve the connector for this session's data source. Deferred until here
    # (rather than a top-level Depends()) because connection_id only becomes
    # known once the session is loaded from Redis.
    try:
        connector = connector_registry.get(state.connection_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # ── Step 2: Build user identity ───────────────────────────────────────────
    user_identity = UserIdentity(
        user_id=user_id,
        username=current_user.username,
        roles=getattr(current_user, "roles", []),
        raw_jwt=request.headers.get("Authorization", "").removeprefix("Bearer ").strip(),
    )

    # ── Step 3: Append user turn ──────────────────────────────────────────────
    user_turn = ConversationTurn(
        role=MessageRole.USER,
        content=body.content,
        timestamp=datetime.now(UTC),
    )
    await session_mgr.append_turn(user_id, session_id, user_turn)

    # ── Step 4: Get warehouse schema ──────────────────────────────────────────
    schema = await schema_mgr.get_schema(state.connection_id, user_identity)

    # No collections at all means every downstream branch has nothing to work
    # with — the LLM would otherwise be prompted with an empty schema section
    # and freely hallucinate an unrelated answer. Fail loudly instead.
    if not schema.tables:
        reply = (
            f'No collections found in the "{state.connection_id}" data source yet. '
            "If this dataset was just added, make sure its data has actually been "
            "imported into MongoDB before asking questions here."
        )
        await _append_assistant_turn(session_mgr, user_id, session_id, reply)
        await _write_audit(
            db, current_user.id, session_id, body.content,
            None, None, AuditStatus.ERROR,
            f"connection_id={state.connection_id!r} resolved to an empty schema (no collections)",
            int((time.monotonic() - start_time) * 1000),
        )
        return MessageResponse(
            role=MessageRole.ASSISTANT,
            content=reply,
            timestamp=datetime.now(UTC),
        )

    # ── Step 5: Parse intent ──────────────────────────────────────────────────
    try:
        intent = await intent_parser.parse(body.content, state.history, schema)
    except RuntimeError as exc:
        # IntentParser already retried internally — nothing left to feed
        # back into, so fail as a normal chat reply instead of a raw error
        # (a raw HTTPException makes the frontend silently drop the user's
        # own message from the thread).
        logger.warning("Intent parsing failed", error=str(exc))
        reply = (
            "I had trouble understanding that request — the model's response wasn't in "
            "the expected format. Try rephrasing your question, or switch models in the "
            "picker above if this keeps happening."
        )
        await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
        await _write_audit(
            db, current_user.id, session_id, body.content,
            None, None, AuditStatus.ERROR, f"Intent parsing failed: {exc}",
            int((time.monotonic() - start_time) * 1000),
        )
        return MessageResponse(
            role=MessageRole.ASSISTANT,
            content=reply,
            model_used=model_used,
            timestamp=datetime.now(UTC),
        )

    logger.info(
        "Pipeline routing",
        session_id=session_id,
        user_prompt=body.content[:120],
        request_type=intent.request_type.value,
        entities=intent.entities,
        metrics=intent.metrics,
        needs_clarification=intent.needs_clarification,
    )

    if intent.needs_clarification:
        audit_status = AuditStatus.CLARIFICATION
        reply = intent.clarification_question or "Could you provide more detail about your query?"
        await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
        await _write_audit(
            db, current_user.id, session_id, body.content,
            None, None, audit_status, None,
            int((time.monotonic() - start_time) * 1000),
        )
        return MessageResponse(
            role=MessageRole.ASSISTANT,
            content=reply,
            needs_clarification=True,
            clarification_question=reply,
            model_used=model_used,
            timestamp=datetime.now(UTC),
        )

    # ── Step 6: Route by request type ────────────────────────────────────────
    if intent.request_type == RequestType.DESCRIBE_QUERY:
        return await _run_describe_pipeline(
            session_id=session_id,
            user_id=user_id,
            body=body,
            intent=intent,
            schema=schema,
            llm_provider=effective_llm,
            model_used=model_used,
            session_mgr=session_mgr,
            db=db,
            current_user=current_user,
            start_time=start_time,
        )

    if intent.request_type == RequestType.STRUCTURE_QUERY:
        return await _run_structure_pipeline(
            session_id=session_id,
            user_id=user_id,
            body=body,
            intent=intent,
            schema=schema,
            llm_provider=effective_llm,
            model_used=model_used,
            session_mgr=session_mgr,
            db=db,
            current_user=current_user,
            start_time=start_time,
            settings=settings,
        )

    return await _run_data_pipeline(
        session_id=session_id,
        user_id=user_id,
        body=body,
        intent=intent,
        schema=schema,
        user_identity=user_identity,
        query_planner=query_planner,
        connector=connector,
        llm_provider=effective_llm,
        model_used=model_used,
        session_mgr=session_mgr,
        db=db,
        current_user=current_user,
        start_time=start_time,
        settings=settings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline branches
# ─────────────────────────────────────────────────────────────────────────────


_DESCRIBE_PROMPT = """\
You are a helpful data analyst. The user asked: "{user_prompt}"

Below is the full schema of the available dataset:
{schema_summary}

Write a clear, friendly plain-English description that answers the user's question.
- Mention the collections/tables that exist and what each one represents.
- Give a sense of what kind of questions can be answered with this data.
- Keep it concise — 3 to 8 sentences.
- Do NOT use JSON, code blocks, or markdown headers. Plain prose only.
"""


async def _run_describe_pipeline(
    *,
    session_id: str,
    user_id: str,
    body: MessageRequest,
    intent,
    schema,
    llm_provider: LLMProviderBase,
    model_used: str,
    session_mgr: SessionManager,
    db: AsyncSession,
    current_user: User,
    start_time: float,
) -> MessageResponse:
    """
    Answer a conversational 'what is this data about?' question in plain text.

    No diagram, no chart, no file download — just an LLM-generated prose summary
    of the schema grounded in the actual collection/table names and fields.
    """
    logger.info(
        "Describe pipeline — NO database query will run",
        session_id=session_id,
        user_prompt=body.content[:120],
    )
    schema_summary = "\n".join(
        f"- {key}: {', '.join(c.name for c in table.columns[:8])}"
        + (" ..." if len(table.columns) > 8 else "")
        for key, table in schema.tables.items()
    )
    prompt = _DESCRIBE_PROMPT.format(
        user_prompt=body.content,
        schema_summary=schema_summary,
    )

    try:
        reply = await llm_provider.complete(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
        )
        reply = reply.strip()
    except Exception as exc:
        logger.warning("Describe pipeline LLM call failed", error=str(exc))
        reply = "I couldn't generate a description right now. Please try again."
        await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
        await _write_audit(
            db, current_user.id, session_id, body.content,
            None, None, AuditStatus.ERROR, f"Describe pipeline failed: {exc}",
            int((time.monotonic() - start_time) * 1000),
        )
        return MessageResponse(
            role=MessageRole.ASSISTANT,
            content=reply,
            model_used=model_used,
            timestamp=datetime.now(UTC),
        )

    await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
    await _write_audit(
        db, current_user.id, session_id, body.content,
        None, None, AuditStatus.SUCCESS, None,
        int((time.monotonic() - start_time) * 1000),
    )
    return MessageResponse(
        role=MessageRole.ASSISTANT,
        content=reply,
        model_used=model_used,
        timestamp=datetime.now(UTC),
    )


async def _run_structure_pipeline(
    *,
    session_id: str,
    user_id: str,
    body: MessageRequest,
    intent,
    schema,
    llm_provider: LLMProviderBase,
    model_used: str,
    session_mgr: SessionManager,
    db: AsyncSession,
    current_user: User,
    start_time: float,
    settings: Settings,
) -> MessageResponse:
    """
    Generate a Mermaid diagram PNG answering the structural/schema query.

    Uses MermaidGenerator: LLM produces Mermaid syntax → mmdc renders PNG.
    """
    token_uuid = str(uuid.uuid4())
    output_dir = Path(settings.OUTPUT_TEMP_DIR) / session_id / token_uuid
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "diagram.png"

    mermaid_gen = MermaidGenerator(llm_provider=llm_provider)
    try:
        await mermaid_gen.generate(intent, schema, output_path)
    except RuntimeError as exc:
        logger.warning("Diagram generation failed", error=str(exc))
        reply = (
            "I couldn't generate that diagram. Try rephrasing your request, or switch "
            "models in the picker above if this keeps happening."
        )
        await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
        await _write_audit(
            db, current_user.id, session_id, body.content,
            None, "chart", AuditStatus.ERROR, f"Diagram generation failed: {exc}",
            int((time.monotonic() - start_time) * 1000),
        )
        return MessageResponse(
            role=MessageRole.ASSISTANT,
            content=reply,
            model_used=model_used,
            timestamp=datetime.now(UTC),
        )

    # Compound download token: "{session_id}:{uuid}"
    compound_token = f"{session_id}:{token_uuid}"
    token_meta = DownloadTokenMeta(
        file_path=str(output_path),
        output_format=OutputFormat.CHART,
        filename="schema_diagram.png",
        expires_at=datetime.fromtimestamp(
            time.time() + settings.OUTPUT_FILE_TTL_SECONDS, tz=UTC
        ),
    )
    await session_mgr.register_download_token(user_id, session_id, token_uuid, token_meta)

    reply = (
        f"Here is the schema diagram for: *{body.content}*\n\n"
        "Download the Mermaid diagram PNG below."
    )
    await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
    await _write_audit(
        db, current_user.id, session_id, body.content,
        None, "chart", AuditStatus.SUCCESS,
        None,
        int((time.monotonic() - start_time) * 1000),
    )

    return MessageResponse(
        role=MessageRole.ASSISTANT,
        content=reply,
        download_token=compound_token,
        output_format=OutputFormat.CHART,
        model_used=model_used,
        timestamp=datetime.now(UTC),
    )


_MAX_VALIDATOR_ATTEMPTS = 3


async def _run_data_pipeline(
    *,
    session_id: str,
    user_id: str,
    body: MessageRequest,
    intent,
    schema,
    user_identity: UserIdentity,
    query_planner: QueryPlanner,
    connector: MCPConnectorBase,
    llm_provider: LLMProviderBase,
    model_used: str,
    session_mgr: SessionManager,
    db: AsyncSession,
    current_user: User,
    start_time: float,
    settings: Settings,
) -> MessageResponse:
    """
    Plan → execute → validate → retry loop (max 3 attempts), then chart/PDF/PPT output.

    On each attempt:
      1. Plan (or re-plan with feedback from the previous failure) — a
         RuntimeError (model returned unparseable output) is fed back into
         the next attempt exactly like any other failure
      2. Execute against MongoDB — a QueryExecutionError (bad field/type/operator)
         is fed back into the next attempt exactly like a failed semantic check;
         a QueryTimeoutError fails immediately instead (retrying a slow query
         3x just triples the wait for the same outcome)
      3. Run the result validator (3 semantic checks)
      4. Break on success; inject the failure hint and retry on failure
    After _MAX_VALIDATOR_ATTEMPTS failures (planning, execution, or semantic),
    return a structured failure reply instead of a raw error.
    """
    from app.mcp.base import QueryExecutionError, QueryTimeoutError

    generated_query: str | None = None
    # A factual observation of what went wrong on the previous attempt (row
    # counts, which field was null, the raw DB error, etc.) — never a
    # pre-solved diagnosis. Combined with previous_query and the schema, the
    # model has to work out the root cause itself on the next attempt, the
    # same way a person debugging this would.
    failure_reason: str | None = None
    previous_query: str | None = None
    plan_result: QueryPlan | None = None
    result = None

    for attempt in range(1, _MAX_VALIDATOR_ATTEMPTS + 1):

        # ── Plan (attempt 1) or re-plan with feedback (attempts 2-3) ─────────
        try:
            if attempt == 1:
                plan_or_clarify = await query_planner.plan(intent, schema, user_identity)
            else:
                logger.warning(
                    "[VALIDATOR] Re-planning with feedback",
                    attempt=f"{attempt}/{_MAX_VALIDATOR_ATTEMPTS}",
                    failure_reason=failure_reason,
                )
                plan_or_clarify = await query_planner.plan_with_feedback(
                    intent, schema, user_identity,
                    previous_query=previous_query or "",
                    failure_reason=failure_reason or "",
                )
        except RuntimeError as exc:
            # The model failed to produce parseable JSON even after
            # QueryPlanner's own internal retry. Treat it like any other
            # failed attempt — feed it back and try again — rather than
            # surfacing a raw 503 that skips the graceful failure message.
            logger.warning(
                "[VALIDATOR] planning failed to produce parseable output",
                error=str(exc),
                attempt=f"{attempt}/{_MAX_VALIDATOR_ATTEMPTS}",
            )
            if attempt == _MAX_VALIDATOR_ATTEMPTS:
                failure_reply = (
                    f"I wasn't able to generate a working query after {_MAX_VALIDATOR_ATTEMPTS} "
                    "attempts — the model kept returning text that wasn't a valid query "
                    "instead of following the required format. This usually means the "
                    "selected model isn't reliably instruction-following enough for this "
                    "task. Try switching to a different model in the picker above, or "
                    "rephrase your question."
                )
                await _append_assistant_turn(session_mgr, user_id, session_id, failure_reply, model_used=model_used)
                await _write_audit(
                    db, current_user.id, session_id, body.content,
                    None, None, AuditStatus.ERROR,
                    f"Planning failed after {_MAX_VALIDATOR_ATTEMPTS} attempts: {exc}",
                    int((time.monotonic() - start_time) * 1000),
                )
                return MessageResponse(
                    role=MessageRole.ASSISTANT,
                    content=failure_reply,
                    model_used=model_used,
                    timestamp=datetime.now(UTC),
                )
            # No previous_query to show here — the response never parsed into a
            # structured query in the first place, so there's nothing to reason
            # about except "the format itself was wrong."
            failure_reason = f"Your previous response could not be parsed as valid JSON: {exc}"
            continue

        # ── Handle planner clarification request ──────────────────────────────
        if isinstance(plan_or_clarify, ClarificationRequest):
            logger.info("Query planner requested clarification", question=plan_or_clarify.question)
            reply = plan_or_clarify.question
            await _append_assistant_turn(session_mgr, user_id, session_id, reply, model_used=model_used)
            await _write_audit(
                db, current_user.id, session_id, body.content,
                None, None, AuditStatus.CLARIFICATION, None,
                int((time.monotonic() - start_time) * 1000),
            )
            return MessageResponse(
                role=MessageRole.ASSISTANT,
                content=reply,
                needs_clarification=True,
                clarification_question=reply,
                model_used=model_used,
                timestamp=datetime.now(UTC),
            )

        plan_result = plan_or_clarify
        previous_query = plan_result.query_string
        logger.info(
            "Query plan generated",
            attempt=attempt,
            query=plan_result.query_string[:400],
            target_tables=plan_result.target_tables,
            complexity=plan_result.estimated_complexity,
        )

        # ── Validate read-only ────────────────────────────────────────────────
        # A non-SELECT query is something the model can fix — same treatment
        # as a QueryExecutionError below: feed it back and retry rather than
        # failing hard on the first offending attempt (we never execute the
        # unsafe query either way, so retrying here costs nothing in safety).
        if not QueryPlanner._validate_query_is_read_only(plan_result.query_string):
            logger.warning(
                "[VALIDATOR] generated query contains forbidden operations",
                attempt=f"{attempt}/{_MAX_VALIDATOR_ATTEMPTS}",
                query=plan_result.query_string[:200],
            )
            failure_reason = (
                "The previous query contained a write/DDL operation (INSERT, UPDATE, "
                "DELETE, DROP, CREATE, ALTER, or TRUNCATE), which is never allowed — "
                "only read-only SELECT / aggregation queries are permitted."
            )
            if attempt == _MAX_VALIDATOR_ATTEMPTS:
                failure_reply = (
                    f"I wasn't able to generate a safe, read-only query after "
                    f"{_MAX_VALIDATOR_ATTEMPTS} attempts. Please try rephrasing your question."
                )
                await _append_assistant_turn(session_mgr, user_id, session_id, failure_reply, model_used=model_used)
                await _write_audit(
                    db, current_user.id, session_id, body.content,
                    plan_result.query_string, None, AuditStatus.ERROR,
                    f"Generated query failed read-only validation after {_MAX_VALIDATOR_ATTEMPTS} attempts",
                    int((time.monotonic() - start_time) * 1000),
                )
                return MessageResponse(
                    role=MessageRole.ASSISTANT,
                    content=failure_reply,
                    model_used=model_used,
                    timestamp=datetime.now(UTC),
                )
            continue
        generated_query = plan_result.query_string

        # ── Execute query ─────────────────────────────────────────────────────
        try:
            result = await connector.execute_query(
                plan_result, user_identity, settings.QUERY_TIMEOUT_SECONDS
            )
        except QueryTimeoutError:
            # A slow query stays slow — retrying 3x just triples the wait for
            # the same outcome, so fail immediately instead of feeding it back.
            logger.error("Query timed out", query=plan_result.query_string[:200])
            raise HTTPException(status_code=504, detail="Query execution timed out")
        except QueryExecutionError as exc:
            # Unlike a timeout, this is usually something the model CAN fix —
            # a bad field name, wrong type, or unsupported operator combo — so
            # treat it exactly like a failed semantic check: feed the real
            # database error back into the next planning attempt.
            logger.warning(
                "[VALIDATOR] execution error, feeding back to planner",
                error=str(exc),
                attempt=f"{attempt}/{_MAX_VALIDATOR_ATTEMPTS}",
                query=plan_result.query_string[:200],
            )
            failure_reason = f"The query failed to execute against MongoDB with this error: {exc}"
            if attempt == _MAX_VALIDATOR_ATTEMPTS:
                failure_reply = (
                    f"I wasn't able to generate a working query after {_MAX_VALIDATOR_ATTEMPTS} "
                    "attempts — the database kept rejecting it. Please try rephrasing your question."
                )
                await _append_assistant_turn(session_mgr, user_id, session_id, failure_reply, model_used=model_used)
                await _write_audit(
                    db, current_user.id, session_id, body.content,
                    generated_query, None, AuditStatus.ERROR,
                    f"Execution failed after {_MAX_VALIDATOR_ATTEMPTS} attempts: {exc}",
                    int((time.monotonic() - start_time) * 1000),
                )
                return MessageResponse(
                    role=MessageRole.ASSISTANT,
                    content=failure_reply,
                    model_used=model_used,
                    timestamp=datetime.now(UTC),
                )
            continue

        logger.info(
            "Query result",
            attempt=attempt,
            rows_returned=result.row_count,
            columns=result.columns,
            execution_ms=result.execution_time_ms,
            preview=result.rows[:3],
        )

        # ── Validate result semantics ─────────────────────────────────────────
        check_name, failure_reason = validate_result(result.rows, plan_result, intent)
        if check_name is None:
            break  # all checks passed — proceed to output

        logger.warning(
            "[VALIDATOR] check failed",
            check=check_name,
            attempt=f"{attempt}/{_MAX_VALIDATOR_ATTEMPTS}",
        )

        if attempt == _MAX_VALIDATOR_ATTEMPTS:
            failure_reply = (
                f"I wasn't able to generate a correct query after {_MAX_VALIDATOR_ATTEMPTS} attempts. "
                "Please try rephrasing your question, or break it into a simpler query first."
            )
            await _append_assistant_turn(session_mgr, user_id, session_id, failure_reply, model_used=model_used)
            await _write_audit(
                db, current_user.id, session_id, body.content,
                generated_query, None, AuditStatus.ERROR,
                f"Validator failed after {_MAX_VALIDATOR_ATTEMPTS} attempts: {failure_reason}",
                int((time.monotonic() - start_time) * 1000),
            )
            return MessageResponse(
                role=MessageRole.ASSISTANT,
                content=failure_reply,
                model_used=model_used,
                timestamp=datetime.now(UTC),
            )

    # At this point plan_result and result are valid and validator-approved
    assert plan_result is not None and result is not None

    token_uuid = str(uuid.uuid4())
    output_dir = Path(settings.OUTPUT_TEMP_DIR) / session_id / token_uuid
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Step A: Compute summary stats ─────────────────────────────────────────
    stats = compute_stats(result.rows)

    # ── Step B: Generate insight (3rd LLM call, max_tokens=150) ──────────────
    insight_text = await InsightGenerator().generate(
        user_question=intent.user_question or body.content,
        rows=result.rows,
        stats=stats,
        llm=llm_provider,
    )
    logger.info("Insight generated", insight=insight_text[:120])

    # ── Step C: Render chart PNG (auto chart type) ────────────────────────────
    chart_renderer = ChartRenderer()
    chart_type = chart_renderer.pick_chart_type(result, intent.chart_type)
    chart_path: Path | None = None
    if result.rows:
        chart_path = output_dir / "chart.png"
        try:
            chart_path = await chart_renderer.render(
                result=result,
                chart_type=chart_type,
                title=body.content[:80],
                output_path=chart_path,
            )
        except Exception as exc:
            import traceback as _tb
            logger.error("Chart rendering failed", error=str(exc), traceback=_tb.format_exc())
            chart_path = None

    # ── Step D: Always generate PDF ───────────────────────────────────────────
    pdf_path = output_dir / "report.pdf"
    try:
        pdf_path = await PDFGenerator().generate(
            result=result,
            chart_path=chart_path,
            title=body.content[:80],
            query_context=generated_query or "",
            output_path=pdf_path,
            insight_text=insight_text,
            stats=stats,
        )
    except Exception as exc:
        logger.warning("PDF generation failed", error=str(exc))
        pdf_path = None  # type: ignore[assignment]

    # ── Step E: Handle explicit PPT request ───────────────────────────────────
    output_format = intent.output_format or OutputFormat.CHART
    output_format_val = output_format.value
    ppt_path: Path | None = None
    if output_format == OutputFormat.PPT:
        try:
            ppt_path = output_dir / "report.pptx"
            ppt_path = await PPTGenerator().generate(
                result=result,
                chart_path=chart_path,
                title=body.content[:80],
                output_path=ppt_path,
            )
        except Exception as exc:
            logger.warning("PPT generation failed", error=str(exc))
            ppt_path = None

    # ── Step F: Register download tokens ─────────────────────────────────────
    expires_at = datetime.fromtimestamp(
        time.time() + settings.OUTPUT_FILE_TTL_SECONDS, tz=UTC
    )

    # Chart PNG token (always if chart rendered)
    compound_chart_token: str | None = None
    if chart_path and chart_path.exists():
        chart_token_uuid = token_uuid          # reuse the same UUID prefix
        token_meta_chart = DownloadTokenMeta(
            file_path=str(chart_path),
            output_format=OutputFormat.CHART,
            filename="chart.png",
            expires_at=expires_at,
        )
        await session_mgr.register_download_token(
            user_id, session_id, chart_token_uuid, token_meta_chart
        )
        compound_chart_token = f"{session_id}:{chart_token_uuid}"

    # PDF token (always if PDF generated)
    compound_pdf_token: str | None = None
    if pdf_path and Path(pdf_path).exists():
        pdf_token_uuid = str(uuid.uuid4())
        token_meta_pdf = DownloadTokenMeta(
            file_path=str(pdf_path),
            output_format=OutputFormat.PDF,
            filename="report.pdf",
            expires_at=expires_at,
        )
        await session_mgr.register_download_token(
            user_id, session_id, pdf_token_uuid, token_meta_pdf
        )
        compound_pdf_token = f"{session_id}:{pdf_token_uuid}"

    # PPT token (only if PPT was requested and generated)
    if ppt_path and ppt_path.exists():
        ppt_token_uuid = str(uuid.uuid4())
        token_meta_ppt = DownloadTokenMeta(
            file_path=str(ppt_path),
            output_format=OutputFormat.PPT,
            filename="report.pptx",
            expires_at=expires_at,
        )
        await session_mgr.register_download_token(
            user_id, session_id, ppt_token_uuid, token_meta_ppt
        )

    # ── Step G: Append assistant turn + audit ─────────────────────────────────
    await _append_assistant_turn(
        session_mgr, user_id, session_id, insight_text,
        query_plan=plan_result, model_used=model_used,
    )

    await _write_audit(
        db, current_user.id, session_id, body.content,
        generated_query, output_format_val, AuditStatus.SUCCESS, None,
        int((time.monotonic() - start_time) * 1000),
    )

    import dataclasses as _dc
    stats_dict = _dc.asdict(stats) if stats is not None else None

    return MessageResponse(
        role=MessageRole.ASSISTANT,
        content=insight_text,
        download_token=compound_chart_token,
        pdf_download_token=compound_pdf_token,
        output_format=output_format,
        summary_stats=stats_dict,
        reasoning=plan_result.reasoning,
        generated_query=generated_query,
        target_tables=plan_result.target_tables,
        model_used=model_used,
        timestamp=datetime.now(UTC),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _append_assistant_turn(
    session_mgr: SessionManager,
    user_id: str,
    session_id: str,
    content: str,
    *,
    query_plan: QueryPlan | None = None,
    model_used: str | None = None,
) -> None:
    turn = ConversationTurn(
        role=MessageRole.ASSISTANT,
        content=content,
        timestamp=datetime.now(UTC),
        query_plan=query_plan,
        model_used=model_used,
    )
    await session_mgr.append_turn(user_id, session_id, turn)


async def _write_audit(
    db: AsyncSession,
    user_id,
    session_id: str,
    prompt_text: str,
    generated_query: str | None,
    output_format: str | None,
    audit_status: AuditStatus,
    error_message: str | None,
    duration_ms: int,
) -> None:
    try:
        audit = AuditLog(
            user_id=user_id,
            prompt_text=prompt_text,
            generated_query=generated_query,
            output_format=output_format,
            status=audit_status.value,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        db.add(audit)
        await db.commit()
    except Exception as exc:
        logger.error("Audit log write failed", error=str(exc))


def _turn_to_response(turn: ConversationTurn) -> MessageResponse:
    """Convert a ConversationTurn dataclass to a MessageResponse Pydantic model."""
    return MessageResponse(
        role=turn.role,
        content=turn.content,
        timestamp=turn.timestamp,
        download_token=turn.download_token,
        reasoning=turn.query_plan.reasoning if turn.query_plan else None,
        generated_query=turn.query_plan.query_string if turn.query_plan else None,
        target_tables=turn.query_plan.target_tables if turn.query_plan else None,
        model_used=turn.model_used,
    )
