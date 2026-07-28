"""
query_planner.py

Converts a free-text question ("what's competing with rezafungin in
refractory candidemia?") into CT.gov API v2 query parameters.

Pipeline:
  1. Claude (Haiku) proposes a structured plan -- conditions, candidate
     intervention terms with confidence, exclude terms, phases, statuses.
  2. Every candidate intervention term is validated against the live
     registry with a cheap count-only call. Zero-hit terms are dropped
     and logged -- they never reach the real query.
  3. Validated terms are assembled into final CT.gov parameters.

Design rule this file exists to enforce: Claude proposes, the registry
disposes. Nothing the model invents can survive into a result set.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import anthropic

# common/ is a sibling of enrich/ — add repo root so it's importable
# without turning this into an installed package.
sys.path.insert(0, str(Path(__file__).parent.parent))
from common.ctgov_client import CTGovClient  # noqa: E402

logger = logging.getLogger("traillens.query_planner")

PLANNER_MODEL = "claude-haiku-4-5-20251001"  # classification-tier, not the write-up model

SYSTEM_PROMPT = """You are the query planner for TrailLens, a clinical trial research tool. Your only
job is to convert a natural-language question into a structured search plan. You do
not answer the question yourself, and you never see or return actual trial data.

SCOPE: This tool covers ClinicalTrials.gov registry data only, restricted to two
therapeutic areas: (1) antifungals and anti-infectives, (2) oncology. If a question
falls clearly outside both areas, set "out_of_scope" and leave other fields minimal.

RULES:
1. Never invent an NCT ID, sponsor name, or trial result. You have no access to the
   registry -- you are proposing search terms, not reporting findings.
2. For drug/intervention names, propose multiple candidate terms, not one guess.
   Include both generic and brand names when relevant. Mark your confidence honestly:
   "high" only for terms you are certain exist in current registry data; "low" for
   inferred or speculative class members.
3. When the question asks about "competitors" or "alternatives" to a named drug, put
   that drug's own name(s) in exclude_terms, and populate candidate_interventions with
   other agents in the same mechanistic class or indication -- never the drug itself.
4. Do not use brand names as CT.gov query terms if the generic is more likely to be
   registry-indexed -- propose the generic as high confidence and the brand as a
   lower-confidence supplementary term.
5. Interpretation must be one plain sentence a non-scientist could read and immediately
   understand what will be searched for and why.
6. Output strict JSON matching the schema below. No prose outside the JSON.

