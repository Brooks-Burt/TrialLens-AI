"""
normalize/compact.py

Shrink a CT.gov study record for STORAGE, without changing its shape.

WHY THIS IS NOT enrich/trim.py
==============================

The repo already has a size-reduction module, and reusing it here was the
obvious move. It is the wrong move, for two concrete reasons.

`trim_study()` is tuned for one job: building an LLM payload. It therefore
does two things this module must not do.

1. It RESHAPES the record. Output keys are flattened snake_case --
   `nct_id`, `brief_title`, `lead_sponsor` -- not CT.gov's nested
   `protocolSection.identificationModule.nctId`. Storing that as
   `trials.raw_json` would break `compute_materiality_hash()`, which walks
   the nested path, and would break the `"protocolSection" in trial` check
   that `summarize.py` uses to decide whether a record still needs
   trimming.

2. It DROPS `interventions[].description`, keeping only name, type, and
   otherNames. That field is Tier C in the v2 structured-match scoring
   table (see docs/SCOPE-v2.md) -- it is where a target like "selective
   inhibitor of beta-1,3-glucan synthase" actually appears when the
   intervention name is just a development code. Storing trimmed records
   would silently delete a field the ranker scores on, and the symptom
   would be novel assets ranking badly for mechanism queries, with nothing
   in any log to explain it.

So: two functions, two jobs.

    enrich/trim.py     reshape + cap, for what Claude reads
    normalize/compact.py   structure-preserving bulk removal, for what we store

This module is deliberately conservative. It removes only things that are
verifiably bulk with no retrieval or summarization value, and it leaves
`protocolSection` structurally intact.


WHAT IS REMOVED, AND WHY IT IS SAFE
===================================

trim.py already measured where the bulk lives, on the largest record in the
corpus (NCT04229303, 333,463 chars):

    resultsSection                  293,861 of 333,463   (88%)
      outcomeMeasuresModule           222,787
      adverseEventsModule              37,454
      baselineCharacteristicsModule    18,698

Those are per-arm statistical tables -- the raw numbers behind a result,
not the result. Nothing in v2 reads them: the landscape metrics come from
phase, status, enrollment, sponsor, and conditions; the embedding reads
title, conditions, interventions, and summary; provenance quotes
eligibility criteria and outcome measures. The tables are never touched.

`hasResults` is preserved at the top level, because "results are posted" is
a material, dateable event and the materiality whitelist includes it. What
is dropped is the contents of those tables, not the fact of their existence
-- and `_compacted` records exactly what was removed so a reader is never
guessing whether a record is whole.

Full records remain on disk in `data/raw/`, untouched and gitignored. They
are the source of truth and are regenerable by re-running ingest. This
module only affects what gets copied into the database.
"""

from __future__ import annotations

import copy
import json
from typing import Any

# Whole protocolSection modules removed. Each is either administrative or
# reproducible from elsewhere, and none is read by any v2 module.
DROPPED_PROTOCOL_MODULES = {
    "contactsLocationsModule": "site addresses and investigator contacts; "
                               "hundreds of entries on multi-site trials, "
                               "no retrieval or analytic value",
    "referencesModule": "publication citation list",
    "ipdSharingStatementModule": "data-sharing boilerplate",
    "oversightModule": "regulatory oversight flags",
}

# resultsSection modules removed. These are the per-arm statistical tables
# measured above -- the single largest contributor to record size.
DROPPED_RESULTS_MODULES = {
    "outcomeMeasuresModule": "per-arm statistical tables for each endpoint",
    "adverseEventsModule": "per-arm AE tables",
    "baselineCharacteristicsModule": "per-arm baseline demographic tables",
}

# Top-level sections removed.
DROPPED_SECTIONS = {
    "documentSection": "attached protocol and SAP PDFs",
    "derivedSection": "CT.gov-derived MeSH terms; regenerable and not read",
}


