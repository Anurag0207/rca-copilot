"""
Query planner — maps a ParsedIntent to an executable HQL/SQL query.

Uses a second local LLM call (via OllamaProvider) to translate the structured
intent into a concrete query string, guided by the warehouse schema. If the
intent is ambiguous or cannot be reliably mapped, returns a ClarificationRequest
instead of a query — the caller must then surface the question to the user.

The prompt template (QUERY_GENERATION_PROMPT) is an editable module-level
constant. Modify it here to improve query quality without touching logic.

Safety guarantee: the prompt instructs the LLM to generate only SELECT
statements. Upstream callers additionally validate the returned query with
_validate_query_is_read_only() before any MCP execution.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

import structlog

from app.llm.base import LLMProviderBase
from app.llm.json_repair import LLMResponseParseError, escape_literal_newlines, extract_json_object
from app.models.schemas import (
    ClarificationRequest,
    ParsedIntent,
    QueryPlan,
    SchemaContext,
)

if TYPE_CHECKING:
    from app.config import Settings
    from app.mcp.base import UserIdentity

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Editable prompt template — modify to tune query generation behaviour
# ─────────────────────────────────────────────────────────────────────────────

QUERY_GENERATION_PROMPT_SQL: str = """\
You are an expert HQL/SQL query writer for Apache Hive data warehouses.
Given the structured intent and the warehouse schema below, generate a single,
correct, read-only HQL query that satisfies the user's request.

## Warehouse Schema
{schema_summary}

## Structured Intent
{intent_json}

## Rules
- Generate ONLY a SELECT query. Never write INSERT, UPDATE, DELETE, DROP, or any DDL.
- Use fully-qualified table names: database.table_name.
- Apply all filters from the intent in a WHERE clause.
- Apply any time range filters using appropriate date/timestamp columns.
- Use GROUP BY when aggregating metrics.
- If the intent cannot be reliably mapped to the schema, set needs_clarification=true.
- Be precise: do not guess column names that aren't in the schema.

Respond ONLY with a valid JSON object — no prose, no markdown:
{{
  "query_string": "<HQL SELECT statement or null>",
  "target_tables": ["<database.table>"],
  "estimated_complexity": "<low|medium|high>",
  "parameters": {{}},
  "needs_clarification": <true|false>,
  "clarification_question": "<string or null>"
}}
"""

QUERY_GENERATION_PROMPT_MONGO: str = """\
You are a data analyst and MongoDB expert. Before writing any pipeline, you
will extract the metric and dimension from the question, find the collections
that hold them, trace the join path, and then write the pipeline.

## Full Database Schema
{schema_summary}

## User Question (exact wording — use this to extract METRIC and DIMENSION)
{user_question}

## Parsed Intent (structured hints — may be incomplete; the question above takes precedence)
{intent_json}
{feedback_section}
## Instructions

### Step 0 — Extract METRIC and DIMENSION from the question

Read the question carefully and answer these three questions in your reasoning:
- METRIC: what number is being computed? (e.g. revenue → sum of price; count → sum of 1; score → avg of review_score)
- DIMENSION: what field is the breakdown grouped by? (e.g. "by product category" → product_category_name; "by seller" → seller_id; "by payment type" → payment_type)
- FILTER: any conditions? (e.g. year = 2017; status = 'delivered'; none)

CRITICAL — "which X" / "top N X" / "highest X per Y" questions:
The word "which" or "top" signals a RANKED LIST. The noun after "which" / "top" is your DIMENSION.
Example: "which product categories generate the most revenue"
  → METRIC = revenue (sum of price from the transactions/items collection)
  → DIMENSION = product category name (look up the field name in the schema above)
  → This is a ranked list — group by that field, sort descending, limit 10
  → NEVER collapse this to a single total with _id: null

_id: null is ONLY valid when the user explicitly asks for ONE number with no breakdown
(e.g. "what is the total revenue?", "how many orders are there in total?")

