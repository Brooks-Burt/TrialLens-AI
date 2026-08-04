"""
cli/ask.py

Turns a plain-English question into an answer, end to end:

    question --> query_planner (Claude proposes, registry validates)
             --> LOCAL database search (never a live CT.gov study pull)
             --> deterministic ranking (phase, then status, then recency)
             --> landscape synthesis (one Claude call, only over trials
                 that already have a cached summary)
             --> printed answer, with dropped terms and un-summarized
                 trials both surfaced honestly rather than hidden

WHY THE SEARCH STEP IS LOCAL-ONLY, ON PURPOSE:
Two different things in this file talk to CT.gov, and they are not the same
cost:

  1. Term validation (inside query_planner.build_query()) -- a cheap,
     count-only HTTP call per candidate term, no Claude involved. This is
     the hallucination guardrail. It MUST stay live: the whole point is
     catching a term that ISN'T in your local database, so checking only
     locally would defeat it.

  2. Finding which trials actually match the validated query -- this is
     the part that would normally mean pulling full study records from
     CT.gov's live search endpoint, which is slow and, if summaries then
     need generating, expensive in Claude tokens.

This file replaces step 2 with a search against data/trials.db. That means
an answer is only as complete as what's already been pulled and enriched
locally. Rather than silently pretending that's the whole picture, any
matching trial that doesn't have a cached summary yet is listed separately
and explicitly, never folded into the synthesis paragraph.

Usage:
    python cli/ask.py "what's competing with rezafungin in refractory candidemia?"
    python cli/ask.py --limit 10 "what antifungals are being tested for aspergillosis?"
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# cli/ is a sibling of enrich/, common/, normalize/ -- add repo root so
# package-qualified imports resolve regardless of where this is launched
# from. Same pattern as query_planner.py and pull_trials.py.
HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

import anthropic  # noqa: E402

from common.ctgov_client import CTGovClient  # noqa: E402
from enrich.landscape import synthesize_landscape  # noqa: E402
from enrich.query_planner import ValidatedQuery, build_query  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "trials.db"

# How many of the best-matching local trials to consider for an answer.
# Not every one of these will have a cached summary -- see split logic
# below. Matches the "top ~15-20 trials" figure in the scope doc.
DEFAULT_TOP_N = 15

# Deterministic ranking weights. Higher phase and more active status rank
# first; ties break on whichever registry date is most recent. Nothing
# here is subjective or model-driven -- these are settled, documented
# choices, not tuned constants.
PHASE_RANK = {"PHASE4": 4, "PHASE3": 3, "PHASE2": 2, "PHASE1": 1}
STATUS_RANK = {
    "RECRUITING": 3,
    "ACTIVE_NOT_RECRUITING": 2,
    "COMPLETED": 1,
    "TERMINATED": 0,
    "WITHDRAWN": 0,
}


def _phase_score(phase_field: str | None) -> int:
    """trials.phase can hold multiple phases joined with '/', e.g.
    'PHASE2/PHASE3' -- score by whichever is highest."""
    if not phase_field:
        return 0
    return max((PHASE_RANK.get(p, 0) for p in phase_field.split("/")), default=0)


def _status_score(status: str | None) -> int:
    return STATUS_RANK.get(status or "", 0)


def _text_blob(trial: dict) -> str:
    """One lowercased string of everything worth substring-matching
    against: title plus the JSON-encoded conditions/interventions lists.
    Cheap and good enough at this trial count -- see scope doc's
    'no vector search' non-goal."""
    return " ".join(
        [trial.get("title") or "", trial.get("conditions") or "", trial.get("interventions") or ""]
    ).lower()


def _matches(trial: dict, query: ValidatedQuery) -> bool:
    """
    A local trial counts as a match if:
      - it mentions at least one validated intervention term (if any were
        validated -- if every candidate term was dropped, this check is
        skipped rather than matching nothing, so a condition-only search
        still returns something; the dropped terms are still shown to the
        user separately, so nothing is hidden)
      - it mentions at least one condition term (same fallback rule)
      - it does NOT mention any excluded term (e.g. the drug the user
        asked about competitors to, per query_planner rule 3)
      - its phase is in the requested phase list, if one was given
      - its status is in the requested status list, if one was given
    """
    blob = _text_blob(trial)

    if query.interventions and not any(term.lower() in blob for term in query.interventions):
        return False

    if query.conditions and not any(term.lower() in blob for term in query.conditions):
        return False

    if query.exclude_terms and any(term.lower() in blob for term in query.exclude_terms):
        return False

    if query.phases:
        trial_phases = set((trial.get("phase") or "").split("/"))
        if not trial_phases & set(query.phases):
            return False

    if query.statuses and (trial.get("overall_status") or "") not in query.statuses:
        return False

    return True


def search_local_trials(conn: sqlite3.Connection, query: ValidatedQuery) -> list[dict]:
    """
    Search data/trials.db only. No live CT.gov study-search call happens
    here -- that is the deliberate trade-off that keeps this file fast and
    free to run repeatedly. Ranked best-match-first before returning.
    """
    rows = conn.execute(
        """
        SELECT t.nct_id, t.title, t.sponsor, t.phase, t.overall_status,
               t.primary_completion_date, t.conditions, t.interventions,
               t.last_changed_at, s.scientific_summary
        FROM trials t
        LEFT JOIN summaries s ON s.nct_id = t.nct_id
        """
    )
    columns = [d[0] for d in rows.description]
    all_trials = [dict(zip(columns, row)) for row in rows.fetchall()]

    matched = [t for t in all_trials if _matches(t, query)]

    matched.sort(
        key=lambda t: (
            _phase_score(t.get("phase")),
            _status_score(t.get("overall_status")),
            t.get("primary_completion_date") or t.get("last_changed_at") or "",
        ),
        reverse=True,
    )
    return matched


def answer_question(question: str, top_n: int = DEFAULT_TOP_N) -> dict:
    """
    Runs the full pipeline and returns a dict describing what happened,
    rather than printing directly -- keeps this function testable and
    keeps the CLI's print formatting separate from the logic.
    """
    anthropic_client = anthropic.Anthropic()
    ctgov_client = CTGovClient()

    query = build_query(question, anthropic_client, ctgov_client)

    if query.out_of_scope:
        return {
            "out_of_scope": query.out_of_scope,
            "interpretation": query.interpretation,
        }

    conn = sqlite3.connect(DB_PATH)
    all_matches = search_local_trials(conn, query)
    top = all_matches[:top_n]
    conn.close()

    with_summary = [t for t in top if t.get("scientific_summary")]
    without_summary = [t for t in top if not t.get("scientific_summary")]

    landscape_paragraph = None
    if with_summary:
        landscape_paragraph = synthesize_landscape(question, with_summary)

    return {
        "out_of_scope": None,
        "interpretation": query.interpretation,
        "dropped_terms": query.dropped_terms,
        "total_matches": len(all_matches),
        "shown": len(top),
        "with_summary": with_summary,
        "without_summary": without_summary,
        "landscape": landscape_paragraph,
    }


def print_report(result: dict):
    if result.get("out_of_scope"):
        print(f"Interpretation: {result['interpretation']}")
        print(f"\nOut of scope: {result['out_of_scope']}")
        return

    print(f"Interpretation: {result['interpretation']}")

    if result["dropped_terms"]:
        print(f"\nDropped terms (proposed by Claude, zero registry hits, never queried):")
        for term in result["dropped_terms"]:
            print(f"  - {term}")

    print(f"\n{result['total_matches']} trial(s) matched locally; showing top {result['shown']}.")

    if result["without_summary"]:
        print(
            f"\n{len(result['without_summary'])} of those have NOT been summarized yet "
            f"(run enrich/run_enrichment.py to include them next time):"
        )
        for t in result["without_summary"]:
            print(f"  - {t['nct_id']}  ({t.get('phase') or 'phase unknown'}, "
                  f"{t.get('overall_status') or 'status unknown'})")

    if result["landscape"]:
        print(f"\n--- Competitive landscape ({len(result['with_summary'])} summarized trials) ---")
        print(result["landscape"])
    else:
        print(
            "\nNo matching trials have a cached summary yet, so no landscape synthesis "
            "was generated. Run enrich/run_enrichment.py, then ask again."
        )

    if result["with_summary"]:
        print("\n--- Trials included in the synthesis above ---")
        for t in result["with_summary"]:
            print(f"  {t['nct_id']}  {t.get('title') or ''}")
            print(f"    phase={t.get('phase') or '?'}  status={t.get('overall_status') or '?'}"
                  f"  sponsor={t.get('sponsor') or 'not reported'}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", nargs="+", help="Natural-language question, quoted or not")
    parser.add_argument("--limit", type=int, default=DEFAULT_TOP_N,
                        help=f"How many top-ranked local matches to consider (default {DEFAULT_TOP_N})")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    question_text = " ".join(args.question)

    if not DB_PATH.exists():
        print(f"ERROR: no database at {DB_PATH}. Run normalize/build_db.py first.", file=sys.stderr)
        sys.exit(1)

    try:
        result = answer_question(question_text, top_n=args.limit)
        print_report(result)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