def compact_study(study: dict) -> dict:
    """
    Structure-preserving compaction. Returns a new dict; the input is not
    mutated.

    Post-conditions this function guarantees, because downstream code
    depends on all three:

      1. `protocolSection` is present and nested exactly as CT.gov returns
         it, so `compute_materiality_hash()` produces the SAME hash for a
         compacted record as for the full one. This is asserted in
         `verify_hash_stability()` below and in the smoke test at the bottom
         of this file -- if it ever stops holding, every trial in the corpus
         would appear to have materially changed on the next build, and the
         entire enrichment cost control would silently evaporate. That is
         v1 bug #2 with a different cause.
      2. `interventions[].description` survives, because the ranker scores
         on it.
      3. `hasResults` survives, because it is in the materiality whitelist.
    """
    compacted = copy.deepcopy(study)
    omitted: dict[str, str] = {}

    for section, reason in DROPPED_SECTIONS.items():
        if section in compacted:
            del compacted[section]
            omitted[section] = reason

    protocol = compacted.get("protocolSection")
    if isinstance(protocol, dict):
        for module, reason in DROPPED_PROTOCOL_MODULES.items():
            if module in protocol:
                del protocol[module]
                omitted[f"protocolSection.{module}"] = reason

    results = compacted.get("resultsSection")
    if isinstance(results, dict):
        for module, reason in DROPPED_RESULTS_MODULES.items():
            if module in results:
                del results[module]
                omitted[f"resultsSection.{module}"] = reason
        # An emptied resultsSection is removed rather than left as {}, so
        # "this record has no results content" is one condition to test
        # instead of two.
        if not results:
            del compacted["resultsSection"]

    # Recorded IN the stored record, not in a sidecar, so anyone reading a
    # row -- in the dashboard, in a SQL client, in six months -- can see
    # immediately that it is compacted and what is absent. A truncated
    # record that looks complete is the kind of thing that gets quoted in a
    # meeting.
    compacted["_compacted"] = {
        "note": "Storage-compacted by normalize/compact.py. "
                "The full record is in data/raw/{nctId}.json.",
        "omitted": omitted,
    }

    return compacted


def verify_hash_stability(study: dict) -> bool:
    """
    Assert that compaction does not move the materiality hash.

    Imported locally rather than at module scope to keep this file free of
    import-order coupling -- compact.py is a pure function over a dict and
    should stay importable on its own.
    """
    from normalize.materiality import compute_materiality_hash

    return compute_materiality_hash(study) == compute_materiality_hash(
        compact_study(study)
    )


def measure(study: dict) -> dict:
    before = len(json.dumps(study, default=str))
    after = len(json.dumps(compact_study(study), default=str))
    return {
        "before": before,
        "after": after,
        "saved": before - after,
        "pct": round(100 * (before - after) / before, 1) if before else 0.0,
    }


if __name__ == "__main__":
    # Measure reduction and verify hash stability across the real corpus.
    #
    #   python normalize/compact.py
    #   python normalize/compact.py data/raw/NCT04229303.json
    import statistics
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from common.paths import RAW_DIR  # noqa: E402

    if len(sys.argv) > 1:
        paths = [Path(sys.argv[1])]
    else:
        paths = sorted(RAW_DIR.glob("NCT*.json"))

    if not paths:
        print(f"No raw records found in {RAW_DIR}. Run ingest first.")
        sys.exit(1)

    results = []
    hash_failures = []

    for path in paths:
        study = json.loads(path.read_text())
        results.append(measure(study))
        if not verify_hash_stability(study):
            hash_failures.append(path.stem)

    before = sum(r["before"] for r in results)
    after = sum(r["after"] for r in results)
    pcts = [r["pct"] for r in results]

    print(f"Records:        {len(results)}")
    print(f"Total before:   {before / 1e6:.2f} MB")
    print(f"Total after:    {after / 1e6:.2f} MB")
    print(f"Reduction:      {100 * (before - after) / before:.1f}%")
    print(f"Per-record pct: median {statistics.median(pcts):.1f}%  "
          f"min {min(pcts):.1f}%  max {max(pcts):.1f}%")
    print(f"Mean size:      {before / len(results) / 1024:.1f} KB -> "
          f"{after / len(results) / 1024:.1f} KB")
    print()

    if hash_failures:
        print(f"HASH STABILITY FAILED on {len(hash_failures)} record(s):")
        for nct_id in hash_failures[:10]:
            print(f"  {nct_id}")
        print()
        print("Compaction removed a field inside the materiality whitelist.")
        print("Every trial would appear materially changed on the next build,")
        print("and the enrichment cost control would silently stop working.")
        sys.exit(1)

    print(f"HASH STABILITY VERIFIED on all {len(results)} records:")
    print("  compacted and full records produce identical materiality hashes.")
