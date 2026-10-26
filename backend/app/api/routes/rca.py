"""RCA Copilot endpoints."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user, get_rca_engine
from app.connectors.base import ConnectorHealth
from app.models.db import User
from app.rca.engine import RCAEngine
from app.rca.models import Incident, RCAResult

logger = structlog.get_logger(__name__)
router = APIRouter()


@router.post("/analyze", response_model=RCAResult, summary="Run a root cause analysis for an incident")
async def analyze_incident(
    incident: Incident,
    engine: RCAEngine = Depends(get_rca_engine),
    user: User = Depends(get_current_user),
) -> RCAResult:
    logger.info("RCA requested", user_id=str(user.id), service=incident.service)
    try:
        return await engine.analyze(incident)
    except KeyError as exc:  # unknown connector name in incident.connectors
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc.args[0])) from exc


@router.get(
    "/connectors/health",
    response_model=list[ConnectorHealth],
    summary="Health of every enabled evidence source",
)
async def connectors_health(
    engine: RCAEngine = Depends(get_rca_engine),
    user: User = Depends(get_current_user),
) -> list[ConnectorHealth]:
    return await engine.connector_health()
