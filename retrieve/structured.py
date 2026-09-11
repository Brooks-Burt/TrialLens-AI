"""
retrieve/structured.py

Score a trial against explicit criteria, per criterion, with field authority.

TWO DESIGN DETAILS THAT MATTER MORE THAN THEY LOOK
==================================================

1. FIELD AUTHORITY TIERS.

   A criterion matching in the intervention NAME is stronger evidence than
   the same string appearing somewhere in the brief summary. Flat boolean
   matching throws that distinction away, and it is exactly the distinction
   that separates "this trial studies caspofungin" from "this trial mentions
   caspofungin as a comparator in its background section".

   For a drug criterion the authority order is
       intervention name > otherNames / alias > description > summary
   For a disease criterion it is
       structured conditions list > alias > keywords > eligibility > summary

   The orders differ because the registry's field hierarchy differs, which
   is why tiers are defined per criterion FAMILY rather than globally.

2. AN UNSPECIFIED CRITERION IS EXCLUDED FROM THE AVERAGE, NOT SCORED ZERO.

   This is a one-line mistake with corpus-wide consequences. If the user
   never mentioned a phase, a trial cannot be penalised for its phase.
   Scoring an unmentioned criterion as 0.0 drags every trial's score toward
   zero -- and does so unevenly, by however many fields the question
   happened to mention, which makes scores from two different questions
   incomparable. Worse, it would make a vague question produce uniformly
   low scores that look like "nothing matched well" rather than "few
   constraints were given".

   So the denominator is the sum of weights of criteria PRESENT IN THE
   QUERY. A question specifying only an indication is scored purely on that
   indication.

WHY THE POOL IS NOT JUST THE STRUCTURED MATCHES
===============================================
The v2 proposal describes structured filtering producing a candidate pool,
then semantic ranking WITHIN that pool. Taken literally, a trial with no
structured match can never surface -- which contradicts the other thing the
proposal wants, namely that semantic similarity should find a trial that
says "candidemia" when the question said "invasive fungal infection".

If semantic only re-ranks, it cannot add recall; it can only reorder what
keywords already found. So the pool here is the UNION of the top structured
matches and the top semantic matches over the whole corpus. That is what
makes the semantic half contribute recall rather than decoration, and it is
what the ablation in docs/ABLATION.md is able to measure.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import ASSET_ALIASES_PATH, RANKING_CONFIG_PATH  # noqa: E402
from normalize.materiality import dig  # noqa: E402

# Which tier table each criterion uses.
CRITERION_FAMILY = {
    "interventions": "drug",
    "drug_classes": "drug",
    "primary_target": "drug",
    "indications": "disease",
    "pathogens": "disease",
    "phase": "exact",
    "status": "exact",
    "sponsor": "exact",
}


@dataclass
class TrialFields:
    """
    The searchable surface of one trial, split by field authority.

    Built once per trial per query batch. Splitting at construction rather
    than searching raw_json repeatedly is what keeps per-criterion tier
    scoring affordable across a 3,000-trial corpus.
    """

    nct_id: str
    intervention_names: list[str] = field(default_factory=list)
    intervention_other_names: list[str] = field(default_factory=list)
    intervention_descriptions: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    eligibility: str = ""
    brief_summary: str = ""
    detailed_description: str = ""
    phases: list[str] = field(default_factory=list)
    status: str = ""
    sponsor: str = ""
    asset_names: list[str] = field(default_factory=list)

    @classmethod
    def from_study(cls, nct_id: str, study: dict, asset_names: list[str] | None = None):
        protocol = study.get("protocolSection", {}) or {}
        interventions = dig(protocol, "armsInterventionsModule", "interventions", default=[]) or []

        names, others, descriptions = [], [], []
        for item in interventions:
            if not isinstance(item, dict):
                continue
            if item.get("name"):
                names.append(item["name"])
            others.extend(item.get("otherNames") or [])
            if item.get("description"):
                descriptions.append(item["description"])

        return cls(
            nct_id=nct_id,
            intervention_names=names,
            intervention_other_names=others,
            intervention_descriptions=descriptions,
            conditions=dig(protocol, "conditionsModule", "conditions", default=[]) or [],
            keywords=dig(protocol, "conditionsModule", "keywords", default=[]) or [],
            eligibility=dig(protocol, "eligibilityModule", "eligibilityCriteria") or "",
            brief_summary=dig(protocol, "descriptionModule", "briefSummary") or "",
            detailed_description=dig(protocol, "descriptionModule", "detailedDescription") or "",
            phases=dig(protocol, "designModule", "phases", default=[]) or [],
            status=dig(protocol, "statusModule", "overallStatus") or "",
            sponsor=dig(protocol, "sponsorCollaboratorsModule", "leadSponsor", "name") or "",
            asset_names=asset_names or [],
        )


def _contains(haystack: str, needle: str) -> bool:
    """
    Word-boundary-anchored, case-insensitive containment.

    Boundaries are not optional. Plain substring matching lets a query for
    "azole" match "omeprazole", which is the same class of error that made
    the asset dictionary an allowlist rather than a set of stems.
    """
    if not haystack or not needle:
        return False
    return re.search(
        rf"(?<![a-z0-9]){re.escape(needle.lower())}(?![a-z0-9])", haystack.lower()
    ) is not None


def _any_contains(values, needle: str) -> bool:
    if isinstance(values, str):
        return _contains(values, needle)
    return any(_contains(v or "", needle) for v in values or [])


class StructuredScorer:
    def __init__(self, config: dict | None = None, alias_spec: dict | None = None):
        self.config = config or json.loads(RANKING_CONFIG_PATH.read_text())
        self.weights = self.config["criterion_weights"]
        self.tiers = self.config["tier_scores"]

        # Alias expansion: a query for "AmBisome" should match a trial whose
        # intervention reads "liposomal amphotericin B". The dictionary that
        # already resolves assets for the program layer is reused here
        # rather than duplicated.
        spec = alias_spec
        if spec is None and ASSET_ALIASES_PATH.exists():
            spec = json.loads(ASSET_ALIASES_PATH.read_text())
        self.alias_expansion: dict[str, set[str]] = {}
        for key, meta in ((spec or {}).get("assets") or {}).items():
            terms = {key.lower(), meta["canonical"].lower()}
            terms |= {a.lower() for a in meta.get("aliases", [])}
            if meta.get("class"):
                terms.add(meta["class"].lower())
            for term in terms:
                self.alias_expansion.setdefault(term, set()).update(terms)

    def _expand(self, term: str) -> set[str]:
        return self.alias_expansion.get(term.lower(), set()) | {term.lower()}

    # -- per-family tier resolution ----------------------------------------

    def _score_drug_term(self, fields: TrialFields, term: str) -> tuple[float, str]:
        tiers = self.tiers["drug"]
        expanded = self._expand(term)

        if any(_any_contains(fields.intervention_names, t) for t in {term}):
            return tiers["intervention_name"], "intervention_name"
        if any(_any_contains(fields.intervention_other_names, t) for t in {term}):
            return tiers["intervention_other_name"], "intervention_other_name"
        # Alias / class hit: the trial names a different string for the same
        # asset, or names the mechanistic class the asset belongs to.
        if any(_any_contains(fields.intervention_names, t)
               or _any_contains(fields.intervention_other_names, t)
               or _any_contains(fields.asset_names, t)
               for t in expanded):
            return tiers["alias_dictionary"], "alias_dictionary"
        if any(_any_contains(fields.intervention_descriptions, t) for t in expanded):
            return tiers["intervention_description"], "intervention_description"
        if any(_contains(fields.brief_summary, t) for t in expanded):
            return tiers["brief_summary"], "brief_summary"
        if any(_contains(fields.detailed_description, t) for t in expanded):
            return tiers["detailed_description"], "detailed_description"
        return 0.0, "no_match"

    def _score_disease_term(self, fields: TrialFields, term: str) -> tuple[float, str]:
        tiers = self.tiers["disease"]
        if _any_contains(fields.conditions, term):
            return tiers["condition_exact"], "condition_exact"
        if _any_contains(fields.keywords, term):
            return tiers["keyword"], "keyword"
        if _contains(fields.eligibility, term):
            return tiers["eligibility"], "eligibility"
        if _contains(fields.brief_summary, term):
            return tiers["brief_summary"], "brief_summary"
        if _contains(fields.detailed_description, term):
            return tiers["detailed_description"], "detailed_description"
        return 0.0, "no_match"

    def _score_exact(self, fields: TrialFields, criterion: str, term: str) -> tuple[float, str]:
        """
        Phase, status and sponsor are registry enums or names. Partial credit
        on them would be meaningless -- a trial is Phase 3 or it is not.
        """
        if criterion == "phase":
            hit = any(term.upper().replace(" ", "") == p.upper() for p in fields.phases)
        elif criterion == "status":
            hit = term.upper().replace(" ", "_") == fields.status.upper()
        else:
            hit = _contains(fields.sponsor, term)
        return (1.0, "registry_field") if hit else (0.0, "no_match")

    # -- public API --------------------------------------------------------

    def score(self, fields: TrialFields, criteria: dict) -> dict:
        """
        Returns the weighted structured score plus a full per-criterion
        breakdown.

        The breakdown is not diagnostics-only. It is what the dashboard
        renders when a user asks why a trial ranked where it did, and a
        ranking that cannot be explained to a domain expert who disagrees
        with it is not usable in this setting.
        """
        per_criterion: dict[str, dict] = {}
        weighted_sum = 0.0
        weight_total = 0.0

        for criterion, family in CRITERION_FAMILY.items():
            terms = criteria.get(criterion)
            if not terms:
                # Unspecified -> excluded from the denominator entirely.
                continue
            if isinstance(terms, str):
                terms = [terms]

            best_score, best_tier, best_term = 0.0, "no_match", None
            for term in terms:
                if family == "drug":
                    value, tier = self._score_drug_term(fields, term)
                elif family == "disease":
                    value, tier = self._score_disease_term(fields, term)
                else:
                    value, tier = self._score_exact(fields, criterion, term)
                if value > best_score:
                    best_score, best_tier, best_term = value, tier, term

            weight = self.weights.get(criterion, 1.0)
            weighted_sum += weight * best_score
            weight_total += weight
            per_criterion[criterion] = {
                "score": round(best_score, 4),
                "tier": best_tier,
                "matched_term": best_term,
                "weight": weight,
            }

        score = weighted_sum / weight_total if weight_total else 0.0
        return {
            "structured_score": round(score, 4),
            "criteria_scored": len(per_criterion),
            "per_criterion": per_criterion,
        }


def specified_criteria(criteria: dict) -> list[str]:
    """Which scorable criteria the question actually constrained."""
    return [c for c in CRITERION_FAMILY if criteria.get(c)]
