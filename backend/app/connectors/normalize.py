"""Shared helpers that turn raw source rows into EvidenceRecord."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.connectors.base import EvidenceRecord, to_utc


@dataclass(frozen=True)
class FieldMap:
    """Which raw field holds which EvidenceRecord attribute. Dotted paths are supported."""

    timestamp: str = "timestamp"
    message: str = "message"
    level: str = "level"
    service: str = "service"


def get_path(raw: dict[str, Any], path: str) -> Any:
    """Look up ``a.b.c`` in nested dicts, tolerating flat keys that contain dots."""
    if path in raw:
        return raw[path]
    current: Any = raw
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return to_utc(value)
    if isinstance(value, (int, float)):
        # epoch milliseconds are the common case for ES/Hive exports; seconds otherwise
        seconds = value / 1000 if value > 1e11 else value
        return to_utc(datetime.utcfromtimestamp(seconds))
    if isinstance(value, str) and value:
        return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def build_record(source: str, seq: int, raw: dict[str, Any], fields: FieldMap) -> EvidenceRecord | None:
    """
    Normalise one raw row. Returns None when the row has no usable timestamp,
    since an event we cannot place on the timeline is not usable evidence.
    """
    try:
        timestamp = parse_timestamp(get_path(raw, fields.timestamp))
    except ValueError:
        return None
    if timestamp is None:
        return None

    level = get_path(raw, fields.level)
    service = get_path(raw, fields.service)
    message = get_path(raw, fields.message)
    known = {fields.timestamp, fields.message, fields.level, fields.service}
    return EvidenceRecord(
        id=f"{source}#{seq}",
        timestamp=timestamp,
        source=source,
        service=str(service) if service is not None else None,
        level=str(level).lower() if level is not None else None,
        message=str(message) if message is not None else "",
        attributes={k: v for k, v in raw.items() if k not in known and k != "_id"},
    )
