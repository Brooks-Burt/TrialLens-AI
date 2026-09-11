"""
normalize/relevance.py

Corpus precision audit: decide whether an ingested trial is actually about
this therapeutic area, and quarantine the ones that are not.

WHY THIS EXISTS
===============

tests/test_coverage.py answers "did every config term find trials?" -- a
RECALL question. This module answers the opposite one: "is everything we
found actually relevant?" Nothing in v1 or in the v2 plan asked that, and
the first full antifungal ingest showed it needed asking.

The root cause is a property of the registry, not a bug in the config.
CT.gov's `query.cond` performs semantic term EXPANSION rather than exact
matching, and quoting a phrase does not make it exact. So an ingest query
is inherently fuzzy, and a corpus assembled from fuzzy queries contains
trials nobody asked for.

Measured on the first complete antifungal corpus (2,470 trials):

    fungal signal present     2,255   91.3%
    no fungal signal            215    8.7%   <- pollution

What the 8.7% actually contained:

    query.cond="Candida albicans"  ->  red yeast rice cholesterol trials,
                                       HPV-16/18 vaccine immunogenicity,
                                       neuroblastoma beta-glucan studies,
                                       Candida albicans as a SKIN-TEST
                                       ANTIGEN for neurogenic inflammation
    query.cond="coccidioidomycosis" -> 26 unrelated trials
    query.intr="fluconazole"        -> 23 unrelated trials

Why this matters more than a tidiness complaint: every headline number the
dashboard reports is a count over this corpus. "27 relevant trials",
"14 companies", "most crowded indication" -- if one trial in eleven is a
statin-intolerance study, those numbers are wrong, and they are wrong in
the confident, specific-looking way that survives a demo until someone who
knows the field reads a row.


QUARANTINE, NOT DELETE
======================

A flagged trial stays in the corpus with a verdict and the reason recorded.
Three reasons for that choice:

  1. The signal lexicon is a heuristic and will have false positives. The
     first audit pass flagged NCT00000794 -- atovaquone plus pyrimethamine
     -- as irrelevant, because the lexicon lacked PCP drugs even though
     Pneumocystis is a fungus. Deleting on a heuristic's word makes that
     class of error unrecoverable and invisible.
  2. The quarantine rate is itself a reported metric. "8.7% of ingested
     trials were off-target and here they are" is a stronger statement
     than a corpus that silently happens to be clean.
  3. Retrieval can then choose: exclude quarantined trials from counts,
     while still allowing a direct NCT lookup to find them.

This module has no database awareness and no network access. It takes a
study dict and a signal spec, and returns a verdict.
"""

from __future__ import annotations

import json
import re
from typing import Any

VERDICT_RELEVANT = "relevant"
VERDICT_QUARANTINED = "quarantined"


def _collect_signal_stems(spec: dict) -> list[str]:
    """Flatten the config's signal spec, ignoring `_`-prefixed commentary."""
    signals = spec.get("relevance_signals") or {}
    stems: list[str] = []
    for key, value in signals.items():
        if key.startswith("_"):
            continue
        if isinstance(value, list):
            stems.extend(str(v).lower() for v in value)
    return stems


def build_signal_pattern(spec: dict) -> re.Pattern | None:
    """
    Compile the area's signal stems into one alternation.

    Stems are regex-escaped. They are substrings by design, not word-bounded:
    "candid" has to match "candidiasis", "candidemia", "Candida", and
    "anti-candidal", and a word boundary would break every one of those.
    """
    stems = _collect_signal_stems(spec)
    if not stems:
        return None
    return re.compile("|".join(re.escape(s) for s in sorted(set(stems))), re.I)


def searchable_text(study: dict) -> str:
    """
    The fields a relevance judgment is allowed to read.

    Deliberately the same fields the embedding representation reads, so a
    trial cannot be judged relevant on evidence the ranker never sees --
    or quarantined despite evidence the ranker would have used.

    Intervention DESCRIPTIONS are included. That is where a mechanism
    appears when the intervention name is only a development code, which is
    exactly the novel-asset case the whole pipeline is built to catch.
    """
    protocol = study.get("protocolSection", {}) or {}
    ident = protocol.get("identificationModule", {}) or {}
    conditions = protocol.get("conditionsModule", {}) or {}
    desc = protocol.get("descriptionModule", {}) or {}
    arms = protocol.get("armsInterventionsModule", {}) or {}
    outcomes = protocol.get("outcomesModule", {}) or {}

    parts: list[str] = [
        ident.get("briefTitle") or "",
        ident.get("officialTitle") or "",
        " ".join(conditions.get("conditions") or []),
        " ".join(conditions.get("keywords") or []),
        desc.get("briefSummary") or "",
        desc.get("detailedDescription") or "",
    ]

    for intervention in arms.get("interventions") or []:
        if isinstance(intervention, dict):
            parts.append(intervention.get("name") or "")
            parts.append(intervention.get("description") or "")
            parts.extend(intervention.get("otherNames") or [])

    for outcome in outcomes.get("primaryOutcomes") or []:
        if isinstance(outcome, dict):
            parts.append(outcome.get("measure") or "")

    return " ".join(p for p in parts if p)