### Step 1 — Locate the METRIC field and the DIMENSION field in the schema

Scan the schema above for:
1. Which collection contains the METRIC field
2. Which collection contains the DIMENSION field

These may be the same collection (no join needed) or different collections (join needed).

### Step 2 — Find the join path

If METRIC and DIMENSION are in different collections, find the chain of shared field
names that connects them. The join path is: MetricCollection --[shared_field]--> DimensionCollection
(or through a bridge collection if there is no direct shared field).

Write the full path in your reasoning before writing any pipeline stage.

### Step 3 — Write the aggregation pipeline

Rules:
- Start from the collection that holds the METRIC
- Use $lookup to reach the DIMENSION collection; use $unwind immediately after every $lookup
- After joining collection B with alias "b", all of B's fields are referenced as "$b.field_name"
- Set _id in $group to "$<alias>.<dimension_field>" when grouping by a joined field
- NEVER use _id: null for ranked/breakdown questions (see Step 0)
- For top-N: $group → $sort descending → $limit N
- If a field has more than 80% null values (marked in the schema above), do NOT use it as a grouping key
- Use BARE collection names (no database prefix)
- NEVER use _id as a foreignField in $lookup. MongoDB's _id is an internal ObjectId, not a business join key. Always use the named field that appears in both collections (e.g. customer_id, order_id, product_id, seller_id)
- Place $match before $lookup where possible for performance
- ALWAYS add a $project stage as the LAST pipeline stage to rename _id to a descriptive human-readable field name. Format: {{"$project": {{"_id": 0, "<descriptive_name>": "$_id", "<metric_field>": 1}}}}. Example: if you grouped by product_category_name → {{"$project": {{"_id": 0, "product_category": "$_id", "total_revenue": 1}}}}. This is REQUIRED — never leave _id as an output column.
- When the user asks for human-readable names or labels (e.g. "product name", "category name", "customer name", "city") and the natural grouping key is a raw ID field (field name ends in "_id" or values look like UUIDs/hashes), look up the dimension table in the schema that maps those IDs to human-readable names, join it, and group by the name field instead. Raw ID values make unreadable chart labels.

### Worked examples

⚠️  The collection names in these examples are from a sample e-commerce dataset used only to
illustrate reasoning methodology. In your actual response you MUST use only the collection
names that appear in the Full Database Schema above — never copy names from the examples below.

Example A — metric and dimension in the same collection: "total revenue by payment type"
  METRIC = sum of payment_value (in generic_transactions)
  DIMENSION = payment_type (also in generic_transactions)
  → No join needed. Rename _id → payment_type in $project.
{{
  "reasoning": "METRIC: sum of payment_value. DIMENSION: payment_type. Both are in generic_transactions — no join needed. Group by payment_type, then rename _id.",
  "collection": "generic_transactions",
  "pipeline": [
    {{"$group": {{"_id": "$payment_type", "total_revenue": {{"$sum": "$payment_value"}}}}}},
    {{"$sort": {{"total_revenue": -1}}}},
    {{"$project": {{"_id": 0, "payment_type": "$_id", "total_revenue": 1}}}}
  ],
  "target_tables": ["generic_transactions"],
  "estimated_complexity": "low",
  "needs_clarification": false,
  "clarification_question": null
}}

Example B — metric and dimension in different collections: "which product categories generate the most revenue"
  METRIC = sum of price (in generic_line_items)
  DIMENSION = product_category_name (in generic_products)
  JOIN KEY = product_id (appears in both generic_line_items and generic_products)
  → Join line_items → products on product_id, group by $products.product_category_name, rename _id.
{{
  "reasoning": "METRIC: sum of price — in generic_line_items. DIMENSION: product_category_name — in generic_products. JOIN: product_id shared between both. Path: generic_line_items --[product_id]--> generic_products. Group by $products.product_category_name, then rename _id.",
  "collection": "generic_line_items",
  "pipeline": [
    {{"$lookup": {{"from": "generic_products", "localField": "product_id", "foreignField": "product_id", "as": "products"}}}},
    {{"$unwind": "$products"}},
    {{"$group": {{"_id": "$products.product_category_name", "total_revenue": {{"$sum": "$price"}}}}}},
    {{"$sort": {{"total_revenue": -1}}}},
    {{"$limit": 10}},
    {{"$project": {{"_id": 0, "product_category": "$_id", "total_revenue": 1}}}}
  ],
  "target_tables": ["generic_line_items", "generic_products"],
  "estimated_complexity": "medium",
  "needs_clarification": false,
  "clarification_question": null
}}

