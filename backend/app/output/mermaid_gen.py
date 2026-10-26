"""
Mermaid diagram generator — LLM generates syntax, mermaid-cli renders it to PNG.

Flow:
  1. Build a schema summary + intent description as context
  2. Prompt the LLM (qwen2.5:7b via OllamaProvider) to generate valid Mermaid syntax
  3. Strip any markdown fences the model may add
  4. Attempt to render via `mmdc` (mermaid-cli) subprocess
  5. On render failure: retry once — pass the error back to the LLM for self-correction
  6. Return the PNG path on success

Diagram type selection (guided by the LLM prompt):
  erDiagram       — "how do X and Y relate", foreign keys, data model questions
  flowchart TD    — data pipeline, ETL flow, process step questions
  sequenceDiagram — step-by-step or temporal questions

The LLM provider is injected (LLMProviderBase), so this module is provider-agnostic.
Switching from OllamaProvider to a cloud provider later requires zero changes here.

Prerequisites (baked into the Docker image):
  npm install -g @mermaid-js/mermaid-cli
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import structlog

from app.llm.base import LLMProviderBase
from app.models.schemas import ParsedIntent, SchemaContext

logger = structlog.get_logger(__name__)


def _find_mmdc() -> str:
    """
    Locate the mmdc binary, handling Windows .cmd wrapper scripts.

    On Windows, npm installs mmdc as mmdc.cmd which subprocess cannot
    resolve without shell=True or the explicit .cmd extension.
    Returns the resolved command string, or raises FileNotFoundError.
    """
    # Try plain name first (works on Linux/macOS and Windows if PATH is set correctly)
    found = shutil.which("mmdc")
    if found:
        logger.debug("mmdc found via shutil.which", path=found)
        return found

    # Windows fallback: try mmdc.cmd explicitly
    if sys.platform == "win32":
        found_cmd = shutil.which("mmdc.cmd")
        if found_cmd:
            logger.debug("mmdc found as mmdc.cmd", path=found_cmd)
            return found_cmd

        # Last resort: well-known npm global path on Windows
        npm_mmdc = Path.home() / "AppData" / "Roaming" / "npm" / "mmdc.cmd"
        if npm_mmdc.exists():
            logger.debug("mmdc found at npm global path", path=str(npm_mmdc))
            return str(npm_mmdc)

    raise FileNotFoundError(
        "mmdc binary not found — ensure @mermaid-js/mermaid-cli is installed: "
        "npm install -g @mermaid-js/mermaid-cli"
    )

# ── Prompt templates ──────────────────────────────────────────────────────────

_GENERATE_PROMPT = """\
You are a technical diagram expert. Generate a single valid Mermaid diagram \
that directly answers the user's question about the data schema below.

## Warehouse Schema
{schema_summary}

## User Question Context
{intent_summary}

## Instructions
Choose the most appropriate diagram type:
  - erDiagram       : table relationships, foreign keys, data model structure
  - flowchart TD    : data pipelines, ETL steps, processing flows
  - sequenceDiagram : temporal or step-by-step flows between systems

Rules:
  - Output ONLY raw Mermaid syntax — no prose, no markdown fences, no explanation
  - Use actual table and column names from the schema above
  - Keep it concise (max 20 entities/nodes)
  - The syntax must be valid — it will be fed directly to mermaid-cli for rendering

