"""
Intent parser — extracts structured intent from a natural-language prompt.

Uses a single local LLM call (via OllamaProvider) to determine:
  - Which warehouse entities/tables the user is asking about
  - Which metrics / columns they want
  - Any filters or time ranges mentioned
  - The preferred output format (chart / pdf / ppt), if stated
  - Whether the prompt is too vague to act on (needs_clarification)

The prompt template (INTENT_EXTRACTION_PROMPT) is an editable module-level
constant — modify it here to tune LLM behaviour without touching parsing logic.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from app.llm.base import LLMProviderBase
from app.llm.json_repair import LLMResponseParseError, escape_literal_newlines, extract_json_object
from app.models.schemas import (
    ChartType,
    ConversationTurn,
    OutputFormat,
    ParsedIntent,
    RequestType,
    SchemaContext,
)

if TYPE_CHECKING:
    from app.config import Settings

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Editable prompt template — modify to tune intent extraction behaviour
# ─────────────────────────────────────────────────────────────────────────────

INTENT_EXTRACTION_PROMPT: str = """\
You are a data analyst assistant. Your job is to understand a user's
natural-language query and extract a structured intent from it, given the
warehouse schema and conversation history provided below.

## Warehouse Schema
{schema_summary}

## Conversation History (oldest first)
{history_summary}

## Current User Query
{user_prompt}

## Instructions
Determine request_type — choose EXACTLY ONE:

"data_query"
  *** USE THIS whenever the answer requires actually running a query against the database. ***
  This includes: aggregations, counts, sums, averages, rankings, filters, charts,
  and ANY question asking for a number or statistic from the data.

  Examples:
  - "Show total revenue by payment type"         → data_query (aggregation)
  - "Top 10 products by number of orders"        → data_query (ranking)
  - "How many rows do you have?"                 → data_query (COUNT)
  - "How many unique customers?"                 → data_query (COUNT DISTINCT)
  - "How many values do you see?"                → data_query (COUNT)
  - "What is the average review score?"          → data_query (AVG aggregation)
  - "How many orders were placed last month?"    → data_query (COUNT with filter)
  - "What is the total number of records?"       → data_query (COUNT)
  - "Give me the count of sellers"               → data_query (COUNT)
  - "Show me the data"                           → data_query (fetch sample)

  For data_query: set metrics to the aggregation needed (e.g. ["count", "total_revenue"]).
  If no output_format is specified, set it to "chart" and chart_type to "table"
  for count/single-number queries, or "bar" for grouped aggregations.

"describe_query"
  Use ONLY when the user is asking for a text explanation with NO numbers needed
  from the database. This means questions about what the data represents conceptually,
  what a field name means, or what topics the dataset covers — questions that can be
  answered from the schema definition alone.

  Examples (ONLY these qualify):
  - "Tell me what this dataset is about"         → describe_query
  - "What does the order_status field mean?"     → describe_query
  - "Explain what olist_sellers_dataset contains"→ describe_query
  - "Give me an overview of the data model"      → describe_query

  NOT describe_query (use data_query instead):
  - "How many X?" / "What is the count of X?"   → data_query
  - "How many values do you see?"                → data_query
  - "How many rows are there?"                   → data_query
  - "What is the total/average/sum of X?"        → data_query

"structure_query"
  Use ONLY when the user explicitly asks for a visual diagram, ER diagram,
  flowchart, or graph of how tables/collections relate.
  Must include words like: "diagram", "draw", "visualise", "ER", "flowchart", "graph".

  Examples:
  - "Draw an ER diagram of the schema"           → structure_query
  - "Show me how orders relate to customers visually" → structure_query

Rules:
- When in doubt between describe_query and data_query, ALWAYS choose data_query.
- For data_query: identify entities (collection names), metrics (what to compute),
  filters, time ranges. Set output_format to "chart" if not specified.
- For describe_query: set metrics/filters/time_range to empty/null.
- For structure_query: set output_format to "chart", chart_type to null.
- If the user's intent is ambiguous and cannot be acted on, set needs_clarification=true.