Example C — three-collection chain: "which sellers have the highest avg review score"
  METRIC = avg of review_score (in generic_reviews)
  DIMENSION = seller_id (in generic_line_items, reachable via order_id)
  JOIN PATH: generic_reviews --[order_id]--> generic_line_items --[seller_id]--> group
  → Group by $items.seller_id, rename _id → seller_id in $project.
{{
  "reasoning": "METRIC: avg of review_score — in generic_reviews. DIMENSION: seller_id — in generic_line_items. JOIN PATH: reviews --[order_id]--> line_items. Group by $items.seller_id, then rename _id.",
  "collection": "generic_reviews",
  "pipeline": [
    {{"$lookup": {{"from": "generic_line_items", "localField": "order_id", "foreignField": "order_id", "as": "items"}}}},
    {{"$unwind": "$items"}},
    {{"$group": {{"_id": "$items.seller_id", "avg_score": {{"$avg": "$review_score"}}, "review_count": {{"$sum": 1}}}}}},
    {{"$sort": {{"avg_score": -1}}}},
    {{"$limit": 10}},
    {{"$project": {{"_id": 0, "seller_id": "$_id", "avg_score": 1, "review_count": 1}}}}
  ],
  "target_tables": ["generic_reviews", "generic_line_items"],
  "estimated_complexity": "medium",
  "needs_clarification": false,
  "clarification_question": null
}}

Example D — "most sold products" / "top products by number of items sold"
  METRIC = count of items sold ($sum: 1) in generic_line_items
  DIMENSION = product_category_name (in generic_products — "product names" always means the category name, never the raw product_id UUID)
  JOIN KEY = product_id
  → Join line_items → products, group by category name, rename _id.
{{
  "reasoning": "METRIC: count of items — in generic_line_items. DIMENSION: product names = product_category_name in generic_products (never raw product_id). JOIN: product_id. Path: generic_line_items --[product_id]--> generic_products. Group by $products.product_category_name, rename _id → product_category.",
  "collection": "generic_line_items",
  "pipeline": [
    {{"$lookup": {{"from": "generic_products", "localField": "product_id", "foreignField": "product_id", "as": "products"}}}},
    {{"$unwind": "$products"}},
    {{"$group": {{"_id": "$products.product_category_name", "items_sold": {{"$sum": 1}}}}}},
    {{"$sort": {{"items_sold": -1}}}},
    {{"$limit": 10}},
    {{"$project": {{"_id": 0, "product_category": "$_id", "items_sold": 1}}}}
  ],
  "target_tables": ["generic_line_items", "generic_products"],
  "estimated_complexity": "medium",
  "needs_clarification": false,
  "clarification_question": null
}}

