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

import re
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
    # For drug roles (see drug_role):
    "InterventionOtherName",
    "InterventionArmGroupLabel",
    "ArmGroupLabel",
    "ArmGroupType",
    "InterventionMeshTerm",
]

# --- drug roles ------------------------------------------------------------
# CT.gov's search matches a drug term anywhere in a trial's intervention
# record -- names, descriptions, registry synonyms. That is not the same as
# "this trial tests the drug". Each trial records which arms are experimental
# vs comparator, and which interventions go to which arm, so Python can label
# the drug's role from registry data instead of asking a model.
TESTED = "tested"                  # given in the study arms (see drug_role for what those are)
BOTH_ARMS = "both_arms"            # background therapy: in study AND comparator arms, while another
                                   # real drug is given only in the study arms
COMPARATOR_ONLY = "comparator_only"
ROLE_UNCLEAR = "role_unclear"      # in the drug list but arms missing / OTHER / labels don't resolve,
                                   # or only in the title
OTHER_NAME = "other_name"          # only the registry's standardised (MeSH) drug term matches, e.g. term
                                   # "ibrexafungerp" vs a trial listing only "SCY-078"; arm unknown
NOT_IN_DRUG_LIST = "not_in_drug_list"  # neither name nor MeSH term matches. For a group's own
                                       # term, CT.gov matched it elsewhere (e.g. description text)

_COMPARATOR_ARMS = {"ACTIVE_COMPARATOR", "PLACEBO_COMPARATOR", "SHAM_COMPARATOR"}
_PLACEBO = re.compile(r"placebo|sham", re.IGNORECASE)
# Intervention types that count as "another drug" for head-to-head detection
# (exercise, surgery or diet in the other arm is not a head-to-head).
_DRUG_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT", "GENETIC"}

# Competitor rule (same indication, different drug): drop a trial if the named
# drug is, or may be, what the trial is testing. Unknown roles are dropped
# too -- the strict reading -- and reported, never silently. One exception:
# head-to-head trials (named drug in one study arm, a different drug in
# another) are kept and labeled, since they test a competitor directly.
EXCLUDING_ROLES = {TESTED, ROLE_UNCLEAR, OTHER_NAME}

# The cap decides WHICH trials we see when more than 25 match, so the order
# must be explicit (CT.gov's default order is documented as unsorted). Most
# recent primary completion first favours current work, matching the stage-4
# ranking rule. Only date/numeric fields are sortable, so phase can't be used.
SORT = "PrimaryCompletionDate:desc,LastUpdatePostDate:desc"

PER_GROUP_CAP = 25


@dataclass
class Intervention:
    name: str
    other_names: list[str]
    arm_labels: set[str]         # labels of the arms it is given in
    type: str = ""               # DRUG, BIOLOGICAL, BEHAVIORAL, PROCEDURE, ...


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
    arms: list[Intervention] = field(default_factory=list)
    arm_types: dict[str, str] = field(default_factory=dict)  # arm label -> EXPERIMENTAL, ACTIVE_COMPARATOR, ...
    mesh_terms: list[str] = field(default_factory=list)
    role: str | None = None      # role of this group's drug term; None for condition-only
    notes: list[str] = field(default_factory=list)  # e.g. "fluconazole: comparator_only"

    @property
    def url(self) -> str:
        return f"https://clinicaltrials.gov/study/{self.nct_id}"


@dataclass
class TrialGroup:
    term: str | None             # drug term; None = condition-only search
    total_matching: int          # registry's exact count for this query
    trials: list[Trial]          # fetched (<= cap), after exclusion
    fetched: int                 # fetched before exclusion
    excluded: dict[str, str] = field(default_factory=dict)  # NCT ID -> why, e.g. "fluconazole: tested"
    query: dict = field(default_factory=dict)          # exact params sent, for the trace panel
    error: str | None = None     # set if the search failed; never silently empty