Respond ONLY with a valid JSON object — no prose, no markdown fences:
{{
  "request_type": "<data_query|describe_query|structure_query>",
  "entities": ["<collection or business entity>"],
  "metrics": ["<aggregation or column>"],
  "filters": {{"<field>": "<value>"}},
  "time_range": "<string or null>",
  "output_format": "<chart|pdf|ppt|null>",
  "chart_type": "<bar|line|pie|table|null>",
  "needs_clarification": <true|false>,
  "clarification_question": "<string or null>"
}}
"""


def _safe_enum(enum_cls: type, value: Any) -> Any | None:
    """
    Convert a raw LLM-provided value to *enum_cls*, tolerating common small-model
    JSON mistakes — returns None instead of raising for any value that doesn't
    map to a real member (including the literal string "null", which qwen2.5:7b
    occasionally emits in place of JSON's bare null).
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("null", "none", ""):
        return None
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning(f"Ignoring invalid {enum_cls.__name__} value from LLM", value=value)
        return None


class IntentParser:
    """
    Extracts a ParsedIntent from a user's NL prompt via a local LLM call.

    The schema is injected into the prompt as a compact text summary so the
    LLM can ground its entity extraction in the actual warehouse structure.
    Uses OllamaProvider (qwen2.5:14b by default) — no cloud API calls.
    """

    def __init__(self, llm_provider: LLMProviderBase, settings: "Settings") -> None:
        self._llm = llm_provider
        self._settings = settings

    async def parse(
        self,
        user_prompt: str,
        conversation_history: list[ConversationTurn],
        schema_context: SchemaContext,
    ) -> ParsedIntent:
        """
        Call the local LLM to extract structured intent from *user_prompt*.

        Args:
            user_prompt:           The raw NL input from the user.
            conversation_history:  Prior turns in this session (for context).
            schema_context:        Current warehouse schema snapshot.

        Returns:
            ParsedIntent dataclass. If needs_clarification is True, no query
            should be generated — surface clarification_question to the user.

        Raises:
            RuntimeError: If the LLM returns unparseable output after retries.
        """
        schema_summary  = self._build_schema_summary(schema_context)
        history_summary = self._build_history_summary(conversation_history)

        prompt = INTENT_EXTRACTION_PROMPT.format(
            schema_summary=schema_summary,
            history_summary=history_summary,
            user_prompt=user_prompt,
        )

        # Retry once on parse failure — local models occasionally output
        # prose before the JSON when the context is complex
        for attempt in range(2):
            try:
                raw = await self._llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self._settings.OLLAMA_MAX_TOKENS,
                )
                intent = self._parse_llm_response(raw)
                intent.user_question = user_prompt   # preserve raw text for query planner
                logger.info(
                    "Intent classified",
                    request_type=intent.request_type.value,
                    entities=intent.entities,
                    metrics=intent.metrics,
                    filters=intent.filters,
                    output_format=intent.output_format.value if intent.output_format else None,
                    chart_type=intent.chart_type.value if intent.chart_type else None,
                    needs_clarification=intent.needs_clarification,
                )
                return intent
            except LLMResponseParseError as exc:
                if attempt == 0:
                    logger.warning("IntentParser: retrying after parse failure", error=str(exc))
                    continue
                raise RuntimeError(f"IntentParser failed after 2 attempts: {exc}") from exc

        raise RuntimeError("IntentParser: unreachable")

    def _build_schema_summary(self, schema: SchemaContext) -> str:
        """
        Render SchemaContext as a compact plain-text summary for prompt injection.

        Uses only the bare collection/table name (not the qualified database.name)
        so the LLM generates entity names that match exactly what the connector expects.
        """
        lines: list[str] = []
        for _, table in schema.tables.items():
            col_summary = ", ".join(
                f"{c.name}: {c.data_type}" for c in table.columns
            )
            lines.append(f"{table.name} ({col_summary})")
        return "\n".join(lines) if lines else "(no schema available)"

    def _build_history_summary(self, history: list[ConversationTurn]) -> str:
        """
        Render conversation history as a numbered list for prompt injection.

        Only includes the last 6 turns to stay within token budget.
        """
        # TODO: fine-tune the number of turns included
        recent = history[-6:]
        if not recent:
            return "(no prior conversation)"
        return "\n".join(
            f"{i + 1}. [{turn.role.value.upper()}] {turn.content}"
            for i, turn in enumerate(recent)
        )

    def _parse_llm_response(self, raw: str) -> ParsedIntent:
        """
        Deserialise the LLM's JSON output into a ParsedIntent dataclass.

        Args:
            raw: Raw string from the LLM response content.

        Returns:
            ParsedIntent populated from the JSON payload.

        Raises:
            LLMResponseParseError: If the JSON is malformed or missing required fields.
        """
        # extract_json_object raises LLMResponseParseError directly (see
        # json_repair.py) — nothing to catch/rewrap here.
        text = extract_json_object(raw)

        try:
            data: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            # Repair literal newlines inside string values (common 7B model failure)
            try:
                data = json.loads(escape_literal_newlines(text))
            except json.JSONDecodeError as exc:
                raise LLMResponseParseError(f"LLM returned non-JSON response: {raw[:200]}") from exc

        raw_rt = data.get("request_type", "data_query")
        try:
            request_type = RequestType(raw_rt)
        except ValueError:
            request_type = RequestType.DATA_QUERY

        return ParsedIntent(
            entities=data.get("entities", []),
            metrics=data.get("metrics", []),
            filters=data.get("filters", {}),
            time_range=data.get("time_range"),
            output_format=_safe_enum(OutputFormat, data.get("output_format")),
            chart_type=_safe_enum(ChartType, data.get("chart_type")),
            needs_clarification=bool(data.get("needs_clarification", False)),
            clarification_question=data.get("clarification_question"),
            request_type=request_type,
            # user_question populated by the caller (parse()) after this returns
        )