Example of valid erDiagram output:
erDiagram
    ORDERS ||--o{{ ORDER_ITEMS : contains
    CUSTOMERS ||--o{{ ORDERS : places
    ORDERS {{
        string order_id PK
        string customer_id FK
        date   created_at
    }}

Output the Mermaid diagram now:
"""

_RETRY_PROMPT = """\
Your previous Mermaid diagram had a syntax error when rendered:

Error: {error}

Previous diagram:
{previous_diagram}

Fix the syntax error. Output ONLY the corrected Mermaid diagram — no prose, no fences:
"""


class MermaidGenerator:
    """
    Generates Mermaid diagrams via LLM-synthesised syntax rendered by mermaid-cli.

    Inject any LLMProviderBase implementation — the generator is provider-agnostic.
    """

    MMDC_TIMEOUT_SECONDS: int = 30
    MAX_RETRIES: int = 2

    def __init__(self, llm_provider: LLMProviderBase) -> None:
        self._llm = llm_provider

    async def generate(
        self,
        intent: ParsedIntent,
        schema: SchemaContext,
        output_path: Path,
    ) -> Path:
        """
        Generate a Mermaid diagram PNG and write it to output_path.

        Args:
            intent:      Parsed user intent — provides diagram type context.
            schema:      Warehouse schema — provides entity and column names.
            output_path: Destination PNG file path.

        Returns:
            output_path on success.

        Raises:
            RuntimeError: If the LLM or mermaid-cli fails after all retries.
        """
        schema_summary = _build_schema_summary(schema)
        intent_summary = _build_intent_summary(intent)

        prompt = _GENERATE_PROMPT.format(
            schema_summary=schema_summary,
            intent_summary=intent_summary,
        )

        mermaid_syntax: str = ""
        last_error: str = ""

        for attempt in range(self.MAX_RETRIES):
            if attempt == 0:
                messages = [{"role": "user", "content": prompt}]
            else:
                messages = [
                    {
                        "role": "user",
                        "content": _RETRY_PROMPT.format(
                            error=last_error,
                            previous_diagram=mermaid_syntax,
                        ),
                    }
                ]

            raw = await self._llm.complete(messages=messages, max_tokens=1024)
            mermaid_syntax = _strip_fences(raw)

            error = await self._render(mermaid_syntax, output_path)
            if error is None:
                logger.info(
                    "Mermaid diagram rendered",
                    path=str(output_path),
                    attempt=attempt,
                )
                return output_path

            last_error = error
            logger.warning(
                "Mermaid render failed, retrying",
                attempt=attempt,
                error=error[:300],
            )

        raise RuntimeError(
            f"Mermaid generation failed after {self.MAX_RETRIES} attempts. "
            f"Last mmdc error: {last_error[:400]}"
        )

    async def _render(self, syntax: str, output_path: Path) -> str | None:
        """
        Write Mermaid syntax to a temp file and call `mmdc` to render PNG.

        Returns None on success, or the error string on failure.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._render_sync,
            syntax,
            output_path,
        )

    def _render_sync(self, syntax: str, output_path: Path) -> str | None:
        """Synchronous mermaid-cli subprocess — called from thread-pool."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mmd", delete=False, encoding="utf-8"
        ) as f:
            f.write(syntax)
            mmd_path = f.name

        logger.debug("Mermaid syntax written to temp file", path=mmd_path, syntax_preview=syntax[:120])

        try:
            mmdc_bin = _find_mmdc()
        except FileNotFoundError as exc:
            return str(exc)

        cmd = [mmdc_bin, "--input", mmd_path, "--output", str(output_path), "--outputFormat", "png"]
        logger.debug("Running mmdc", cmd=" ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.MMDC_TIMEOUT_SECONDS,
            )
            logger.debug(
                "mmdc finished",
                returncode=result.returncode,
                stdout=result.stdout[:300] if result.stdout else "",
                stderr=result.stderr[:300] if result.stderr else "",
            )
            if result.returncode != 0:
                return result.stderr or result.stdout or f"mmdc exited with code {result.returncode}"
            if not output_path.exists():
                return "mmdc returned 0 but output file was not created"
            logger.info("mmdc render successful", output=str(output_path), size_bytes=output_path.stat().st_size)
            return None
        except subprocess.TimeoutExpired:
            return f"mmdc timed out after {self.MMDC_TIMEOUT_SECONDS}s"
        finally:
            Path(mmd_path).unlink(missing_ok=True)


# ── Module-level helpers ──────────────────────────────────────────────────────


def _strip_fences(raw: str) -> str:
    """Remove markdown code fences if the model wraps output in ```mermaid ... ```."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _build_schema_summary(schema: SchemaContext) -> str:
    lines: list[str] = []
    for table_key, table in schema.tables.items():
        cols = ", ".join(f"{c.name} {c.data_type}" for c in table.columns)
        lines.append(f"TABLE {table_key}: {cols}")
    return "\n".join(lines) if lines else "(no schema available)"


def _build_intent_summary(intent: ParsedIntent) -> str:
    parts: list[str] = []
    if intent.entities:
        parts.append(f"Entities: {', '.join(intent.entities)}")
    if intent.filters:
        parts.append(f"Filters: {intent.filters}")
    if intent.time_range:
        parts.append(f"Time range: {intent.time_range}")
    return ". ".join(parts) if parts else "General schema overview requested."