def _parse(study: dict) -> Trial:
    # Every lookup uses .get() with a default: CT.gov omits whole modules
    # when a trial has no data for them (e.g. no designModule -> no phases).
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    design = p.get("designModule", {})
    ai = p.get("armsInterventionsModule", {})
    arm_types = {a.get("label"): a.get("type") for a in ai.get("armGroups", []) if a.get("label") and a.get("type")}
    arms = [
        Intervention(
            name=i.get("name", ""),
            other_names=i.get("otherNames", []),
            arm_labels=set(i.get("armGroupLabels", [])),
            type=i.get("type", ""),
        )
        for i in ai.get("interventions", [])
    ]
    meshes = study.get("derivedSection", {}).get("interventionBrowseModule", {}).get("meshes", [])
    return Trial(
        nct_id=ident.get("nctId", ""),
        title=ident.get("briefTitle", ""),
        sponsor=p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name", ""),
        phases=design.get("phases", []),
        status=status.get("overallStatus", ""),
        conditions=p.get("conditionsModule", {}).get("conditions", []),
        interventions=[a.name for a in arms],
        enrollment=design.get("enrollmentInfo", {}).get("count"),
        primary_completion=status.get("primaryCompletionDateStruct", {}).get("date"),
        why_stopped=status.get("whyStopped"),
        has_results=bool(study.get("hasResults", False)),
        arms=arms,
        arm_types=arm_types,
        mesh_terms=[m.get("term", "") for m in meshes],
    )


def _mentions(text: str, term: str) -> bool:
    # Whole-word, case-insensitive: "rezafungin" matches "Rezafungin for
    # Injection" but a short term can't match inside an unrelated word.
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


def _matching(trial: Trial, term: str) -> list[Intervention]:
    # "Placebo semaglutide" is not semaglutide (NCT07253285 lists one per arm).
    return [a for a in trial.arms
            if not _PLACEBO.search(a.name) and any(_mentions(n, t) for n in [a.name, *a.other_names] for t in _names(term))]


def _names(term: str | list[str]) -> list[str]:
    # A drug may be searched under several names (generic + code names);
    # any of them counts as the drug.
    return [term] if isinstance(term, str) else list(term)


def _study_arms(trial: Trial) -> set[str]:
    # "Study arms" are the EXPERIMENTAL ones. But many placebo-controlled
    # trials label their only drug arm ACTIVE_COMPARATOR (NCT07731256's arm
    # description even says "the experimental group"), so with no
    # EXPERIMENTAL arm at all, the active-comparator arms are the study arms.
    study_types = {"EXPERIMENTAL"} if "EXPERIMENTAL" in trial.arm_types.values() else {"ACTIVE_COMPARATOR"}
    return {l for l, t in trial.arm_types.items() if t in study_types}


def is_head_to_head(trial: Trial, term: str | list[str]) -> bool:
    """`term` is in a study arm AND some study arm gives a different real drug without `term`."""
    matched = _matching(trial, term)
    term_arms = set().union(*(a.arm_labels for a in matched)) if matched else set()
    study_arms = _study_arms(trial)
    if not term_arms & study_arms:
        return False
    return any(
        a.type in _DRUG_TYPES and not _PLACEBO.search(a.name) and (a.arm_labels & study_arms) - term_arms
        for a in trial.arms
        if not any(a is m for m in matched)
    )


def drug_role(trial: Trial, term: str | list[str]) -> str:
    """The role the drug (one name, or a list of its names) plays in `trial`, from registry fields only."""
    matched = _matching(trial, term)
    if not matched:
        if any(_mentions(m, t) for m in trial.mesh_terms for t in _names(term)):
            return OTHER_NAME
        # Drug list empty or vague ("GLP-1 receptor agonist") but the title
        # names the drug (NCT07622992): we can't tell its role, so say so --
        # which also keeps a trial OF an excluded drug from slipping through.
        return ROLE_UNCLEAR if any(_mentions(trial.title, t) for t in _names(term)) else NOT_IN_DRUG_LIST
    arm_type = trial.arm_types
    labels = set().union(*(a.arm_labels for a in matched)) & arm_type.keys()
    study_arms = _study_arms(trial)
    in_study = labels & study_arms
    in_comparator = {l for l in labels if arm_type[l] in _COMPARATOR_ARMS} - study_arms

    if in_study and not in_comparator:
        return TESTED
    if in_comparator and not in_study:
        return COMPARATOR_ONLY
    if not in_study:
        return ROLE_UNCLEAR  # no arm labels, or only OTHER / NO_INTERVENTION arms
    # In study AND comparator arms. Background therapy if some other real drug
    # is given only in study arms ("X + term vs placebo + term"). Otherwise
    # the trial compares doses/durations/populations of the term itself
    # (NCT07021937: semaglutide in every arm, adolescents vs adults).
    other_test_drug = any(
        a.arm_labels and a.arm_labels <= study_arms and not _PLACEBO.search(a.name)
        for a in trial.arms
        if not any(a is m for m in matched)
    )
    return BOTH_ARMS if other_test_drug else TESTED