SCHEMA:
{
  "interpretation": "string",
  "conditions": ["string", "..."],
  "candidate_interventions": [
    {"term": "string", "confidence": "high|medium|low", "rationale": "string"}
  ],
  "mechanism_class": "string or null",
  "phases": ["PHASE1"|"PHASE2"|"PHASE3"|"PHASE4"|"NA", "..."],
  "statuses": ["RECRUITING"|"ACTIVE_NOT_RECRUITING"|"COMPLETED"|"TERMINATED"|"WITHDRAWN", "..."],
  "exclude_terms": ["string", "..."],
  "date_bounds": {"primary_completion_after": "YYYY-MM-DD or null", "primary_completion_before": "YYYY-MM-DD or null"},
  "out_of_scope": "string or null"
}
"""


@dataclass
class CandidateTerm:
    term: str
    confidence: str
    rationale: str
    registry_hits: Optional[int] = None  # filled in during validation


@dataclass
class QueryPlan:
    """Raw output of step 2, before validation."""
    interpretation: str
    conditions: list[str]
    candidate_interventions: list[CandidateTerm]
    mechanism_class: Optional[str]
    phases: list[str]
    statuses: list[str]
    exclude_terms: list[str]
    date_bounds: dict
    out_of_scope: Optional[str]


@dataclass
class ValidatedQuery:
    """Final, registry-checked parameters ready for the CT.gov client."""
    interpretation: str
    conditions: list[str]
    interventions: list[str]              # only terms that had registry_hits > 0
    dropped_terms: list[str]              # logged, never queried
    exclude_terms: list[str]
    phases: list[str]
    statuses: list[str]
    date_bounds: dict
    out_of_scope: Optional[str]
    term_audit: list[CandidateTerm] = field(default_factory=list)  # full trail for logging/demo


def _call_planner(question: str, client: anthropic.Anthropic) -> QueryPlan:
    response = client.messages.create(
        model=PLANNER_MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )

    raw_text = "".join(block.text for block in response.content if block.type == "text")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error("Planner returned non-JSON output: %r", raw_text)
        raise ValueError(f"Query planner did not return valid JSON: {e}") from e

    candidates = [
        CandidateTerm(term=c["term"], confidence=c["confidence"], rationale=c.get("rationale", ""))
        for c in parsed.get("candidate_interventions", [])
    ]

    return QueryPlan(
        interpretation=parsed["interpretation"],
        conditions=parsed.get("conditions", []),
        candidate_interventions=candidates,
        mechanism_class=parsed.get("mechanism_class"),
        phases=parsed.get("phases", []),
        statuses=parsed.get("statuses", []),
        exclude_terms=parsed.get("exclude_terms", []),
        date_bounds=parsed.get("date_bounds", {}),
        out_of_scope=parsed.get("out_of_scope"),
    )


def _validate_interventions(
    candidates: list[CandidateTerm],
    ctgov_client: CTGovClient,
) -> tuple[list[str], list[str], list[CandidateTerm]]:
    """
    Checks each candidate term against the live registry with a
    count-only call — every term, every call, no caching. A term
    survives only if it returns at least one trial right now. Zero-hit
    terms are dropped and logged. This is the boundary that keeps a
    hallucinated drug name from ever reaching a real query or a
    user-facing result, and checking live (rather than against a local
    snapshot) means a term that's absent from your seeded dataset today
    but exists in the registry doesn't get wrongly dropped.
    """
    accepted: list[str] = []
    dropped: list[str] = []
    audit: list[CandidateTerm] = []

    for c in candidates:
        try:
            hits = ctgov_client.count(intervention=c.term)
        except Exception as e:
            logger.warning("Validation call failed for term %r: %s", c.term, e)
            hits = 0

        c.registry_hits = hits
        audit.append(c)

        if hits > 0:
            accepted.append(c.term)
        else:
            dropped.append(c.term)
            logger.info(
                "Dropped candidate term %r (confidence=%s): zero registry hits",
                c.term, c.confidence,
            )

    return accepted, dropped, audit


def build_query(
    question: str,
    anthropic_client: anthropic.Anthropic,
    ctgov_client: CTGovClient,
) -> ValidatedQuery:
    """
    Entry point. Takes a free-text question, returns a ValidatedQuery
    containing only registry-confirmed terms plus the audit trail of
    what was proposed and dropped.
    """
    plan = _call_planner(question, anthropic_client)

    if plan.out_of_scope:
        return ValidatedQuery(
            interpretation=plan.interpretation,
            conditions=[],
            interventions=[],
            dropped_terms=[],
            exclude_terms=[],
            phases=[],
            statuses=[],
            date_bounds={},
            out_of_scope=plan.out_of_scope,
        )

    accepted, dropped, audit = _validate_interventions(plan.candidate_interventions, ctgov_client)

    if not accepted and plan.candidate_interventions:
        # every proposed term failed validation -- surface this rather
        # than silently querying on conditions alone
        logger.warning("All candidate interventions failed validation for question: %r", question)

    return ValidatedQuery(
        interpretation=plan.interpretation,
        conditions=plan.conditions,
        interventions=accepted,
        dropped_terms=dropped,
        exclude_terms=plan.exclude_terms,
        phases=plan.phases,
        statuses=plan.statuses,
        date_bounds=plan.date_bounds,
        out_of_scope=None,
        term_audit=audit,
    )


if __name__ == "__main__":
    # Hits both the Anthropic API and the live CT.gov API — needs
    # ANTHROPIC_API_KEY set, and network access to clinicaltrials.gov.
    logging.basicConfig(level=logging.INFO)
    result = build_query(
        "what's competing with rezafungin in refractory candidemia?",
        anthropic.Anthropic(),
        CTGovClient(),
    )
    print(json.dumps(result.__dict__, default=lambda o: o.__dict__, indent=2))
