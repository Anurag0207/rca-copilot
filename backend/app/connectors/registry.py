"""
Name-based connector registry.

Connector classes self-register with ``@register_connector("<type>")``. The set
of *enabled* connectors comes from the ``RCA_CONNECTORS`` setting, so adding a
source is: write a module in this package + add an entry to the env var.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Callable, TypeVar

import structlog

from app.connectors.base import ConnectorError, MCPConnectorBase

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)

_CONNECTOR_TYPES: dict[str, type[MCPConnectorBase]] = {}
_T = TypeVar("_T", bound=type[MCPConnectorBase])


def register_connector(type_name: str) -> Callable[[_T], _T]:
    """Class decorator that makes a connector selectable by ``type`` in RCA_CONNECTORS."""

    def decorator(cls: _T) -> _T:
        if type_name in _CONNECTOR_TYPES and _CONNECTOR_TYPES[type_name] is not cls:
            raise ValueError(f"Connector type {type_name!r} is already registered")
        cls.source_type = type_name
        _CONNECTOR_TYPES[type_name] = cls
        return cls

    return decorator


def registered_types() -> list[str]:
    load_builtin_connectors()
    return sorted(_CONNECTOR_TYPES)


def load_builtin_connectors() -> None:
    """Import every module in app.connectors so their decorators run."""
    import app.connectors as package

    for module in pkgutil.iter_modules(package.__path__):
        if module.name not in {"base", "registry"}:
            importlib.import_module(f"{package.__name__}.{module.name}")


class EvidenceRegistry:
    """Holds the enabled connector instances, keyed by their configured name."""

    def __init__(self, connectors: dict[str, MCPConnectorBase], config_errors: dict[str, str] | None = None) -> None:
        self._connectors = connectors
        #: entries that failed to build, name -> reason (surfaced by the health endpoint)
        self.config_errors = config_errors or {}

    @classmethod
    def from_settings(cls, settings: "Settings") -> "EvidenceRegistry":
        load_builtin_connectors()
        connectors: dict[str, MCPConnectorBase] = {}
        errors: dict[str, str] = {}

        for index, entry in enumerate(settings.RCA_CONNECTORS):
            name = str(entry.get("name") or f"entry-{index}")
            try:
                connectors[name] = cls._build(name, entry, settings)
            except (ConnectorError, KeyError, ValueError) as exc:
                logger.error("Connector config rejected", connector=name, error=str(exc))
                errors[name] = str(exc)
            else:
                logger.info("Connector enabled", connector=name, type=entry["type"])
        return cls(connectors, errors)

    @staticmethod
    def _build(name: str, entry: dict, settings: "Settings") -> MCPConnectorBase:
        if name in {"", None} or "type" not in entry:
            raise ValueError("each RCA_CONNECTORS entry needs 'name' and 'type'")
        connector_cls = _CONNECTOR_TYPES.get(entry["type"])
        if connector_cls is None:
            raise ValueError(f"unknown connector type {entry['type']!r}; registered: {sorted(_CONNECTOR_TYPES)}")
        options = dict(entry.get("options") or {})
        connector = connector_cls.from_config(name, settings, options)
        if "timeout_seconds" in options:
            connector.timeout_seconds = float(options["timeout_seconds"])
        return connector

    @property
    def names(self) -> list[str]:
        return list(self._connectors)

    def all(self) -> list[MCPConnectorBase]:
        return list(self._connectors.values())

    def get(self, name: str) -> MCPConnectorBase:
        try:
            return self._connectors[name]
        except KeyError:
            raise KeyError(f"No connector named {name!r}. Enabled: {self.names}") from None

    async def close_all(self) -> None:
        for connector in self._connectors.values():
            try:
                await connector.close()
            except Exception as exc:  # shutdown must not be blocked by one bad client
                logger.warning("Error closing connector", connector=connector.name, error=str(exc))
