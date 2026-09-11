"""
enrich/trim.py

Reduces a full CT.gov v2 study record to the fields a summary actually needs.

WHY THIS EXISTS

`summarize.py` used to send the entire raw study record to Claude. For most
trials that's fine (median record is ~11k characters), but records with
posted results are enormous, and the distribution is brutally skewed:

    median                     11,061 chars
    mean                       29,526 chars
    largest (NCT04229303)     333,463 chars   (~90k tokens)

Measuring where that bulk actually lives:

    NCT04229303   resultsSection  293,861 of 333,463   (88%)
      outcomeMeasuresModule       222,787
      adverseEventsModule          37,454
      baselineCharacteristicsModule 18,698

Those are per-arm statistical tables. They are the raw numbers behind a
result, not the result, and a 2-4 sentence business summary does not need
them. Meanwhile the fields that DO drive the summary (status, phase,
enrollment, arms, endpoints, why stopped) are a few hundred characters.

So this module keeps the signal and drops the tables. On the current
dataset that cuts the enrichment input by roughly 80% and removes the
context-limit risk on the largest records entirely.

THE HONESTY PROBLEM, AND HOW IT'S HANDLED

There's a trap here worth being explicit about. `prompts.py` instructs the
model to write "Not reported in registry" when a field it needs is missing.
If this module silently drops a field, the model will faithfully report it
as *absent from the registry* -- which is false. The trial has the data; we
chose not to send it.

Two things prevent that:

  1. Anything the prompt asks about is kept. The trim is a superset of the
     materiality whitelist, not equal to it: change detection and
     summarization need different fields, and this is the wider set.
  2. What was dropped is stated in the payload. Every trimmed record carries
     an `_omitted` key naming the modules that were removed and why, so the
     model can distinguish "the registry doesn't have this" from "this tool
     didn't send it." For a trial with results, that key also carries a
     compact digest (participant counts, serious adverse event totals,
     outcome measure titles) so the risk section still has something real to
     work from.

NO CROSS-MODULE IMPORTS

`dig()` is duplicated from normalize/materiality.py rather than imported.
The project rule is that domain modules don't import each other, and ten
lines of safe dict traversal is a cheaper price than breaking that. If a
third module ever needs it, it belongs in common/.
"""

from __future__ import annotations

import json
from typing import Any

# Free-text fields get capped. These are prose, and a summary needs the gist,
# not the full protocol. Eligibility criteria in particular run to 15k
# characters on some records.
MAX_BRIEF_SUMMARY = 2500
MAX_DETAILED_DESCRIPTION = 2000
MAX_ELIGIBILITY = 1500
MAX_OUTCOME_DESCRIPTION = 400

# Endpoint lists are kept in full for primary outcomes (always material) and
# capped for secondary, which can run to dozens of entries.
MAX_SECONDARY_OUTCOMES = 10
MAX_OUTCOME_TITLES_IN_DIGEST = 15

# Modules removed wholesale, with the reason recorded in the payload.
DROPPED_MODULES = {
    "contactsLocationsModule": "site addresses and investigator contacts; "
                               "hundreds of entries, no bearing on a summary",
    "referencesModule": "citation list",
    "ipdSharingStatementModule": "data sharing boilerplate",
    "oversightModule": "regulatory oversight flags",
    "documentSection": "attached protocol/SAP PDFs",
    "derivedSection": "CT.gov-derived MeSH terms",
}


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    """Walk a nested dict safely, returning `default` instead of raising if
    any level is missing. CT.gov omits whole modules for sparse records."""
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _cap(text: Any, limit: int) -> Any:
    """
    Truncate long free text, marking the cut visibly.

    The marker matters: a silent truncation mid-sentence looks like the
    registry itself is incomplete, which is exactly the confusion this
    module is supposed to avoid.
    """
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"... [truncated at {limit} chars by TrialLens]"


