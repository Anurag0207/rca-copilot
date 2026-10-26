"""
Ollama LLM provider — wraps Ollama's OpenAI-compatible REST API.

Ollama exposes GET/POST endpoints at http://{host}:11434/v1 that mirror the
OpenAI chat completions API exactly. The `openai` Python library is reused
unchanged — only base_url and api_key differ.

A single model (OLLAMA_MODEL, default qwen2.5:7b) handles all tasks:
  - intent parsing
  - HQL/SQL generation
  - Mermaid diagram syntax generation

This model runs on CPU (no GPU required), making it suitable for PoC deployments
without NVIDIA hardware.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from openai import AsyncOpenAI

from app.llm.base import LLMProviderBase

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)


class OllamaProvider(LLMProviderBase):
    """
    Concrete LLM provider backed by a local Ollama instance.

    Uses the openai Python SDK pointed at Ollama's OpenAI-compatible endpoint.
    The api_key value is required by the SDK but ignored by Ollama.
    """

    def __init__(self, settings: "Settings") -> None:
        base_url = settings.OLLAMA_BASE_URL.rstrip("/") + "/v1"
        self._model = settings.OLLAMA_MODEL

        # Single AsyncOpenAI client — Ollama accepts any api_key value
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key="ollama",
        )

        logger.info(
            "OllamaProvider initialised",
            base_url=base_url,
            model=self._model,
        )

    async def complete(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> str:
        """
        Send a text chat request to Ollama and return the response content.

        Args:
            messages:   OpenAI-format message list.
            max_tokens: Token limit for the response.
            model:      Override OLLAMA_MODEL for this call only (e.g. a model
                        the user picked in the UI). None uses the configured default.

        Returns:
            Model's text reply as a plain string.

        Raises:
            RuntimeError: On Ollama API or network failure.
        """
        effective_model = model or self._model
        try:
            response = await self._client.chat.completions.create(
                model=effective_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.1,    # low temperature → more deterministic JSON output
            )
            content = response.choices[0].message.content or ""
            logger.debug(
                "Ollama completion",
                model=effective_model,
                tokens=response.usage.total_tokens if response.usage else None,
            )
            return content
        except Exception as exc:
            logger.error("Ollama complete() failed", model=effective_model, error=str(exc))
            raise RuntimeError(f"Ollama completion failed: {exc}") from exc

    async def list_models(self) -> list[str]:
        """
        Return model ids currently pulled and available in this Ollama instance.

        Hits Ollama's OpenAI-compatible GET /v1/models endpoint (the same
        client already configured for chat completions).

        Raises:
            RuntimeError: On Ollama API or network failure.
        """
        try:
            response = await self._client.models.list()
            return sorted(m.id for m in response.data)
        except Exception as exc:
            logger.error("Ollama list_models() failed", error=str(exc))
            raise RuntimeError(f"Ollama list_models failed: {exc}") from exc
