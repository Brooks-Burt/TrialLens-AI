"""
Per-trial enrichment: turns one trial's registry JSON into the three
trial-intrinsic sections (scientific_summary, potential_risks,
business_impact). competitive_landscape is handled separately in
landscape.py because it's a cross-trial, per-query artifact, not a
per-trial one -- see scope doc, section [3].

This module does NOT decide which trials to run on. That's the diff
engine's job (materiality hash). This module trusts its caller: if you
pass it a trial, it will call the API for that trial. The caching /
skip-if-unchanged logic belongs in run_enrichment.py, one layer up, where
you already have the `changes` table.
"""

import json
import sys
from pathlib import Path

# Guarantee this file's own folder is on the path, regardless of what
# script originally launched Python. Without this, importing summarize.py
# from outside enrich/ (view/, cli/, a test file, a REPL started elsewhere)
# fails to find client.py and prompts.py.
sys.path.insert(0, str(Path(__file__).parent))

from client import call_claude, MODEL_SUMMARY
from prompts import TRIAL_SUMMARY_SYSTEM, TRIAL_SUMMARY_USER_TEMPLATE

REQUIRED_KEYS = {"scientific_summary", "potential_risks", "business_impact"}


def summarize_trial(trial: dict) -> dict:
    """
    trial: a dict of the registry fields you pulled from CT.gov for one
    trial (whatever subset your ingest step stores -- title, phase,
    status, arms, outcomes, enrollment, whyStopped, etc). Pass the whole
    thing; let the prompt's ground rules handle "field not present."

    Returns: {"scientific_summary": ..., "potential_risks": ...,
              "business_impact": ...}

    Raises ValueError if Claude's response is missing an expected key --
    fail loudly here rather than writing a half-populated row to the
    summaries table.
    """
    trial_json = json.dumps(trial, indent=2, default=str)
    user_message = TRIAL_SUMMARY_USER_TEMPLATE.format(trial_json=trial_json)

    result = call_claude(
        system=TRIAL_SUMMARY_SYSTEM,
        user_message=user_message,
        model=MODEL_SUMMARY,
        max_tokens=800,
    )

    missing = REQUIRED_KEYS - result.keys()
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}. Got: {result}")

    return {k: result[k] for k in REQUIRED_KEYS}


if __name__ == "__main__":
    # Smoke test: run against one hand-built trial dict before wiring the
    # real pipeline. Replace this with an NCT ID pulled from your own
    # ingest output once you're ready.
    example_trial = {
        "nct_id": "NCT00000000",
        "title": "Example Phase 2 Study of Drug X in Refractory Candidiasis",
        "phase": "PHASE2",
        "status": "RECRUITING",
        "enrollment": 120,
        "primary_completion_date": "2027-03-01",
        "arms": ["Drug X 100mg", "Placebo"],
        "primary_outcome_measures": ["Clinical cure rate at Day 14"],
        "why_stopped": None,
    }
    print(json.dumps(summarize_trial(example_trial), indent=2))