def _results_digest(results: dict) -> dict | None:
    """
    Compact stand-in for the full resultsSection.

    Keeps what a risk assessment can actually use: how many participants
    started and finished, how many serious adverse events were recorded, and
    what the outcome measures were called. Drops the per-arm numeric tables
    that make up the bulk.
    """
    if not results:
        return None

    digest: dict[str, Any] = {}

    # Participant flow: first period's starting and completing counts.
    periods = dig(results, "participantFlowModule", "periods", default=[]) or []
    if periods:
        milestones = dig(periods[0], "milestones", default=[]) or []
        flow = {}
        for m in milestones:
            if not isinstance(m, dict):
                continue
            t = m.get("type")
            if t not in ("STARTED", "COMPLETED", "NOT COMPLETED"):
                continue
            total = 0
            for a in m.get("achievements", []) or []:
                raw = (a or {}).get("numSubjects")
                if raw and str(raw).isdigit():
                    total += int(raw)
            if total:
                flow[t.lower().replace(" ", "_")] = total
        if flow:
            digest["participant_flow_totals"] = flow

    # Adverse events: counts and the most-affected serious events by name.
    ae = results.get("adverseEventsModule") or {}
    if ae:
        serious = ae.get("seriousEvents") or []
        other = ae.get("otherEvents") or []
        ae_summary: dict[str, Any] = {
            "serious_event_types_reported": len(serious),
            "other_event_types_reported": len(other),
        }
        if ae.get("frequencyThreshold"):
            ae_summary["other_event_frequency_threshold_pct"] = ae["frequencyThreshold"]

        def _affected(ev: dict) -> int:
            return sum(
                int(s.get("numAffected") or 0)
                for s in (ev.get("stats") or [])
                if isinstance(s, dict)
            )

        top = sorted(
            ((_affected(e), e.get("term")) for e in serious if isinstance(e, dict)),
            reverse=True,
        )[:5]
        named = [{"term": term, "num_affected_all_arms": n} for n, term in top if term]
        if named:
            ae_summary["most_frequent_serious_events"] = named
        digest["adverse_events"] = ae_summary

    # Outcome measures: titles only. The tables behind them are the 222k.
    measures = results.get("outcomeMeasuresModule", {}).get("outcomeMeasures") or []
    if measures:
        titles = [
            m.get("title") for m in measures
            if isinstance(m, dict) and m.get("title")
        ][:MAX_OUTCOME_TITLES_IN_DIGEST]
        digest["outcome_measures_reported"] = {
            "count": len(measures),
            "titles": titles,
            "note": "Per-arm numeric results tables omitted. Results ARE posted "
                    "for this trial; the values simply were not sent.",
        }

    return digest or None


def trim_study(study: dict) -> dict:
    """
    Full CT.gov v2 study record in, compact summary-ready dict out.

    Returns a dict with the same conceptual shape as the registry record but
    flattened one level, plus an `_omitted` key documenting what was removed.
    """
    protocol = study.get("protocolSection", {}) or {}
    results = study.get("resultsSection", {}) or {}

    ident = protocol.get("identificationModule", {}) or {}
    status = protocol.get("statusModule", {}) or {}
    design = protocol.get("designModule", {}) or {}
    arms_mod = protocol.get("armsInterventionsModule", {}) or {}
    outcomes = protocol.get("outcomesModule", {}) or {}
    elig = protocol.get("eligibilityModule", {}) or {}
    desc = protocol.get("descriptionModule", {}) or {}

    trimmed: dict[str, Any] = {
        "nct_id": ident.get("nctId"),
        "brief_title": ident.get("briefTitle"),
        "official_title": ident.get("officialTitle"),
        "lead_sponsor": dig(protocol, "sponsorCollaboratorsModule", "leadSponsor", "name"),
        "collaborators": [
            c.get("name")
            for c in (dig(protocol, "sponsorCollaboratorsModule", "collaborators",
                          default=[]) or [])
            if isinstance(c, dict) and c.get("name")
        ],

        "overall_status": status.get("overallStatus"),
        "why_stopped": status.get("whyStopped"),
        "start_date": dig(status, "startDateStruct", "date"),
        "primary_completion_date": dig(status, "primaryCompletionDateStruct", "date"),
        "completion_date": dig(status, "completionDateStruct", "date"),

        "study_type": design.get("studyType"),
        "phases": design.get("phases"),
        "enrollment": dig(design, "enrollmentInfo", "count"),
        "enrollment_type": dig(design, "enrollmentInfo", "type"),
        "design_info": {
            k: v for k, v in (design.get("designInfo") or {}).items()
            if k in ("allocation", "interventionModel", "primaryPurpose", "maskingInfo")
        } or None,

        "conditions": dig(protocol, "conditionsModule", "conditions"),
        "keywords": dig(protocol, "conditionsModule", "keywords"),

        "brief_summary": _cap(desc.get("briefSummary"), MAX_BRIEF_SUMMARY),
        "detailed_description": _cap(
            desc.get("detailedDescription"), MAX_DETAILED_DESCRIPTION
        ),

        "arm_groups": [
            {
                "label": a.get("label"),
                "type": a.get("type"),
                "intervention_names": a.get("interventionNames"),
            }
            for a in (arms_mod.get("armGroups") or []) if isinstance(a, dict)
        ],
        "interventions": [
            {
                "name": i.get("name"),
                "type": i.get("type"),
                "other_names": i.get("otherNames"),
            }
            for i in (arms_mod.get("interventions") or []) if isinstance(i, dict)
        ],

        "primary_outcomes": [
            {
                "measure": o.get("measure"),
                "time_frame": o.get("timeFrame"),
                "description": _cap(o.get("description"), MAX_OUTCOME_DESCRIPTION),
            }
            for o in (outcomes.get("primaryOutcomes") or []) if isinstance(o, dict)
        ],
        "secondary_outcomes": [
            o.get("measure")
            for o in (outcomes.get("secondaryOutcomes") or [])[:MAX_SECONDARY_OUTCOMES]
            if isinstance(o, dict) and o.get("measure")
        ],

        "eligibility": {
            "sex": elig.get("sex"),
            "minimum_age": elig.get("minimumAge"),
            "maximum_age": elig.get("maximumAge"),
            "healthy_volunteers": elig.get("healthyVolunteers"),
            "criteria": _cap(elig.get("eligibilityCriteria"), MAX_ELIGIBILITY),
        },

        "has_results": bool(study.get("hasResults", False)),
    }

    # Drop keys that are None or empty so the model isn't reading a wall of
    # nulls. A field genuinely absent from the registry is correctly reported
    # as not reported; the _omitted note below covers the rest.
    trimmed = {k: v for k, v in trimmed.items() if v not in (None, [], {}, "")}

    omitted: dict[str, Any] = {
        "_read_this_first": (
            "This record is a curated subset of the ClinicalTrials.gov entry, "
            "not the full entry. Fields listed below were removed by TrialLens "
            "before sending. Do NOT describe them as 'not reported in the "
            "registry' -- the registry may well have them. Only say a field is "
            "unreported if it is absent from the data above AND not named here."
        ),
        "dropped_modules": DROPPED_MODULES,
    }

    digest = _results_digest(results)
    if digest:
        omitted["results_section"] = (
            "Full per-arm statistical tables omitted (they are the bulk of the "
            "record). A digest is included below."
        )
        omitted["results_digest"] = digest
    elif study.get("hasResults"):
        omitted["results_section"] = "Flagged as having results, but none parsed."

    trimmed["_omitted"] = omitted
    return trimmed


