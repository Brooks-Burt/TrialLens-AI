"""
pipeline/llm.py -- the only file that touches the Anthropic SDK.

Model IDs live here and nowhere else. Verified against Anthropic's model
table on 2026-10-29 (claude-api skill, cached 2026-10-06):
  claude-haiku-5-5   planner (cheap, fast: turns a question into a plan)
  claude-sonnet-5-5  writer  (stronger: writes the one-paragraph answer)

Replies are requested with structured outputs (`output_config.format` with
a JSON schema), so the API itself guarantees valid JSON in the declared
shape. Callers still read fields with .get() and defaults (v1 bug 5).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import REPO_ROOT  # noqa: E402

PLANNER_MODEL = "claude-haiku-5-5"
WRITER_MODEL = "claude-sonnet-5-5"


class LLMError(RuntimeError):
    """A model call failed in a way the caller should show, not crash on."""


class TruncatedResponseError(LLMError):
    """
    The reply hit max_tokens. Not retried: the same request truncates the
    same way. Checked before parsing so it isn't misreported as bad JSON
    (a v1 debugging trap).
    """


@dataclass
class LLMResult:
    data: dict
    model: str
    usage: dict = field(default_factory=dict)  # input/output/cache tokens, for the trace panel


_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """
    Created on first use, not at import: importing this module (e.g. from a
    test) must never crash because a key is missing (v1 bug 1).
    """
    global _client
    if _client is None:
        load_dotenv(REPO_ROOT / ".env")
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise LLMError(
                f"ANTHROPIC_API_KEY is not set. Add it to {REPO_ROOT / '.env'} "
                "or export it in your shell."
            )
        _client = anthropic.Anthropic()  # SDK retries 429/5xx/connection errors itself (2 retries)
    return _client


def call_json(
    system: str,
    user: str,
    schema: dict,
    model: str,
    max_tokens: int = 8000,
    effort: str = "medium",
) -> LLMResult:
    """One request, one JSON object back in the shape of `schema`."""
    try:
        response = get_client().messages.create(
            model=model,
            max_tokens=max_tokens,
            # The system prompt is fixed, so cache it: repeat calls within
            # 5 minutes read it at ~10% of the input price.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Could not reach the Anthropic API: {e}") from e
    except anthropic.AuthenticationError as e:
        raise LLMError(
            f"The Anthropic API rejected the API key (401). Replace ANTHROPIC_API_KEY in "
            f"{REPO_ROOT / '.env'} with a current key."
        ) from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e

    u = response.usage
    usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise LLMError(f"The model declined this request ({getattr(details, 'category', None) or 'no category given'}).")
    text = "".join(b.text for b in response.content if b.type == "text")
    if response.stop_reason == "max_tokens":
        raise TruncatedResponseError(
            f"Reply hit the {max_tokens}-token limit and was cut off ({len(text)} chars returned)."
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model reply was not valid JSON ({e}). First 300 chars: {text[:300]!r}") from e
    if not isinstance(data, dict):
        raise LLMError(f"Model reply was JSON but not an object: {text[:300]!r}")
    return LLMResult(data=data, model=response.model, usage=usage)
