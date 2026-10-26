"""
Unit tests for QueryPlanner.

Tests:
  - _validate_query_is_read_only: blocks INSERT, UPDATE, DELETE, DROP, etc.
  - _parse_llm_response: QueryPlan on success, ClarificationRequest on ambiguity
  - _build_schema_summary: correct table/column format for prompt injection
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.query_planner import QueryPlanner
from app.llm.json_repair import LLMResponseParseError
from app.models.schemas import (
    ClarificationRequest,
    ColumnMeta,
    QueryPlan,
    SchemaContext,
    TableMeta,
)


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.OLLAMA_MODEL = "qwen2.5:7b"
    settings.OLLAMA_MAX_TOKENS = 2048
    return settings


@pytest.fixture
def mock_llm_provider() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def planner(mock_llm_provider: AsyncMock, mock_settings: MagicMock) -> QueryPlanner:
    return QueryPlanner(llm_provider=mock_llm_provider, settings=mock_settings)


@pytest.fixture
def sample_schema() -> SchemaContext:
    return SchemaContext(
        tables={
            "sales.transactions": TableMeta(
                name="transactions",
                database="sales",
                columns=[
                    ColumnMeta(
                        name="region", data_type="STRING", nullable=True,
                        sample_values=["North", "South"],
                    ),
                    ColumnMeta(name="amount", data_type="DOUBLE", nullable=True),
                ],
            )
        },
        fetched_at=datetime.utcnow(),
        connector_type="hive",
    )


class TestReadOnlyValidation:
    @pytest.mark.parametrize("query", [
        "SELECT region, SUM(amount) FROM sales.transactions GROUP BY region",
        "SELECT * FROM sales.transactions WHERE region = 'West'",
        "SELECT COUNT(*) FROM sales.transactions",
    ])
    def test_valid_select_queries(self, query: str) -> None:
        assert QueryPlanner._validate_query_is_read_only(query) is True

    @pytest.mark.parametrize("query", [
        "INSERT INTO sales.transactions VALUES (1, 'West', 100.0)",
        "UPDATE sales.transactions SET amount = 200 WHERE id = 1",
        "DELETE FROM sales.transactions WHERE id = 1",
        "DROP TABLE sales.transactions",
        "CREATE TABLE foo AS SELECT * FROM bar",
        "ALTER TABLE foo ADD COLUMN baz STRING",
        "TRUNCATE TABLE sales.transactions",
    ])
    def test_forbidden_statements(self, query: str) -> None:
        assert QueryPlanner._validate_query_is_read_only(query) is False


class TestLLMResponseParsing:
    def test_parse_successful_query_plan(self, planner: QueryPlanner) -> None:
        raw = """{
            "query_string": "SELECT region, SUM(amount) FROM sales.transactions GROUP BY region",
            "target_tables": ["sales.transactions"],
            "estimated_complexity": "low",
            "parameters": {},
            "needs_clarification": false,
            "clarification_question": null
        }"""
        result = planner._parse_llm_response(raw)
        assert isinstance(result, QueryPlan)
        assert "SELECT" in result.query_string
        assert result.estimated_complexity == "low"

    def test_parse_clarification_request(self, planner: QueryPlanner) -> None:
        raw = """{
            "query_string": null,
            "target_tables": [],
            "estimated_complexity": "low",
            "parameters": {},
            "needs_clarification": true,
            "clarification_question": "Which time period should I filter by?"
        }"""
        result = planner._parse_llm_response(raw)
        assert isinstance(result, ClarificationRequest)
        assert "time period" in result.question.lower()

    def test_parse_malformed_json_raises(self, planner: QueryPlanner) -> None:
        with pytest.raises(LLMResponseParseError):
            planner._parse_llm_response("{bad json")

    def test_parse_null_query_without_clarification_raises(self, planner: QueryPlanner) -> None:
        raw = """{
            "query_string": null,
            "target_tables": [],
            "estimated_complexity": "low",
            "parameters": {},
            "needs_clarification": false,
            "clarification_question": null
        }"""
        with pytest.raises(LLMResponseParseError, match="null query_string"):
            planner._parse_llm_response(raw)


class TestSchemaSummary:
    def test_schema_summary_includes_table_and_columns(
        self, planner: QueryPlanner, sample_schema: SchemaContext
    ) -> None:
        # Bare collection name only — query_planner's Mongo prompt requires
        # "BARE collection names (no database prefix)" since $lookup.from
        # can't be qualified, so the summary must not qualify it either.
        # data_type itself isn't rendered — sample values are what the LLM
        # actually reasons from (Mongo has no fixed schema to name-check).
        summary = planner._build_schema_summary(sample_schema)
        assert "transactions" in summary
        assert "region" in summary
        assert "North" in summary

    def test_empty_schema_returns_placeholder(self, planner: QueryPlanner) -> None:
        empty = SchemaContext(tables={}, fetched_at=datetime.utcnow(), connector_type="hive")
        summary = planner._build_schema_summary(empty)
        assert "empty" in summary.lower()