def measure_reduction(study: dict) -> dict:
    """Before/after character counts for one record. Used by the CLI below."""
    before = len(json.dumps(study, default=str))
    after = len(json.dumps(trim_study(study), indent=2, default=str))
    return {
        "before_chars": before,
        "after_chars": after,
        "reduction_pct": round((1 - after / before) * 100, 1) if before else 0.0,
    }


if __name__ == "__main__":
    # Run against the real database to see the reduction across every trial
    # still needing enrichment:
    #
    #   python enrich/trim.py
    #
    # Or inspect one trimmed record in full:
    #
    #   python enrich/trim.py NCT04229303
    import sqlite3
    import statistics
    import sys
    from pathlib import Path

    DB = Path(__file__).parent.parent / "data" / "trials.db"
    conn = sqlite3.connect(DB)

    if len(sys.argv) > 1:
        nct = sys.argv[1]
        row = conn.execute(
            "SELECT raw_json FROM trials WHERE nct_id = ?", (nct,)
        ).fetchone()
        if not row:
            print(f"no such trial: {nct}")
            sys.exit(1)
        study = json.loads(row[0])
        print(json.dumps(trim_study(study), indent=2, default=str))
        print("\n" + json.dumps(measure_reduction(study), indent=2), file=sys.stderr)
        sys.exit(0)

    rows = conn.execute(
        """
        SELECT t.nct_id, t.raw_json
        FROM trials t LEFT JOIN summaries s ON s.nct_id = t.nct_id
        WHERE s.nct_id IS NULL OR s.materiality_hash != t.materiality_hash
        """
    ).fetchall()

    befores, afters, worst = [], [], []
    for nct, raw in rows:
        m = measure_reduction(json.loads(raw))
        befores.append(m["before_chars"])
        afters.append(m["after_chars"])
        worst.append((m["before_chars"], m["after_chars"], nct))

    print(f"{len(rows)} trials needing enrichment\n")
    print(f"{'':22} {'before':>12} {'after':>12}")
    print(f"{'total chars':22} {sum(befores):>12,} {sum(afters):>12,}")
    print(f"{'median chars':22} {int(statistics.median(befores)):>12,} "
          f"{int(statistics.median(afters)):>12,}")
    print(f"{'largest record':22} {max(befores):>12,} {max(afters):>12,}")
    print(f"\noverall input reduction: "
          f"{round((1 - sum(afters) / sum(befores)) * 100, 1)}%")
    print(f"est input tokens: {int(sum(befores) / 3.7):,} -> "
          f"{int(sum(afters) / 3.7):,}")

    print("\nlargest records, before -> after:")
    for b, a, nct in sorted(worst, reverse=True)[:8]:
        print(f"  {nct}  {b:>9,} -> {a:>7,}  ({round((1 - a / b) * 100)}% smaller)")