## Output Format
Respond ONLY with a valid JSON object. No text outside the JSON.
{{
  "reasoning": "<Step 0 METRIC/DIMENSION/FILTER extraction, then Step 1 collection location, then Step 2 join path — one paragraph covering all three>",
  "collection": "<name of the starting collection>",
  "pipeline": [<array of aggregation stage objects>],
  "target_tables": ["<bare collection names used>"],
  "estimated_complexity": "<low|medium|high>",
  "needs_clarification": <true|false>,
  "clarification_question": "<string or null>"
}}
"""

# Keep backward-compatible alias — callers that pass schema.connector_type branch below
QUERY_GENERATION_PROMPT = QUERY_GENERATION_PROMPT_SQL


def _pretty_query(query_string: str) -> str:
    """Pretty-print a query for the feedback prompt (JSON pipeline or raw SQL)."""
    try:
        return json.dumps(json.loads(query_string), indent=2)
    except (json.JSONDecodeError, TypeError):
        return query_string


class QueryPlanner:
    """
    Generates an executable HQL/SQL query from a ParsedIntent.

    Returns a QueryPlan on success, or a ClarificationRequest when the
    intent cannot be unambiguously mapped to the warehouse schema.
    Uses OllamaProvider (qwen2.5:14b by default) — no cloud API calls.
    """

    def __init__(self, llm_provider: LLMProviderBase, settings: "Settings") -> None:
        self._llm = llm_provider
        self._settings = settings

    async def plan(
        self,
        intent: ParsedIntent,
        schema: SchemaContext,
        user_identity: "UserIdentity",
    ) -> QueryPlan | ClarificationRequest:
        return await self._do_plan(intent, schema, user_identity, previous_query=None, failure_reason=None)

    async def plan_with_feedback(
        self,
        intent: ParsedIntent,
        schema: SchemaContext,
        user_identity: "UserIdentity",
        previous_query: str,
        failure_reason: str,
    ) -> QueryPlan | ClarificationRequest:
        """
        Re-plan after a validation failure.

        Deliberately does NOT tell the model what the fix is. *previous_query* is
        the exact pipeline it wrote last time; *failure_reason* is a factual
        statement of what was observed (an empty result, a null dimension field,
        the raw MongoDB error, etc.) — never a pre-baked diagnosis like "field X
        doesn't exist, use field Y instead". Diagnosing the root cause against
        the schema is left entirely to the model's own reasoning, the same way
        it reasons through METRIC/DIMENSION/JOIN on a first attempt.
        """
        return await self._do_plan(
            intent, schema, user_identity, previous_query=previous_query, failure_reason=failure_reason
        )

    async def _do_plan(
        self,
        intent: ParsedIntent,
        schema: SchemaContext,
        user_identity: "UserIdentity",
        previous_query: str | None,
        failure_reason: str | None,
    ) -> QueryPlan | ClarificationRequest:
        schema_summary = self._build_schema_summary(schema)
        intent_json    = self._serialise_intent(intent)

        if failure_reason:
            previous_query_block = (
                _pretty_query(previous_query) if previous_query else "(not available)"
            )
            feedback_section = f"""
## Previous Attempt Failed — Diagnose It Yourself Before Writing a New Pipeline

Your previous pipeline:
{previous_query_block}

What was actually observed when it ran:
{failure_reason}

