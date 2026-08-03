"""
normalize/materiality.py

The change-detection layer, in about sixty lines.

The question this file answers is: "has this trial changed in a way worth
spending a Claude call on?" Not "has anything at all changed" -- CT.gov
records churn constantly on fields nobody cares about (contact phone
numbers, facility addresses, record verification dates). If those counted
as changes, every trial would look changed on every pull and the cost
control would silently do nothing.

So: a whitelist of fields that genuinely signal something, serialized in a
canonical form, hashed with SHA-256. Same hash = nothing material moved =
skip the trial entirely.

This file has no database awareness and no Claude awareness. It takes a
CT.gov study dict, returns a string.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# The whitelist, documented in one place. If you add a field here, every
# trial's hash changes on the next run and the whole dataset re-enriches
# once -- that's expected and correct, but know it before you edit.
#
#   overallStatus            recruiting -> terminated is the headline event
#   phase                    phase advancement is the core pipeline signal
#   enrollmentCount          enrollment cuts often precede trouble
#   primaryCompletionDate    slipped readouts are the highest-value signal
#   whyStopped               only ever populated when something went wrong
#   hasResults               results posting is a discrete, dateable event
#   interventions            arm changes mean a protocol amendment
#   armGroups                same
#   primaryOutcomes          endpoint changes are material by definition
#   conditions               indication changes are rare but always material


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    """
    Walk a nested dict safely. dig(study, "protocolSection", "statusModule",
    "overallStatus") returns None instead of raising KeyError if any level
    is missing.

    CT.gov omits whole modules for sparse records rather than including them
    empty, so missing intermediate keys are normal, not exceptional.
    """
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def extract_material_fields(study: dict) -> dict:
    """
    Pull the whitelist fields out of a raw CT.gov v2 study record.

    Lists are sorted before being returned. This is deliberate: CT.gov does
    not guarantee stable ordering within arrays, so an unsorted list would
    produce a different hash on a pull where nothing actually changed.
    Sorting costs you the ability to detect pure reordering, which is not a
    material event.
    """
    protocol = study.get("protocolSection", {})

    interventions = dig(protocol, "armsInterventionsModule", "interventions", default=[]) or []
    arm_groups = dig(protocol, "armsInterventionsModule", "armGroups", default=[]) or []
    primary_outcomes = dig(protocol, "outcomesModule", "primaryOutcomes", default=[]) or []
    phases = dig(protocol, "designModule", "phases", default=[]) or []
    conditions = dig(protocol, "conditionsModule", "conditions", default=[]) or []

    return {
        "overall_status": dig(protocol, "statusModule", "overallStatus"),
        "why_stopped": dig(protocol, "statusModule", "whyStopped"),
        "primary_completion_date": dig(
            protocol, "statusModule", "primaryCompletionDateStruct", "date"
        ),
        "phases": sorted(phases),
        "enrollment": dig(protocol, "designModule", "enrollmentInfo", "count"),
        "has_results": bool(study.get("hasResults", False)),
        "conditions": sorted(conditions),
        "interventions": sorted(
            i.get("name") for i in interventions if isinstance(i, dict) and i.get("name")
        ),
        "arm_labels": sorted(
            a.get("label") for a in arm_groups if isinstance(a, dict) and a.get("label")
        ),
        "primary_outcomes": sorted(
            o.get("measure") for o in primary_outcomes
            if isinstance(o, dict) and o.get("measure")
        ),
    }


def canonicalize(payload: dict) -> str:
    """
    Turn the whitelist dict into one deterministic string.

    Every argument here is load-bearing:
      sort_keys=True     -- dict ordering must never affect the hash
      separators         -- no incidental whitespace between runs
      ensure_ascii=True  -- unicode always escapes the same way
      default=str        -- an unexpected type stringifies instead of raising

    Drop any one of these and the same unchanged trial can hash differently
    on two runs, which silently re-enriches your whole dataset.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def compute_materiality_hash(study: dict) -> str:
    """Raw CT.gov study dict in, 64-character hex digest out."""
    return hashlib.sha256(canonicalize(extract_material_fields(study)).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    # Determinism check. Hashes the same record twice, then hashes a copy
    # with a non-material field changed, then one with a material field
    # changed. Run against a real file:
    #
    #   python normalize/materiality.py data/raw/NCT01234567.json
    import copy
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print("usage: python normalize/materiality.py <path-to-raw-trial.json>")
        sys.exit(1)

    study = json.loads(Path(sys.argv[1]).read_text())

    h1 = compute_materiality_hash(study)
    h2 = compute_materiality_hash(copy.deepcopy(study))
    print(f"same record twice:        {h1[:16]}...  {h2[:16]}...  {'MATCH' if h1 == h2 else 'DIFFER'}")

    noise = copy.deepcopy(study)
    noise.setdefault("protocolSection", {}).setdefault("contactsLocationsModule", {})[
        "centralContacts"
    ] = [{"name": "Changed Person", "phone": "555-0000"}]
    h3 = compute_materiality_hash(noise)
    print(f"non-material field moved: {h3[:16]}...          {'MATCH (correct)' if h1 == h3 else 'DIFFER (bug!)'}")

    material = copy.deepcopy(study)
    material.setdefault("protocolSection", {}).setdefault("designModule", {}).setdefault(
        "enrollmentInfo", {}
    )["count"] = 99999
    h4 = compute_materiality_hash(material)
    print(f"enrollment changed:       {h4[:16]}...          {'DIFFER (correct)' if h1 != h4 else 'MATCH (bug!)'}")
