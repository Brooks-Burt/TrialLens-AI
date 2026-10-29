"""
pipeline/summarize.py -- stage 5: Claude (Sonnet) writes one paragraph from
the facts Python already computed. It never sees raw registry JSON and never
counts. Checking its output (citations, numbers) is parked for the next
update; until then the table beside the answer is the check.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.llm import WRITER_MODEL, LLMError, call_json  # noqa: E402
from pipeline.prompts import SUMMARY_SYSTEM  # noqa: E402

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}


@dataclass
class Summary:
    text: str = ""
    error: str = ""
    model: str = ""
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0


def summarize(facts_text: str, effort: str = "medium") -> Summary:
    """Never raises: a failed call returns Summary(error=...) so the table can still show."""
    start = time.monotonic()
    try:
        reply = call_json(SUMMARY_SYSTEM, f"<facts>\n{facts_text}\n</facts>", SUMMARY_SCHEMA,
                          model=WRITER_MODEL, max_tokens=16000, effort=effort)
    except LLMError as e:
        print(f"  summarizer call failed: {e}")
        return Summary(error=str(e), seconds=round(time.monotonic() - start, 1))
    text = str(reply.data.get("summary", "") or "").strip()
    return Summary(text=text, error="" if text else "The writer returned an empty summary.",
                   model=reply.model, usage=reply.usage, seconds=round(time.monotonic() - start, 1))