def assess(study: dict, pattern: re.Pattern | None) -> dict[str, Any]:
    """
    Judge one trial. Returns verdict, the stems that matched, and a reason.

    A null pattern means the area configured no signals, and the honest
    verdict then is `relevant` with a reason saying the audit did not run --
    not a silent pass that looks like a clean bill of health.
    """
    if pattern is None:
        return {
            "verdict": VERDICT_RELEVANT,
            "matched": [],
            "reason": "no relevance_signals configured for this area; audit did not run",
        }

    text = searchable_text(study)
    matched = sorted({m.group(0).lower() for m in pattern.finditer(text)})

    if matched:
        return {
            "verdict": VERDICT_RELEVANT,
            "matched": matched,
            "reason": f"matched {len(matched)} signal stem(s): {', '.join(matched[:6])}",
        }

    return {
        "verdict": VERDICT_QUARANTINED,
        "matched": [],
        "reason": (
            "no area signal stem appears in title, conditions, keywords, summary, "
            "interventions, or primary outcomes -- retrieved by CT.gov semantic "
            "query expansion rather than genuine topical match"
        ),
    }


if __name__ == "__main__":
    # Audit the whole raw corpus and report the quarantine rate.
    #
    #   python normalize/relevance.py
    #   python normalize/relevance.py --show 20
    #   python normalize/relevance.py --area antifungals
    import argparse
    import collections
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from common.paths import QUERY_CONFIG_PATH, RAW_DIR, RAW_MANIFEST_PATH  # noqa: E402

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="antifungals")
    parser.add_argument("--show", type=int, default=12,
                        help="How many quarantined trials to list.")
    args = parser.parse_args()

    config = json.loads(QUERY_CONFIG_PATH.read_text())
    if args.area not in config:
        print(f"Unknown area {args.area!r}")
        sys.exit(1)

    pattern = build_signal_pattern(config[args.area])
    if pattern is None:
        print(f"No relevance_signals configured for {args.area}.")
        sys.exit(1)

    paths = sorted(RAW_DIR.glob("NCT*.json"))
    if not paths:
        print(f"No raw records in {RAW_DIR}. Run ingest first.")
        sys.exit(1)

    manifest = {}
    if RAW_MANIFEST_PATH.exists():
        manifest = json.loads(RAW_MANIFEST_PATH.read_text()).get("trials", {})

    quarantined = []
    relevant = 0
    blame: collections.Counter = collections.Counter()

    for path in paths:
        study = json.loads(path.read_text())
        result = assess(study, pattern)
        if result["verdict"] == VERDICT_RELEVANT:
            relevant += 1
            continue

        title = (
            study.get("protocolSection", {})
            .get("identificationModule", {})
            .get("briefTitle")
            or ""
        )
        quarantined.append((path.stem, title))
        for group, terms in (manifest.get(path.stem, {}).get(args.area, {}) or {}).items():
            for term in terms:
                blame[f"{group}/{term}"] += 1

    total = relevant + len(quarantined)
    print(f"Area:         {args.area}")
    print(f"Corpus:       {total} trials")
    print(f"Relevant:     {relevant} ({100 * relevant / total:.1f}%)")
    print(f"Quarantined:  {len(quarantined)} ({100 * len(quarantined) / total:.1f}%)")

    if quarantined:
        print()
        print(f"--- quarantined (first {min(args.show, len(quarantined))}) ---")
        for nct_id, title in quarantined[: args.show]:
            print(f"  {nct_id}  {title[:72]}")

    if blame:
        print()
        print("Ingest terms responsible (top 12):")
        for term, count in blame.most_common(12):
            print(f"  {term:<44}{count:>5}")
        print()
        print("  A term with a high count here is not necessarily wrong -- it is")
        print("  a term CT.gov expands aggressively. Worth checking whether a")
        print("  narrower phrasing retrieves the same real trials.")
