"""
Repairs for common small-LLM JSON output failures.

Local models (especially general-purpose ones like codellama, as opposed to
qwen2.5 which was chosen partly for JSON obedience) frequently violate a
"respond with ONLY a JSON object" instruction by prefixing prose, wrapping the
object in markdown fences, or leaving unescaped newlines inside string values.
These helpers are shared by every module that parses structured LLM output
(IntentParser, QueryPlanner) so the tolerance is consistent everywhere.
"""

from __future__ import annotations

import re


class LLMResponseParseError(ValueError):
    """
    Raised when an LLM's response cannot be parsed into the expected structure.

    Deliberately its own type rather than relying on bare ValueError. A retry
    loop that does `except ValueError` around an LLM call will also catch any
    *unrelated* bug that happens to raise a ValueError subclass in the same
    code path — UnicodeEncodeError, notably, is one — and silently misreport
    it as "the model returned invalid JSON", discarding what might have been
    a perfectly good response and hiding the real bug entirely. Retry loops
    must catch exactly this type; anything else should propagate as the real
    error it is.
    """


def extract_json_object(raw: str) -> str:
    """
    Strip markdown fences and leading/trailing prose, return only the JSON object.

    Handles three common small-LLM failure modes:
      1. Code fence wrapping (```json ... ```)
      2. Prose text before the opening brace (e.g. "Here's the JSON: {...}")
      3. Trailing prose after the closing brace

    Finds the first "{" and then walks forward tracking brace depth (ignoring
    braces inside string literals) to find its matching "}", so nested JSON
    (e.g. Mongo aggregation pipelines) and trailing commentary are both handled.

    Raises:
        LLMResponseParseError: If no opening brace is found at all.
    """
    text = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    start = text.find("{")
    if start == -1:
        raise LLMResponseParseError(f"No JSON object found in LLM output: {raw[:100]}")

    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # Unbalanced braces — return what we have and let json.loads report the error
    return text[start:]


def escape_literal_newlines(s: str) -> str:
    """
    Replace literal (unescaped) newlines inside JSON string values with \\n.

    Small LLMs commonly output multi-line reasoning text inside a JSON string
    field, which produces invalid JSON. This function repairs that specific
    failure mode by walking the string character-by-character and escaping
    bare newlines that appear inside a JSON string literal.
    """
    result: list[str] = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == "\\" and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif ch == "\n" and in_string:
            result.append("\\n")
        elif ch == "\r" and in_string:
            result.append("\\r")
        else:
            result.append(ch)
    return "".join(result)
