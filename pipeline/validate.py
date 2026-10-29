"""
pipeline/validate.py -- stage 2: "Claude proposes, the registry disposes."

Every condition, intervention, alias and exclude term the planner proposed
is counted against the live registry (count-only, never cached: registry
state is a live fact). The model's self-reported confidence is not an input.

  hits > 0         kept     ("existence verified by registry")
  hits == 0        dropped  (and reported, with the count)
  the call failed  unverified -- NOT treated as zero. A timeout must not
                   silently drop a real drug (v1 did exactly that). The term
                   is kept, labelled, and the search stage reports its own
                   result for it.

Known limit: this proves a term EXISTS in the registry, not that the model's
claim about it (class, mechanism) is right. It is also lenient for
multi-word names containing a common word: "vitamin Q12" matched 16 trials
on "vitamin" + "q12h" dosing text. The drug-role labels in retrieve.py are
the second check: none of those trials lists such a drug.

Takes plain lists (no planner objects) so it can be tested alone.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.ctgov_client import CTGovClient, CTGovError  # noqa: E402

KEPT, DROPPED, UNVERIFIED = "kept", "dropped", "unverified"

# Outcomes
OK = "ok"                              # search with what survived
CONDITION_ONLY = "condition_only"      # every intervention dropped; conditions survive
NO_CONDITIONS = "no_conditions"        # the plan named conditions and none exist
NOTHING_TO_SEARCH = "nothing_to_search"
REGISTRY_UNAVAILABLE = "registry_unavailable"

# 60 parallel count calls took 2.9 s with no throttling (tested 2026-10-29).
MAX_PARALLEL = 8


@dataclass
class TermCheck:
    term: str
    kind: str                    # condition | intervention | alias | exclude
    hits: int | None             # None when the call failed
    status: str                  # kept | dropped | unverified
    parent: str | None = None    # for aliases: the intervention they belong to
    error: str | None = None


@dataclass
class Validation:
    outcome: str
    conditions: list[str]                    # kept (incl. unverified)
    interventions: dict[str, list[str]]      # kept intervention -> its kept aliases
    exclude_terms: list[str]                 # passed through unchanged (see below)
    checks: list[TermCheck] = field(default_factory=list)  # full audit trail for the trace panel
    message: str = ""                        # plain-language note for the user, if any


def _count(client: CTGovClient, kind: str, term: str) -> tuple[int | None, str | None, bool]:
    """(hits, error, invalid). invalid = the term itself is unusable (e.g. too long)."""
    try:
        if kind == "condition":
            return client.count(condition=term), None, False
        return client.count(intervention=term), None, False
    except ValueError as e:
        # A fact about the input, not a registry failure: drop it, or the
        # search stage would be handed the same term and reject it.
        return None, str(e), True
    except CTGovError as e:
        return None, str(e), False


def validate(
    conditions: list[str],
    interventions: dict[str, list[str]],
    exclude_terms: list[str] | None = None,
    client: CTGovClient | None = None,
) -> Validation:
    """`interventions` maps each proposed intervention to its aliases."""
    client = client or CTGovClient()
    exclude_terms = exclude_terms or []
    if not conditions and not interventions:
        return Validation(NOTHING_TO_SEARCH, [], {}, exclude_terms,
                          message="There is no condition or intervention to search for.")

    jobs = [("condition", c, None) for c in conditions]
    for term, aliases in interventions.items():
        jobs.append(("intervention", term, None))
        jobs += [("alias", a, term) for a in aliases]
    # Exclude terms are checked for the report only. Excluding a name the
    # registry doesn't know removes nothing, so it is harmless to keep, and
    # "the drug you named has 0 trials" is worth telling the user.
    jobs += [("exclude", x, None) for x in exclude_terms]

    with ThreadPoolExecutor(MAX_PARALLEL) as pool:
        results = list(pool.map(lambda j: _count(client, "condition" if j[0] == "condition" else "intervention", j[1]), jobs))

    checks = []
    for (kind, term, parent), (hits, error, invalid) in zip(jobs, results):
        status = DROPPED if invalid else UNVERIFIED if hits is None else KEPT if hits > 0 else DROPPED
        checks.append(TermCheck(term, kind, hits, status, parent, error))
        if status == DROPPED:
            print(f"  validate: dropped {kind} {term[:60]!r} ({error or '0 registry hits'})")
        elif status == UNVERIFIED:
            print(f"  validate: could not verify {kind} {term!r}: {error}")

    def alive(kind: str, term: str) -> bool:
        return any(c.kind == kind and c.term == term and c.status != DROPPED for c in checks)

    kept_conditions = [c for c in conditions if alive("condition", c)]
    kept_interventions = {}
    for term, aliases in interventions.items():
        kept_aliases = [a for a in aliases if alive("alias", a)]
        # A drug survives if ANY of its names exists: a code name can carry
        # a drug whose generic name the registry doesn't use yet.
        if alive("intervention", term) or kept_aliases:
            kept_interventions[term] = kept_aliases

    v = Validation(OK, kept_conditions, kept_interventions, exclude_terms, checks)

    if checks and all(c.status == UNVERIFIED for c in checks):
        v.outcome = REGISTRY_UNAVAILABLE
        v.message = "ClinicalTrials.gov could not be reached, so nothing could be checked or searched. Please try again shortly."
    elif conditions and not kept_conditions:
        v.outcome = NO_CONDITIONS
        names = ", ".join(f'"{c}"' for c in conditions)
        v.message = f"No trials in the registry match the condition {names}. Check the spelling, or try a more common name."
    elif not kept_conditions and not kept_interventions:
        v.outcome = NOTHING_TO_SEARCH
        names = ", ".join(f'"{t}"' for t in interventions)
        v.message = f"None of the interventions ({names}) appear in the registry, so there is nothing to search."
    elif interventions and not kept_interventions:
        v.outcome = CONDITION_ONLY
        v.message = ("None of the proposed interventions appear in the registry, "
                     "so this searches by condition only.")
    if v.outcome in (OK, CONDITION_ONLY):
        # Partial drops matter too: if the user asked about a drug by name, a
        # quiet entry in the trace panel is not enough.
        notes = [v.message] if v.message else []
        missing = [t for t in interventions if t not in kept_interventions]
        if missing and v.outcome == OK:
            notes.append("Not found in the registry: " + ", ".join(missing) + ".")
        missing_ex = [c.term for c in checks if c.kind == "exclude" and c.status == DROPPED]
        if missing_ex:
            notes.append("The registry has no trials listing " + ", ".join(missing_ex) + ".")
        unverified = [c.term for c in checks if c.status == UNVERIFIED]
        if unverified:
            notes.append("Could not check " + ", ".join(unverified) + " (registry error); searched anyway.")
        v.message = " ".join(notes)
    return v
