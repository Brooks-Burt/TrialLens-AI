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


class TruncatedResponseError(RuntimeError):
    """
    The model ran out of output budget mid-response.

    This gets its own exception type because it is NOT a transient failure
    and must not be retried. Retrying is deterministic waste: the same input
    and the same max_tokens produce the same truncation, so a 3-attempt loop
    just triples the bill for the same error.

    It also has to be caught BEFORE json.loads, because a truncated JSON
    object surfaces as `json.JSONDecodeError: Unterminated string`, which
    points at the parser and hides the actual cause (the token ceiling).
    That misdiagnosis cost real debugging time on NCT00413218, a trial whose
    registry record is ~40k tokens and whose summary did not fit in 800.
    """


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

            # Check the stop reason before parsing. `stop_reason == "max_tokens"`
            # is the API telling us plainly that the response is incomplete;
            # trusting the parser to infer that from a syntax error is how you
            # end up chasing a JSON bug that was never a JSON bug.
            if response.stop_reason == "max_tokens":
                raise TruncatedResponseError(
                    f"Response hit the {max_tokens}-token ceiling and was cut off "
                    f"mid-JSON ({len(raw_text)} chars returned). Raise max_tokens "
                    f"for this call, or trim the input record. Not retrying, since "
                    f"the same request would truncate identically."
                )

            cleaned = _strip_json_fences(raw_text)
            return json.loads(cleaned)

        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            last_error = e
            wait = 2 ** attempt
            time.sleep(wait)

        except json.JSONDecodeError as e:
            # Genuine prompt drift: the response completed but wasn't valid
            # JSON. Log the raw text -- swallowing it silently is how prompt
            # regressions go unnoticed.
            last_error = e
            print(
                f"  [call_claude] attempt {attempt}: response was not valid JSON "
                f"({e}). First 300 chars:\n    {raw_text[:300]!r}"
            )
            time.sleep(1)

    raise RuntimeError(
        f"call_claude failed after {max_retries} attempts: {last_error}"
    )
