"""
Thin wrapper around the Anthropic SDK.

Why this file exists at all: every other module should call `call_claude()`
instead of touching the SDK directly. That's what makes the batch-vs-sync
swap in run_enrichment.py a one-line change later, and it's the one place
that needs to know about retries, model names, and JSON-fence stripping.
"""

import json
import os
import time

import anthropic
from dotenv import load_dotenv

load_dotenv()

MODEL_SUMMARY = "claude-sonnet-5"
MODEL_LANDSCAPE = "claude-sonnet-5"

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    raise RuntimeError(
        "ANTHROPIC_API_KEY is not set. Add it to a .env file in the repo root, "
        "or export it in your shell before running this script."
    )

client = anthropic.Anthropic(api_key=api_key)



def _strip_json_fences(text: str) -> str:
    """Claude will occasionally wrap JSON in ```json fences even when told
    not to. Strip defensively rather than trusting the prompt alone."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[: -3]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def call_claude(
    system: str,
    user_message: str,
    model: str,
    max_tokens: int = 1024,
    max_retries: int = 3,
) -> dict:
    """Call Claude, parse a JSON object out of the response, retry on
    transient failures or malformed JSON. Raises on final failure -- callers
    (the enrichment loop) decide whether to skip the trial or halt the run.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            cleaned = _strip_json_fences(raw_text)
            return json.loads(cleaned)

        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            last_error = e
            wait = 2 ** attempt
            time.sleep(wait)

        except json.JSONDecodeError as e:
            # Model didn't follow the JSON contract. Worth logging the raw
            # text somewhere in your real implementation so you can spot
            # prompt drift -- swallowing it silently will hide bugs.
            last_error = e
            time.sleep(1)

    raise RuntimeError(
        f"call_claude failed after {max_retries} attempts: {last_error}"
    )
