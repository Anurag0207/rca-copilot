"""
Unit tests for IntentParser.

Tests:
  - Happy path: well-defined prompt maps to a non-clarification ParsedIntent
  - Vague prompt: LLM signals needs_clarification=True
  - Output format extraction: "give me a bar chart" → chart + bar
  - request_type classification: data_query vs structure_query
  - Schema summary rendering: compact format for prompt injection
  - History summary rendering: limited to last 6 turns
  - LLM response parsing: valid JSON, missing fields, malformed JSON
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.intent_parser import IntentParser
from app.llm.json_repair import LLMResponseParseError
from app.models.schemas import (
    ChartType,
    ColumnMeta,
    ConversationTurn,
    MessageRole,
    OutputFormat,
    ParsedIntent,
    RequestType,
    SchemaContext,
    TableMeta,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.OLLAMA_MODEL = "qwen2.5:7b"
    settings.OLLAMA_MAX_TOKENS = 2048
    return settings


@pytest.fixture
def mock_llm_provider() -> AsyncMock:
    """Mock LLMProviderBase — complete() returns a JSON string by default."""
    provider = AsyncMock()
    return provider


@pytest.fixture
def parser(mock_llm_provider: AsyncMock, mock_settings: MagicMock) -> IntentParser:
    return IntentParser(llm_provider=mock_llm_provider, settings=mock_settings)


@pytest.fixture
def sample_schema() -> SchemaContext:
    return SchemaContext(
        tables={
            "sales.transactions": TableMeta(
                name="transactions",
                database="sales",
                columns=[
                    ColumnMeta(name="id", data_type="BIGINT", nullable=False),
                    ColumnMeta(name="region", data_type="STRING", nullable=True),
                    ColumnMeta(name="amount", data_type="DOUBLE", nullable=True),
                    ColumnMeta(name="sale_date", data_type="DATE", nullable=True),
                ],
            )
        },
        fetched_at=datetime.utcnow(),
        connector_type="hive",
    )


@pytest.fixture
def empty_history() -> list[ConversationTurn]:
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSchemaAndHistorySummary:
    def test_schema_summary_contains_table_names(
        self, parser: IntentParser, sample_schema: SchemaContext
    ) -> None:
        # Bare name only (no "database." prefix) — see _build_schema_summary
        # docstring: the LLM must emit entity names the connector understands.
        summary = parser._build_schema_summary(sample_schema)
        assert "transactions" in summary

    def test_schema_summary_contains_column_types(
        self, parser: IntentParser, sample_schema: SchemaContext
    ) -> None:
        summary = parser._build_schema_summary(sample_schema)
        assert "region" in summary
        assert "STRING" in summary

    def test_empty_schema_returns_placeholder(self, parser: IntentParser) -> None:
        empty_schema = SchemaContext(tables={}, fetched_at=datetime.utcnow(), connector_type="hive")
        summary = parser._build_schema_summary(empty_schema)
        assert "no schema" in summary.lower()

    def test_history_summary_limits_to_six_turns(self, parser: IntentParser) -> None:
        turns = [
            ConversationTurn(role=MessageRole.USER, content=f"message {i}", timestamp=datetime.utcnow())
            for i in range(10)
        ]
        summary = parser._build_history_summary(turns)
        # Should only include last 6 turns (messages 4-9)
        assert "message 4" in summary
        assert "message 0" not in summary

    def test_empty_history_returns_placeholder(self, parser: IntentParser) -> None:
        summary = parser._build_history_summary([])
        assert "no prior" in summary.lower()


class TestLLMResponseParsing:
    def test_parse_valid_intent_response(self, parser: IntentParser) -> None:
        raw = """{
            "request_type": "data_query",
            "entities": ["transactions"],
            "metrics": ["SUM(amount)"],
            "filters": {"region": "West"},
            "time_range": "last quarter",
            "output_format": "chart",
            "chart_type": "bar",
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = parser._parse_llm_response(raw)
        assert intent.entities == ["transactions"]
        assert intent.output_format == OutputFormat.CHART
        assert intent.chart_type == ChartType.BAR
        assert intent.needs_clarification is False
        assert intent.request_type == RequestType.DATA_QUERY

    def test_parse_structure_query_response(self, parser: IntentParser) -> None:
        raw = """{
            "request_type": "structure_query",
            "entities": ["transactions", "users"],
            "metrics": [],
            "filters": {},
            "time_range": null,
            "output_format": "chart",
            "chart_type": null,
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = parser._parse_llm_response(raw)
        assert intent.request_type == RequestType.STRUCTURE_QUERY
        assert "transactions" in intent.entities
        assert intent.needs_clarification is False

    def test_parse_clarification_response(self, parser: IntentParser) -> None:
        raw = """{
            "request_type": "data_query",
            "entities": [],
            "metrics": [],
            "filters": {},
            "time_range": null,
            "output_format": null,
            "chart_type": null,
            "needs_clarification": true,
            "clarification_question": "Which dataset and time period?"
        }"""
        intent = parser._parse_llm_response(raw)
        assert intent.needs_clarification is True
        assert "dataset" in intent.clarification_question.lower()

    def test_parse_malformed_json_raises_value_error(self, parser: IntentParser) -> None:
        with pytest.raises(LLMResponseParseError):
            parser._parse_llm_response("not json at all")

    def test_parse_null_output_format(self, parser: IntentParser) -> None:
        raw = """{
            "request_type": "data_query",
            "entities": ["sales"],
            "metrics": ["revenue"],
            "filters": {},
            "time_range": null,
            "output_format": null,
            "chart_type": null,
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = parser._parse_llm_response(raw)
        assert intent.output_format is None

    def test_parse_missing_request_type_defaults_to_data_query(
        self, parser: IntentParser
    ) -> None:
        raw = """{
            "entities": ["sales"],
            "metrics": ["revenue"],
            "filters": {},
            "time_range": null,
            "output_format": null,
            "chart_type": null,
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = parser._parse_llm_response(raw)
        assert intent.request_type == RequestType.DATA_QUERY

    def test_parse_unknown_request_type_defaults_to_data_query(
        self, parser: IntentParser
    ) -> None:
        raw = """{
            "request_type": "unknown_type",
            "entities": [],
            "metrics": [],
            "filters": {},
            "time_range": null,
            "output_format": null,
            "chart_type": null,
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = parser._parse_llm_response(raw)
        assert intent.request_type == RequestType.DATA_QUERY


class TestParsePipeline:
    @pytest.mark.asyncio
    async def test_parse_calls_ollama_provider(
        self,
        parser: IntentParser,
        mock_llm_provider: AsyncMock,
        sample_schema: SchemaContext,
        empty_history: list[ConversationTurn],
    ) -> None:
        """Verify parse() calls llm_provider.complete() and returns a ParsedIntent."""
        mock_llm_provider.complete.return_value = """{
            "request_type": "data_query",
            "entities": ["transactions"],
            "metrics": ["SUM(amount)"],
            "filters": {"region": "West"},
            "time_range": null,
            "output_format": "chart",
            "chart_type": "bar",
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = await parser.parse("Show total sales by region", empty_history, sample_schema)
        assert mock_llm_provider.complete.called
        assert isinstance(intent, ParsedIntent)
        assert intent.needs_clarification is False
        assert intent.request_type == RequestType.DATA_QUERY

    @pytest.mark.asyncio
    async def test_parse_structure_query(
        self,
        parser: IntentParser,
        mock_llm_provider: AsyncMock,
        sample_schema: SchemaContext,
        empty_history: list[ConversationTurn],
    ) -> None:
        """Verify parse() returns STRUCTURE_QUERY when LLM classifies it as such."""
        mock_llm_provider.complete.return_value = """{
            "request_type": "structure_query",
            "entities": [],
            "metrics": [],
            "filters": {},
            "time_range": null,
            "output_format": "chart",
            "chart_type": null,
            "needs_clarification": false,
            "clarification_question": null
        }"""
        intent = await parser.parse("Show me an ER diagram", empty_history, sample_schema)
        assert intent.request_type == RequestType.STRUCTURE_QUERY

    @pytest.mark.asyncio
    async def test_parse_retries_on_json_error(
        self,
        parser: IntentParser,
        mock_llm_provider: AsyncMock,
        sample_schema: SchemaContext,
        empty_history: list[ConversationTurn],
    ) -> None:
        """Verify parse() retries once on a non-JSON response, then raises RuntimeError."""
        mock_llm_provider.complete.return_value = "not valid json"
        with pytest.raises(RuntimeError, match="failed after 2 attempts"):
            await parser.parse("Show sales", empty_history, sample_schema)
        assert mock_llm_provider.complete.call_count == 2
