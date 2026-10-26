"""
Abstract LLM provider interface.

Decouples the orchestration layer (IntentParser, QueryPlanner, MermaidGenerator)
from any specific LLM vendor. Every concrete provider must implement:

  complete()     — text-in, text-out (intent parsing, query generation, diagram generation)
  list_models()  — enumerate models this provider currently has available

Current implementations:
  OllamaProvider — uses Ollama's OpenAI-compatible REST API (localhost:11434)

Adding a new provider:
  1. Create backend/app/llm/{name}_provider.py
  2. Subclass LLMProviderBase and implement complete() and list_models()
  3. Add a branch in get_llm_provider() in api/deps.py
  4. Add an entry to the static provider list in api/routes/chat.py::list_providers()
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProviderBase(ABC):
    """
    Minimal interface all LLM providers must satisfy.

    Returns the raw text response from the model so that callers
    (IntentParser, QueryPlanner, MermaidGenerator) can apply their own parsing.
    """

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> str:
        """
        Send a list of chat messages and return the model's text reply.

        Args:
            messages:   OpenAI-format message list, e.g.
                        [{"role": "user", "content": "..."}]
            max_tokens: Maximum tokens in the response.
            model:      Override the provider's configured default model for
                        this call only. None uses the provider's default.

        Returns:
            Plain text response string from the model.
        """

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return the model ids currently available from this provider."""


class ScopedLLMProvider(LLMProviderBase):
    """
    Thin wrapper that pins a base provider to a fixed model for one request.

    Lets a single user-selected model_id (e.g. from a per-message model picker)
    flow through IntentParser / QueryPlanner / InsightGenerator / MermaidGenerator
    without changing any of their constructors or call sites — they only ever
    see an LLMProviderBase and call complete() the same way regardless of
    whether it's the shared default provider or one of these scoped wrappers.
    """

    def __init__(self, base: LLMProviderBase, model: str) -> None:
        self._base = base
        self._model = model

    async def complete(
        self,
        messages: list[dict],
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> str:
        return await self._base.complete(messages, max_tokens=max_tokens, model=model or self._model)

    async def list_models(self) -> list[str]:
        return await self._base.list_models()
