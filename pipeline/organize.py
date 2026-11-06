"""
pipeline/organize.py -- stage 4: group, count, rank, cap. Pure Python, no
model, no network, so every number the answer uses can be tested offline.

Output:
  - Facts: per-drug numbers plus a capped, ranked trial list -> rendered as
    compact plain text for the writer (render_facts). The writer only words
    numbers that are already here; it never counts.
  - rows: every retrieved trial, once, for the UI table.

Which trials count. Using each trial's drug role (retrieve.drug_role):
  core        tested, or listed with an unclear role       -> counted as "trials of the drug"
  comparator  comparator only, or background in all arms   -> counted separately, labelled
  off_topic   not in the drug list at all                  -> shown in the table, never counted
Condition-only searches have no drug, so every trial is core.

Ranking (documented, nothing subjective): highest phase, then most active
status, then most recent primary completion date. Groups rank by the same
three facts over their core trials. Ties keep registry order.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Only the data shapes and role labels are imported -- organize never calls
# retrieve, so it can be tested on hand-built groups.
from pipeline.retrieve import (  # noqa: E402
    BOTH_ARMS, COMPARATOR_ONLY, NOT_IN_DRUG_LIST, OTHER_NAME, ROLE_UNCLEAR, TESTED, Trial, TrialGroup,
)

SUMMARY_TRIAL_CAP = 40

# Design doc: recruiting + active not recruiting + not yet recruiting.
# ENROLLING_BY_INVITATION is added: it is recruiting, by invitation.
ACTIVE = {"RECRUITING", "ACTIVE_NOT_RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"}
PHASE_RANK = {"EARLY_PHASE1": 1, "PHASE1": 2, "PHASE2": 3, "PHASE3": 4, "PHASE4": 5}  # NA / missing = 0
STATUS_RANK = {"RECRUITING": 8, "ACTIVE_NOT_RECRUITING": 7, "ENROLLING_BY_INVITATION": 6, "NOT_YET_RECRUITING": 5,
               "COMPLETED": 4, "SUSPENDED": 3, "TERMINATED": 2, "WITHDRAWN": 1}

CORE_ROLES = {TESTED, OTHER_NAME, ROLE_UNCLEAR, None}
COMPARATOR_ROLES = {COMPARATOR_ONLY, BOTH_ARMS}

# The registry's own sponsor classes (AgencyClass in the CT.gov spec), so the
# writer can say "mostly industry" from counted data, not from guessing at names.
SPONSOR_CLASS_LABEL = {
    "INDUSTRY": "industry", "NIH": "US NIH", "FED": "US federal agency", "OTHER_GOV": "other government",
    "INDIV": "individual", "NETWORK": "research network",
    "OTHER": "other (universities, hospitals, foundations)", "AMBIG": "unknown", "UNKNOWN": "unknown", "": "unknown",
}

ROLE_LABEL = {
    TESTED: "tests it", OTHER_NAME: "lists it under another name (role not recorded)",
    ROLE_UNCLEAR: "lists it (role not recorded)", COMPARATOR_ONLY: "uses it only as comparator",
    BOTH_ARMS: "uses it as background in all arms", NOT_IN_DRUG_LIST: "does not list it as an intervention",
    None: "",
}


def highest_phase(t: Trial) -> int:
    return max((PHASE_RANK.get(p, 0) for p in t.phases), default=0)


def phase_label(rank: int) -> str:
    return {v: k for k, v in PHASE_RANK.items()}.get(rank, "no phase listed")


def trial_key(t: Trial) -> tuple:
    """Sort key, best first: phase, then activity, then latest completion."""
    return (-highest_phase(t), -STATUS_RANK.get(t.status, 0), _neg_date(t.primary_completion))


def _neg_date(d: str | None) -> tuple:
    # Latest first; missing dates last. "2029-12" vs "2029-12-31" compare fine as digits.
    digits = [int(x) for x in (d or "").split("-") if x.isdigit()]
    return (0, *[-x for x in digits]) if digits else (1,)


@dataclass
class GroupFacts:
    term: str | None             # None = condition-only search
    aliases: list[str]
    registry_total: int          # registry's own count for this drug + conditions + filters
    fetched: int
    excluded: int                # removed by the competitor rule
    core: int
    comparator: int
    off_topic: int
    active: int                  # active core trials
    top_phase: str               # highest phase among core trials
    sponsors: list[tuple[str, int]]  # core trials per lead sponsor, most first (top 5)
    latest: str | None           # most recent primary completion among core trials
    error: str | None = None


@dataclass
class Facts:
    total_matching: int | None   # M: unique trials matching the whole search (registry count)
    retrieved: int               # N: unique trials retrieved and kept
    core_unique: int             # unique trials that test (or list) one of the drugs
    active_unique: int           # of those, how many are active (see ACTIVE)
    groups: list[GroupFacts]
    summary_trials: list[tuple[Trial, str | None]]  # (trial, group term), capped and ranked
    rows: list[dict]             # one per unique trial, for the UI table
    phase_counts: dict[str, int] = field(default_factory=dict)   # unique core trials by highest phase
    status_counts: dict[str, int] = field(default_factory=dict)  # unique core trials by status
    sponsor_class_counts: dict[str, int] = field(default_factory=dict)  # unique core trials by sponsor type


def organize(groups: list[TrialGroup], total_matching: int | None,
             cap: int = SUMMARY_TRIAL_CAP) -> Facts:
    ranked: list[tuple[GroupFacts, list[Trial]]] = []
    for g in groups:
        core = [t for t in g.trials if t.role in CORE_ROLES]
        comp = [t for t in g.trials if t.role in COMPARATOR_ROLES]
        off = [t for t in g.trials if t.role == NOT_IN_DRUG_LIST]
        gf = GroupFacts(
            term=g.term, aliases=g.aliases, registry_total=g.total_matching, fetched=g.fetched,
            excluded=len(g.excluded), core=len(core), comparator=len(comp), off_topic=len(off),
            active=sum(t.status in ACTIVE for t in core),
            top_phase=phase_label(max((highest_phase(t) for t in core), default=0)),
            sponsors=Counter(t.sponsor for t in core if t.sponsor).most_common(5),
            latest=max((t.primary_completion for t in core if t.primary_completion), default=None,
                       key=lambda d: [int(x) for x in d.split("-") if x.isdigit()]),
            error=g.error,
        )
        # Within a group: core trials first, then comparator, then off-topic; each by trial_key.
        ordered = sorted(core, key=trial_key) + sorted(comp, key=trial_key) + sorted(off, key=trial_key)
        ranked.append((gf, ordered))

    def group_key(item):
        gf, ordered = item
        core = [t for t in ordered if t.role in CORE_ROLES]
        best = min((trial_key(t) for t in core), default=(1,))
        return (gf.error is not None, gf.core == 0, -max((highest_phase(t) for t in core), default=0),
                -gf.active, _neg_date(gf.latest), best)

    ranked.sort(key=group_key)
    gfacts = [gf for gf, _ in ranked]

    # Rows for the UI: each trial once, listing every group (and role) it came from.
    rows: dict[str, dict] = {}
    for gf, ordered in ranked:
        for t in ordered:
            row = rows.setdefault(t.nct_id, {"trial": t, "groups": []})
            row["groups"].append((gf.term, t.role, t.notes))

    # Writer's input: round-robin across ranked groups so every drug is
    # represented, core and comparator trials only, each trial once.
    queues = [[t for t in ordered if t.role != NOT_IN_DRUG_LIST] for _, ordered in ranked]
    picked, seen = [], set()
    while len(picked) < cap and any(queues):
        for (gf, _), q in zip(ranked, queues):
            while q and q[0].nct_id in seen:
                q.pop(0)
            if q and len(picked) < cap:
                t = q.pop(0)
                seen.add(t.nct_id)
                picked.append((t, gf.term))

    core_ids = {nct for nct, r in rows.items() if any(role in CORE_ROLES for _, role, _ in r["groups"])}
    core_trials = [rows[n]["trial"] for n in core_ids]
    return Facts(
        total_matching=total_matching,
        retrieved=len(rows),
        core_unique=len(core_ids),
        active_unique=sum(t.status in ACTIVE for t in core_trials),
        groups=gfacts,
        summary_trials=picked,
        rows=list(rows.values()),
        phase_counts=dict(Counter(phase_label(highest_phase(t)) for t in core_trials).most_common()),
        status_counts=dict(Counter(t.status for t in core_trials).most_common()),
        sponsor_class_counts=dict(Counter(SPONSOR_CLASS_LABEL.get(t.sponsor_class, "unknown")
                                          for t in core_trials).most_common()),
    )


# A condition-only search matching this many trials can only be sampled
# thinly (25 of them). Grid examples: "cancer drugs" 98,075; "liver cancer"
# 4,381; "AF trials" 2,689. Mesothelioma (525) stays below.
BROAD_SEARCH_THRESHOLD = 1000


def breadth_note(f: Facts) -> str:
    """A nudge to narrow very broad searches. Python decides when; nothing is declined."""
    condition_only = all(g.term is None for g in f.groups)
    if not condition_only or f.total_matching is None or f.total_matching < BROAD_SEARCH_THRESHOLD:
        return ""
    return (f"This search matches {f.total_matching:,} trials, so the {f.retrieved} shown are a small sample. "
            "A narrower question (a specific drug, phase, or recruitment status) gives a more complete answer.")


def lead_sentence(f: Facts) -> str:
    """Written by Python, not the model, so it can't be dropped or misstated."""
    m = f"{f.total_matching:,}" if f.total_matching is not None else "an unknown number of"
    return f"Based on {f.retrieved:,} trials retrieved of {m} matching interventional studies in ClinicalTrials.gov."