This is evidence, not a diagnosis — you have to work out the root cause
yourself by comparing your previous pipeline against the schema above.
Common causes: a field name that doesn't actually exist in the collection
(check the schema's field list, don't guess), a join on the wrong key, a
filter that's too strict, grouping by the wrong field for what the user
asked, or an aggregation operator that doesn't match the metric. State the
specific cause you found in your "reasoning" field, then write a corrected
pipeline that fixes exactly that — do not resubmit the previous pipeline
unchanged.
"""
        else:
            feedback_section = ""

        prompt_template = (
            QUERY_GENERATION_PROMPT_MONGO
            if schema.connector_type == "mongodb"
            else QUERY_GENERATION_PROMPT_SQL
        )
        prompt = prompt_template.format(
            schema_summary=schema_summary,
            user_question=intent.user_question or "(not available)",
            intent_json=intent_json,
            feedback_section=feedback_section,
        )

        if failure_reason:
            logger.debug("QueryPlanner retry prompt (full, unabridged)", prompt=prompt)

        for attempt in range(2):
            try:
                raw = await self._llm.complete(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self._settings.OLLAMA_MAX_TOKENS,
                )
                return self._parse_llm_response(raw)
            except LLMResponseParseError as exc:
                if attempt == 0:
                    logger.warning("QueryPlanner: retrying after parse failure", error=str(exc))
                    continue
                raise RuntimeError(f"QueryPlanner failed after 2 attempts: {exc}") from exc

        raise RuntimeError("QueryPlanner: unreachable")

    def _build_schema_summary(self, schema: SchemaContext) -> str:
        """
        Render SchemaContext in verbose vertical format for the query planner prompt.

        Each field is shown on its own line with up to 5 sample values and a
        null-rate warning when >= 80 % of sampled documents lack the field.
        This format lets the LLM scan for shared field names across collections
        to infer join keys without any hardcoded relationship map.
        """
        blocks: list[str] = []
        for _, table in schema.tables.items():
            lines: list[str] = [f"COLLECTION {table.name}"]
            for c in table.columns:
                if c.name == "_id":
                    # MongoDB's internal ObjectId — never a business join key; skip it
                    # so the model doesn't confuse it with named foreign-key fields
                    continue
                parts: list[str] = [f"  {c.name}"]
                if c.sample_values:
                    formatted: list[str] = []
                    for v in c.sample_values[:5]:
                        if v is None:
                            formatted.append("null")
                        elif isinstance(v, str):
                            truncated = v[:40] + ("…" if len(v) > 40 else "")
                            formatted.append(f'"{truncated}"')
                        else:
                            formatted.append(str(v))
                    parts.append(f"  (e.g. {', '.join(formatted)})")
                if c.null_pct > 0.8:
                    parts.append(
                        f"  [{int(c.null_pct * 100)}% null — avoid as group key or filter]"
                    )
                lines.append("".join(parts))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else "(empty schema)"

    def _serialise_intent(self, intent: ParsedIntent) -> str:
        """Convert ParsedIntent to a JSON string for prompt injection."""
        d = dataclasses.asdict(intent)
        # Convert enum values to their string representations
        if d.get("output_format"):
            d["output_format"] = d["output_format"]
        if d.get("chart_type"):
            d["chart_type"] = d["chart_type"]
        return json.dumps(d, default=str, indent=2)

    def _parse_llm_response(
        self, raw: str
    ) -> QueryPlan | ClarificationRequest:
        """
        Deserialise the LLM's JSON output into a QueryPlan or ClarificationRequest.
        Logs the reasoning field so join-path decisions are visible in structured logs.

        Raises:
            LLMResponseParseError: If the response JSON is malformed.
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

        # Always log the reasoning field — this is the primary debugging signal
        reasoning = data.get("reasoning", "")
        if reasoning:
            logger.info(
                "QueryPlanner reasoning",
                reasoning=reasoning,
                collection=data.get("collection"),
                target_tables=data.get("target_tables"),
            )

        if data.get("needs_clarification"):
            return ClarificationRequest(
                question=data.get("clarification_question", "Could you provide more detail?"),
                options=data.get("options"),
            )

        # MongoDB response: has "collection" + "pipeline" instead of "query_string"
        if "collection" in data and "pipeline" in data:
            query_string = json.dumps(
                {"collection": data["collection"], "pipeline": data["pipeline"]}
            )
        else:
            query_string = data.get("query_string")

        if not query_string:
            raise LLMResponseParseError("LLM returned null query_string without needs_clarification=true")

        return QueryPlan(
            query_string=query_string,
            target_tables=data.get("target_tables", []),
            estimated_complexity=data.get("estimated_complexity", "low"),
            parameters=data.get("parameters", {}),
            reasoning=reasoning or None,
        )

    @staticmethod
    def _validate_query_is_read_only(query: str) -> bool:
        """
        Basic guardrail: return False if the query contains DML/DDL keywords.

        This is a defence-in-depth check — the LLM is already instructed to
        generate only SELECT statements. This method must be called before
        passing any generated query to execute_query.
        """
        forbidden = {"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE"}
        upper = query.upper()
        return not any(kw in upper for kw in forbidden)