def _or(terms: list[str]) -> str | None:
    # Parenthesised OR keeps multi-word terms intact:
    # "(invasive candidiasis) OR (candidemia)".
    if len(terms) == 1:
        return terms[0]
    return " OR ".join(f"({t})" for t in terms) or None


def _build_params(conditions: list[str], names: list[str], phases: list[str], statuses: list[str]) -> dict:
    return {
        "query.cond": _or(conditions),
        "query.intr": _or(names),
        "filter.overallStatus": "|".join(statuses),
        "filter.advanced": f"AREA[Phase]({' OR '.join(phases)})" if phases else None,
        "sort": SORT,
    }


def retrieve(
    conditions: list[str],
    drug_terms: list[str],
    phases: list[str] | None = None,
    statuses: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    per_group_cap: int = PER_GROUP_CAP,
    client: CTGovClient | None = None,
    aliases: dict[str, list[str]] | None = None,
) -> list[TrialGroup]:
    """
    One group per drug term, or a single condition-only group if none.
    `aliases` maps a drug term to its other names; a drug and its aliases
    are one search ("(a) OR (b)") and one group, so the same drug never
    shows up as two competitors.
    """
    # Blank terms would become "()" in the query, which CT.gov rejects.
    conditions = [c for c in map(clean_term, conditions) if c]
    drug_terms = [t for t in map(clean_term, drug_terms) if t]
    exclude_terms = [t for t in map(clean_term, exclude_terms or []) if t]
    aliases = {clean_term(k): [a for a in map(clean_term, v) if a] for k, v in (aliases or {}).items()}
    if not conditions and not drug_terms:
        raise ValueError("retrieve() needs at least one condition or drug term")
    client = client or CTGovClient()
    phases, statuses = phases or [], statuses or []

    groups = []
    for term in drug_terms or [None]:
        names = [term, *aliases.get(term, [])] if term else []
        params = _build_params(conditions, names, phases, statuses)
        try:
            studies, total = client.search(params, FIELDS, per_group_cap)
        except CTGovError as exc:
            print(f"  search failed for {term or 'condition-only'}: {exc}")
            groups.append(TrialGroup(term, 0, [], 0, query=params, error=str(exc)))
            continue
        trials = [_parse(s) for s in studies]
        kept, excluded = [], {}
        for t in trials:
            t.role = drug_role(t, names) if term else None
            hits, notes = [], []
            for x in exclude_terms:
                r = drug_role(t, x)
                if r == TESTED and is_head_to_head(t, x):
                    notes.append(f"head-to-head vs {x}")
                elif r in EXCLUDING_ROLES:
                    hits.append(f"{x}: {r}")
                elif r in (COMPARATOR_ONLY, BOTH_ARMS):
                    # Named drug present but not under test: keep, and say so.
                    notes.append(f"{x}: {r}")
            if hits:
                excluded[t.nct_id] = "; ".join(hits)
                continue
            t.notes = notes
            kept.append(t)
        groups.append(TrialGroup(term, total, kept, len(trials), excluded, params))
    return groups


def _show(label: str, groups: list[TrialGroup]) -> None:
    print(f"\n=== {label} ===")
    for g in groups:
        name = g.term or "(condition-only)"
        if g.error:
            print(f"\n[{name}] ERROR: {g.error}")
            continue
        roles = {}
        for t in g.trials:
            roles[t.role] = roles.get(t.role, 0) + 1
        print(f"\n[{name}] matching={g.total_matching} fetched={g.fetched} "
              f"kept={len(g.trials)} roles={roles}")
        for nct, why in g.excluded.items():
            print(f"  EXCLUDED {nct}  ({why})")
        for t in g.trials[:8]:
            print(f"  {t.nct_id}  {t.role or '-':<16} {'/'.join(t.phases) or '-':<14} {t.status:<20} "
                  f"{t.sponsor[:24]:<24}  {t.title[:50]}  {t.notes or ''}")
        if len(g.trials) > 8:
            print(f"  ... {len(g.trials) - 8} more")


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