def render_facts(interpretation: str, f: Facts, exclude_terms: list[str]) -> str:
    """
    Compact plain text (not JSON) for the writer: fewer tokens, easier to read.

    The user's raw question is deliberately NOT included: the writer works
    from the planner's interpretation, so text typed into the box can never
    address the writer directly (grid X6: it replied "I can't share my
    system prompt" to an injected request).
    """
    lines = [
        f"QUESTION (as interpreted): {interpretation}",
        f"SCOPE: interventional studies only (trials that test an intervention); observational studies are not searched.",
        f"TOTALS: {f.total_matching if f.total_matching is not None else 'unknown'} trials match the search in the registry; "
        f"{f.retrieved} were retrieved (at most 25 per drug); {f.core_unique} of those test or list one of the searched drugs.",
        f"ACTIVE: {f.active_unique} of those {f.core_unique} are active (recruiting, not yet recruiting, "
        "active but not recruiting, or enrolling by invitation).",
    ]
    searched = [g for g in f.groups if g.term]
    if searched:
        with_trials = [g.term for g in searched if g.core]
        lines.append(f"DRUGS: {len(searched)} searched; {len(with_trials)} have trials testing or listing them"
                     + (f" ({', '.join(with_trials)})" if with_trials else "") + ".")
    if exclude_terms:
        lines.append(f"EXCLUDED:trials testing {', '.join(exclude_terms)} were removed (the user asked for alternatives); "
                     f"{sum(g.excluded for g in f.groups)} removed in total.")
    if f.phase_counts:
        lines.append("BY HIGHEST PHASE: " + "; ".join(f"{k}: {v}" for k, v in f.phase_counts.items()))
    if f.status_counts:
        lines.append("BY STATUS: " + "; ".join(f"{k}: {v}" for k, v in f.status_counts.items()))
    if f.sponsor_class_counts:
        lines.append("BY LEAD SPONSOR TYPE: " + "; ".join(f"{k}: {v}" for k, v in f.sponsor_class_counts.items()))
    lines.append("")
    for i, g in enumerate(f.groups, 1):
        name = g.term or "ALL INTERVENTIONS (condition-only search)"
        if g.error:
            lines.append(f"GROUP {i}: {name}: SEARCH FAILED, no data.")
            continue
        alias = f" (also searched as {', '.join(g.aliases)})" if g.aliases else ""
        sponsors = ", ".join(f"{s} ({n})" for s, n in g.sponsors) or "none"
        lines.append(
            f"GROUP {i}: {name}{alias}. Registry matches: {g.registry_total}. Retrieved: {g.fetched}. "
            f"Trials testing or listing it: {g.core} (active: {g.active}; highest phase: {g.top_phase}; "
            f"latest primary completion: {g.latest or 'none listed'}). "
            f"Using it only as comparator or background: {g.comparator}. "
            f"Not listing it as an intervention: {g.off_topic}. Lead sponsors: {sponsors}."
        )
    lines += ["", "TRIALS (ranked; a sample, not every trial):"]
    for t, term in f.summary_trials:
        date = t.primary_completion or "none listed"
        if t.primary_completion and t.primary_completion_type:
            date += f" ({t.primary_completion_type.lower()})"
        role = ROLE_LABEL.get(t.role, "")
        bits = [
            t.nct_id, "/".join(t.phases) or "no phase", t.status, f"primary completion {date}",
            t.sponsor or "sponsor not listed", f"enrollment {t.enrollment}" if t.enrollment is not None else "enrollment not listed",
            "results posted" if t.has_results else "no results posted",
        ]
        if term:
            bits.append(f"{term}: {role}")
        if t.why_stopped:
            bits.append(f"why stopped: {t.why_stopped[:150]}")
        if t.notes:
            bits.append("; ".join(t.notes))
        lines.append(" | ".join(bits) + f" | {t.title[:120]}")
    return "\n".join(lines)
