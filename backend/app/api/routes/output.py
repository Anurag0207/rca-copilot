"""
Output download route — /output

Endpoints:
  GET /output/{download_token}

Serves generated output files (PNG charts, PDFs, PowerPoint decks) to
authenticated users.

Token format: ``{session_id}:{uuid}`` (compound token)
  - The session_id prefix lets output.py look up the session directly without
    scanning all active sessions for the requesting user.
  - The uuid suffix is the key in session.download_tokens.
  - Ownership is enforced by looking up the session under the requesting
    user's user_id — a different user's token returns 404.

Security model:
  - The requesting user's JWT user_id must match the session owner.
  - The token lives as long as the session (up to 24 h).
  - Files are served via FastAPI's FileResponse which streams the file
    directly from the local temp store.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from app.api.deps import get_current_user, get_session_manager
from app.core.session_manager import SessionManager
from app.models.db import User

logger = structlog.get_logger(__name__)

router = APIRouter()

# Map output format to MIME type
_MIME_TYPES: dict[str, str] = {
    "chart": "image/png",
    "pdf":   "application/pdf",
    "ppt":   "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@router.get(
    "/{download_token}",
    summary="Download a generated output file",
    response_class=FileResponse,
)
async def download_output(
    download_token: str,
    current_user: User = Depends(get_current_user),
    session_mgr: SessionManager = Depends(get_session_manager),
) -> FileResponse:
    """
    Authenticate the download_token, validate ownership, and stream the file.

    The token must be in the compound format ``{session_id}:{uuid}`` as
    issued by the chat pipeline. Only the session owner (matched by JWT
    user_id) can download the file.

    Args:
        download_token: Compound token from MessageResponse.download_token.
        current_user:   Authenticated user (from JWT).
        session_mgr:    SessionManager for token lookup.

    Returns:
        FileResponse streaming the generated file with appropriate Content-Type.

    Raises:
        HTTPException(400): Malformed token (not compound format).
        HTTPException(404): Token not found in the session, or session expired.
        HTTPException(410): Token exists but the file has been deleted from disk.
    """
    # Parse compound token: "{session_id}:{uuid}"
    parts = download_token.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed download token",
        )
    session_id, token_uuid = parts
    user_id = str(current_user.id)

    meta = await session_mgr.get_download_token(user_id, session_id, token_uuid)
    if meta is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Download token not found or session expired",
        )

    file_path = Path(meta.file_path)
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="File has been deleted",
        )

    media_type = _MIME_TYPES.get(meta.output_format.value, "application/octet-stream")

    logger.info(
        "File download",
        user_id=user_id,
        session_id=session_id,
        filename=meta.filename,
    )

    return FileResponse(
        path=str(file_path),
        filename=meta.filename,
        media_type=media_type,
    )
