"""
pipeline/retrieve.py -- stage 3: live, field-limited search. No model.

Input: already-validated conditions and drug terms, plus phase/status filters
and exclude terms. Output: one TrialGroup per drug term (or a single
condition-only group), each carrying the registry's exact total match count
and up to `per_group_cap` flattened trial records.

Why one search per drug instead of one combined search: grouping by drug then
falls out for free, and CT.gov returns an exact total for each drug, so no
model (and no Python guesswork) is needed to say "M trials match drug X".

Run directly for the step-1 check (hard-coded filters, no AI):
    python pipeline/retrieve.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.ctgov_client import CTGovClient, CTGovError, clean_term  # noqa: E402

# Field names verified against /api/v2/studies/metadata on 2026-10-29.
FIELDS = [
    "NCTId",
    "BriefTitle",
    "LeadSponsorName",
    "Phase",
    "OverallStatus",
    "Condition",
    "InterventionName",
    "InterventionType",
    "EnrollmentCount",
    "PrimaryCompletionDate",
    "WhyStopped",
    "HasResults",
]

# The cap decides WHICH trials we see when more than 25 match, so the order
# must be explicit (CT.gov's default order is documented as unsorted). Most
# recent primary completion first favours current work, matching the stage-4
# ranking rule. Only date/numeric fields are sortable, so phase can't be used.
SORT = "PrimaryCompletionDate:desc,LastUpdatePostDate:desc"

PER_GROUP_CAP = 25


@dataclass
class Trial:
    nct_id: str
    title: str
    sponsor: str
    phases: list[str]
    status: str
    conditions: list[str]
    interventions: list[str]
    enrollment: int | None
    primary_completion: str | None
    why_stopped: str | None
    has_results: bool

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"


@dataclass
class TrialGroup:
    term: str | None             # drug term; None = condition-only search
    total_matching: int          # registry's exact count for this query
    trials: list[Trial]          # fetched (<= cap), after exclusion
    fetched: int                 # fetched before exclusion
    excluded: list[str] = field(default_factory=list)  # NCT IDs removed by exclude_terms
    query: dict = field(default_factory=dict)          # exact params sent, for the trace panel
    error: str | None = None     # set if the search failed; never silently empty


def _parse(study: dict) -> Trial:
    # Every lookup uses .get() with a default: CT.gov omits whole modules
    # when a trial has no data for them (e.g. no designModule -> no phases).
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    design = p.get("designModule", {})
    return Trial(
        nct_id=ident.get("nctId", ""),
        title=ident.get("briefTitle", ""),
        sponsor=p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
        phases=design.get("phases", []),
        status=status.get("overallStatus", ""),
        conditions=p.get("conditionsModule", {}).get("conditions", []),
        interventions=[i.get("name", "") for i in p.get("armsInterventionsModule", {}).get("interventions", [])],
        enrollment=design.get("enrollmentInfo", {}).get("count"),
        primary_completion=status.get("primaryCompletionDateStruct", {}).get("date"),
        why_stopped=status.get("whyStopped"),
        has_results=bool(study.get("hasResults", False)),
    )


def _build_params(conditions: list[str], term: str | None, phases: list[str], statuses: list[str]) -> dict:
    return {
        # Parenthesised OR keeps multi-word conditions intact:
        # "(invasive candidiasis) OR (candidemia)".
        "query.cond": " OR ".join(f"({c})" for c in conditions),
        "query.intr": term,
        "filter.overallStatus": "|".join(statuses),
        "filter.advanced": f"AREA[Phase]({' OR '.join(phases)})" if phases else None,
        "sort": SORT,
    }


def _is_excluded(trial: Trial, exclude_terms: list[str]) -> bool:
    haystack = " ".join([trial.title, *trial.interventions]).lower()
    return any(t.lower() in haystack for t in exclude_terms)


def retrieve(
    conditions: list[str],
    drug_terms: list[str],
    phases: list[str] | None = None,
    statuses: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    per_group_cap: int = PER_GROUP_CAP,
    client: CTGovClient | None = None,
) -> list[TrialGroup]:
    """One group per drug term, or a single condition-only group if none."""
    # Blank terms would become "()" in the query, which CT.gov rejects.
    conditions = [c for c in map(clean_term, conditions) if c]
    drug_terms = [t for t in map(clean_term, drug_terms) if t]
    if not conditions and not drug_terms:
        raise ValueError("retrieve() needs at least one condition or drug term")
    client = client or CTGovClient()
    phases, statuses, exclude_terms = phases or [], statuses or [], exclude_terms or []

    groups = []
    for term in drug_terms or [None]:
        params = _build_params(conditions, term, phases, statuses)
        try:
            studies, total = client.search(params, FIELDS, per_group_cap)
        except CTGovError as exc:
            print(f"  search failed for {term or 'condition-only'}: {exc}")
            groups.append(TrialGroup(term, 0, [], 0, query=params, error=str(exc)))
            continue
        trials = [_parse(s) for s in studies]
        kept = [t for t in trials if not _is_excluded(t, exclude_terms)]
        excluded = [t.nct_id for t in trials if _is_excluded(t, exclude_terms)]
        groups.append(TrialGroup(term, total, kept, len(trials), excluded, params))
    return groups


def _show(label: str, groups: list[TrialGroup]) -> None:
    print(f"\n=== {label} ===")
    for g in groups:
        name = g.term or "(condition-only)"
        if g.error:
            print(f"\n[{name}] ERROR: {g.error}")
            continue
        print(f"\n[{name}] matching={g.total_matching} fetched={g.fetched} "
              f"kept={len(g.trials)} excluded={g.excluded or 0}")
        print(f"  query: { {k: v for k, v in g.query.items() if v} }")
        for t in g.trials[:5]:
            print(f"  {t.nct_id}  {'/'.join(t.phases) or '-':<14} {t.status:<22} "
                  f"{t.primary_completion or '-':<10}  {t.sponsor[:30]:<30}  {t.title[:60]}")
        if len(g.trials) > 5:
            print(f"  ... {len(g.trials) - 5} more")


if __name__ == "__main__":
    # Step-1 check: hard-coded inputs standing in for a validated plan.
    # Two unrelated fields so the search is visibly not tuned to one.
    _show("Antifungal: what else besides fluconazole for invasive candidiasis (Phase 2/3)?", retrieve(
        conditions=["invasive candidiasis", "candidemia"],
        drug_terms=["rezafungin", "ibrexafungerp", "fosmanogepix", "fluconazole"],
        phases=["PHASE2", "PHASE3"],
        exclude_terms=["fluconazole"],
    ))
    _show("Cardiometabolic: recruiting trials for obesity, by drug", retrieve(
        conditions=["obesity"],
        drug_terms=["tirzepatide", "semaglutide", "retatrutide"],
        statuses=["RECRUITING", "NOT_YET_RECRUITING"],
    ))
    _show("Condition-only: heart failure with preserved ejection fraction, Phase 3", retrieve(
        conditions=["heart failure with preserved ejection fraction"],
        drug_terms=[],
        phases=["PHASE3"],
    ))
