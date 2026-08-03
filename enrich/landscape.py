"""
Competitive landscape synthesis. One call per query/comparison set --
this is the piece of the cost strategy that's easiest to accidentally
break (it's tempting to just loop summarize_trial-style logic per trial,
but that's the wrong shape here; see scope doc cost strategy item 5).

Called by the CLI ([6] in the architecture diagram) after it has
ranked/selected the top ~15-20 trials for a query and made sure each of
them already has a cached scientific_summary (generating any that are
missing via summarize.py first).
"""

import json

from client import call_claude, MODEL_LANDSCAPE
from prompts import LANDSCAPE_SYSTEM, LANDSCAPE_USER_TEMPLATE


def synthesize_landscape(user_query: str, trials_with_summaries: list[dict]) -> str:
    """
    user_query: the original natural-language question, e.g. "what's
        competing with fluconazole in refractory candidiasis?"
    trials_with_summaries: list of dicts, each already containing at
        least {"nct_id", "sponsor", "phase", "status", "scientific_summary"}
        -- pull these from your cached `summaries` table, not fresh from
        the API. If any trial in this list lacks a scientific_summary,
        that's a bug in the caller: generate it first, don't send partial
        data here.

    Returns the synthesis paragraph as a plain string (not a dict) since
    this is a single field, easier for the CLI to print directly.
    """
    if not trials_with_summaries:
        return "No trials matched this query closely enough to synthesize a landscape."

    trials_block = "\n\n".join(
        f"- NCT ID: {t['nct_id']}\n"
        f"  Sponsor: {t.get('sponsor', 'Not reported')}\n"
        f"  Phase: {t.get('phase', 'Not reported')}\n"
        f"  Status: {t.get('status', 'Not reported')}\n"
        f"  Scientific summary: {t['scientific_summary']}"
        for t in trials_with_summaries
    )

    user_message = LANDSCAPE_USER_TEMPLATE.format(
        user_query=user_query,
        n_trials=len(trials_with_summaries),
        trials_block=trials_block,
    )

    result = call_claude(
        system=LANDSCAPE_SYSTEM,
        user_message=user_message,
        model=MODEL_LANDSCAPE,
        max_tokens=600,
    )

    if "competitive_landscape" not in result:
        raise ValueError(f"Claude response missing competitive_landscape: {result}")

    return result["competitive_landscape"]
