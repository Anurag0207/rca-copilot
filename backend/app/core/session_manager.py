"""
Session manager — CRUD for multi-turn conversation state in Redis.

Each active session is stored as a JSON-serialised SessionState under the key:
    session:{user_id}:{session_id}

A 24-hour sliding TTL is maintained by re-issuing EXPIRE on every write.
Download tokens registered within a session share the same expiry.

This module never reads from or writes to PostgreSQL. The session_records table
in PostgreSQL tracks metadata only (IDs, timestamps, turn counts) — see api/routes/chat.py
for where those writes happen.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.models.schemas import (
    ConversationTurn,
    DownloadTokenMeta,
    MessageRole,
    OutputFormat,
    QueryPlan,
    SessionState,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

SESSION_KEY_PATTERN: str = "session:{user_id}:{session_id}"


class SessionManager:
    """
    Manages conversation session state in Redis.

    Key pattern: ``session:{user_id}:{session_id}``
    TTL:         24 hours, sliding (reset on every write)
    """

    SESSION_TTL_SECONDS: int = 86400  # 24 hours

    def __init__(self, redis_client: "Redis") -> None:
        self._redis = redis_client

    # ── Session lifecycle ────────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: str,
        connection_id: str,
    ) -> SessionState:
        """
        Create a new empty session and persist it to Redis.

        Args:
            user_id:       Authenticated user's UUID string.
            connection_id: MCP connection identifier for this session.

        Returns:
            Fresh SessionState with empty history.
        """
        session_id = f"ses_{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        state = SessionState(
            session_id=session_id,
            user_id=user_id,
            connection_id=connection_id,
            history=[],
            download_tokens={},
            created_at=now,
            last_activity=now,
        )
        await self.save_session(state)
        logger.info("Session created", user_id=user_id, session_id=session_id)
        return state

    async def get_session(
        self,
        user_id: str,
        session_id: str,
    ) -> SessionState | None:
        """
        Load a session from Redis.

        Returns:
            Deserialised SessionState or None if not found / expired.
        """
        key = self._make_key(user_id, session_id)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        return self._deserialise(raw)

    async def save_session(self, state: SessionState) -> None:
        """
        Serialise and persist a SessionState; resets the sliding 24-h TTL.

        Must be called after every mutation (append_turn, register_download_token).
        """
        state.last_activity = datetime.now(UTC)
        key = self._make_key(state.user_id, state.session_id)
        payload = self._serialise(state)
        await self._redis.set(key, payload, ex=self.SESSION_TTL_SECONDS)

    async def session_exists(self, user_id: str, session_id: str) -> bool:
        """Return True if the session key exists in Redis (not expired)."""
        key = self._make_key(user_id, session_id)
        return bool(await self._redis.exists(key))

    async def delete_session(self, user_id: str, session_id: str) -> None:
        """
        Delete the session key from Redis.

        Deleting the key also causes all registered download tokens to become
        invalid; a background cleanup task should remove the associated files.
        """
        key = self._make_key(user_id, session_id)
        await self._redis.delete(key)
        logger.info("Session deleted", user_id=user_id, session_id=session_id)

    # ── Conversation history ─────────────────────────────────────────────────

    async def append_turn(
        self,
        user_id: str,
        session_id: str,
        turn: ConversationTurn,
    ) -> None:
        """
        Append a ConversationTurn to the session history and persist.

        Args:
            user_id:    Session owner.
            session_id: Target session.
            turn:       The new turn to append (user or assistant).
        """
        state = await self.get_session(user_id, session_id)
        if state is None:
            raise KeyError(f"Session not found: {session_id}")
        state.history.append(turn)
        await self.save_session(state)

    # ── Download token registry ───────────────────────────────────────────────

    async def register_download_token(
        self,
        user_id: str,
        session_id: str,
        token: str,
        meta: DownloadTokenMeta,
    ) -> None:
        """
        Store a download token and its file metadata in the session.

        Args:
            user_id:    Session owner.
            session_id: Target session.
            token:      UUID token string (key in download_tokens dict).
            meta:       DownloadTokenMeta describing the file.
        """
        state = await self.get_session(user_id, session_id)
        if state is None:
            raise KeyError(f"Session not found: {session_id}")
        state.download_tokens[token] = meta
        await self.save_session(state)

    async def get_download_token(
        self,
        user_id: str,
        session_id: str,
        token: str,
    ) -> DownloadTokenMeta | None:
        """
        Retrieve download token metadata.

        Returns:
            DownloadTokenMeta if token exists in the session; None otherwise.
        """
        state = await self.get_session(user_id, session_id)
        if state is None:
            return None
        return state.download_tokens.get(token)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_key(self, user_id: str, session_id: str) -> str:
        """Return the Redis key for a session: ``session:{user_id}:{session_id}``."""
        return SESSION_KEY_PATTERN.format(user_id=user_id, session_id=session_id)

    def _serialise(self, state: SessionState) -> str:
        """Convert SessionState to a JSON string for Redis storage."""

        def _default(obj: Any) -> Any:
            if isinstance(obj, datetime):
                return obj.isoformat()
            if hasattr(obj, "value"):
                return obj.value
            raise TypeError(f"Cannot serialise {type(obj)!r}")

        def _turn_to_dict(turn: ConversationTurn) -> dict:
            d: dict[str, Any] = {
                "role": turn.role.value,
                "content": turn.content,
                "timestamp": turn.timestamp.isoformat(),
                "download_token": turn.download_token,
                "model_used": turn.model_used,
            }
            if turn.query_plan is not None:
                d["query_plan"] = {
                    "query_string": turn.query_plan.query_string,
                    "target_tables": turn.query_plan.target_tables,
                    "estimated_complexity": turn.query_plan.estimated_complexity,
                    "parameters": turn.query_plan.parameters,
                    "reasoning": turn.query_plan.reasoning,
                }
            else:
                d["query_plan"] = None
            return d

        def _token_meta_to_dict(meta: DownloadTokenMeta) -> dict:
            return {
                "file_path": meta.file_path,
                "output_format": meta.output_format.value,
                "filename": meta.filename,
                "expires_at": meta.expires_at.isoformat(),
            }

        payload = {
            "session_id": state.session_id,
            "user_id": state.user_id,
            "connection_id": state.connection_id,
            "history": [_turn_to_dict(t) for t in state.history],
            "download_tokens": {
                k: _token_meta_to_dict(v) for k, v in state.download_tokens.items()
            },
            "created_at": state.created_at.isoformat(),
            "last_activity": state.last_activity.isoformat(),
        }
        return json.dumps(payload)

    def _deserialise(self, raw: str | bytes) -> SessionState:
        """Reconstruct a SessionState from a JSON string retrieved from Redis."""
        data = json.loads(raw)

        history: list[ConversationTurn] = []
        for t in data.get("history", []):
            qp = None
            if t.get("query_plan"):
                qp = QueryPlan(
                    query_string=t["query_plan"]["query_string"],
                    target_tables=t["query_plan"]["target_tables"],
                    estimated_complexity=t["query_plan"]["estimated_complexity"],
                    parameters=t["query_plan"].get("parameters", {}),
                    reasoning=t["query_plan"].get("reasoning"),
                )
            history.append(
                ConversationTurn(
                    role=MessageRole(t["role"]),
                    content=t["content"],
                    timestamp=datetime.fromisoformat(t["timestamp"]),
                    query_plan=qp,
                    download_token=t.get("download_token"),
                    model_used=t.get("model_used"),
                )
            )

        download_tokens: dict[str, DownloadTokenMeta] = {}
        for k, v in data.get("download_tokens", {}).items():
            download_tokens[k] = DownloadTokenMeta(
                file_path=v["file_path"],
                output_format=OutputFormat(v["output_format"]),
                filename=v["filename"],
                expires_at=datetime.fromisoformat(v["expires_at"]),
            )

        return SessionState(
            session_id=data["session_id"],
            user_id=data["user_id"],
            connection_id=data["connection_id"],
            history=history,
            download_tokens=download_tokens,
            created_at=datetime.fromisoformat(data["created_at"]),
            last_activity=datetime.fromisoformat(data["last_activity"]),
        )
