"""
analyze/landscape.py

Deterministic landscape metrics. No model call anywhere in this file.

WHY EVERY METRIC CARRIES ITS FORMULA
====================================
Each metric returns `{value, formula, basis, evidence}` rather than a bare
number, for three consumers:

  the dashboard     renders the formula next to the number, so "most active
                    sponsor" is never a claim the user has to take on trust
  synthesize/       is given these values to write prose FROM, so the model
                    is not asked to count anything
  validate/         RECOMPUTES superlatives independently and compares them
                    against what the model claimed. That is the design where
                    the deterministic layer grades the LLM rather than
                    merely sitting beside it.

A metric whose formula cannot be written in one sentence does not belong
here. That rule is what killed "fastest-moving companies" -- see below.

WHAT THIS FILE REFUSES TO COMPUTE
=================================
**Phase velocity.** The v2 proposal asks for "fastest-moving companies".
That requires observing a program at Phase 2 and later at Phase 3 -- a
claim about two points in time. v1 deliberately dropped snapshot history,
so `trial_snapshots` holds exactly one baseline observation per trial and
there is no velocity to measure. Any single-snapshot proxy presented under
that label would be an unlabelled inference, which is the failure the data
honesty rule exists to prevent, and a domain expert would catch it live.

So the card is `most_active_sponsors`, defined as trials INITIATED in a
trailing window, with the window in the formula string. Different question,
honestly labelled. Real velocity becomes computable once snapshots
accumulate; it is not backfillable and the README says so.

THREE THINGS THE CORPUS FORCED
==============================
1. **Programs split by sponsor class.** `(sponsor, asset)` mints a program
   for every investigator-initiated trial, so an academic running one
   oteseconazole study becomes an "oteseconazole program" -- 994 programs
   across 48 assets, corpus-wide. `programs.sponsor_class` comes straight
   from the registry, so industry-led (435) and investigator-initiated are
   separable as fact rather than inference. Headline counts use industry-led.

2. **23% of trials have no usable phase.** 501 are `NA` and 182 are null.
   They cannot be placed on a phase axis at all, so they are excluded from
   phase metrics and counted in `coverage` instead of being silently
   dropped or coerced to Phase 1.

3. **Umbrella indications are excluded from crowding.** "Invasive Fungal
   Infection" and "Mycoses" cover many markets at once. A program tagged
   only with those cannot be attributed to a specific market without
   inventing the attribution, so crowding counts `specificity='specific'`
   at `rollup_depth=0` -- directly stated, not inherited from a child.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.paths import DB_PATH  # noqa: E402

# Ordered so "most advanced" is a max over a number rather than a lookup
# table of special cases. Combined phases sit between their endpoints: a
# PHASE1/PHASE2 study is genuinely further along than pure Phase 1 and not
# as far as pure Phase 2.
#
# NA and None map to None, not 0. NA means "not applicable" -- device and
# behavioural studies -- and coercing it to Phase 1 would invent an early
# program out of a trial that has no phase concept at all.
PHASE_RANK: dict[str, float] = {
    "EARLY_PHASE1": 0.5,
    "PHASE1": 1.0,
    "PHASE1/PHASE2": 1.5,
    "PHASE2": 2.0,
    "PHASE2/PHASE3": 2.5,
    "PHASE3": 3.0,
    "PHASE3/PHASE4": 3.5,
    "PHASE4": 4.0,
}
PHASE_LABEL = {v: k for k, v in PHASE_RANK.items()}

# PHASE 4 IS NOT "MORE ADVANCED". It is post-approval.
#
# The first run of this module returned "most advanced: Micafungin, Astellas,
# PHASE 4" -- arithmetically correct and analytically wrong. Phase 4 means the
# asset is already marketed, so a Phase 4 study of a 2005 generic is not a
# more advanced competitor than a Phase 3 study of a novel compound; it is
# past the race the landscape is describing. Ranking it top buries exactly the
# programs a BD or investor audience is looking for.
#
# So "most advanced" is computed over DEVELOPMENT phases only (1 through 3),
# and Phase 4 assets are reported separately as already marketed. Both are
# useful; conflating them answers neither question.
DEVELOPMENT_CEILING = 3.5

ACTIVE_STATUSES = ("RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION",
                   "NOT_YET_RECRUITING")
STOPPED_STATUSES = ("TERMINATED", "WITHDRAWN", "SUSPENDED")
ACTIVITY_WINDOW_MONTHS = 24


def phase_rank(phase: str | None) -> float | None:
    """None for NA, null, or anything unrecognised -- never a default."""
    if not phase:
        return None
    return PHASE_RANK.get(phase.strip().upper())


def development_rank(rank: float | None) -> float | None:
    """
    Phase rank restricted to pre-approval development.

    Returns None for Phase 4, so a marketed asset cannot win "most
    advanced". See the DEVELOPMENT_CEILING note above.
    """
    if rank is None or rank > DEVELOPMENT_CEILING:
        return None
    return rank


def metric(value, formula: str, basis: str = "registry_field", evidence=None) -> dict:
    """
    Every number this module emits is wrapped.

    `basis` distinguishes a registry fact from a dictionary lookup from a
    derived value, matching the vocabulary in attribute_provenance, so the
    dashboard can mark derived numbers and validate/ knows which claims it
    can check against a stored field.
    """
    return {
        "value": value,
        "formula": formula,
        "basis": basis,
        "evidence": list(evidence or []),
    }


class LandscapeAnalyzer:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._marketed: set[str] | None = None

    def marketed_assets(self) -> set[str]:
        """
        Assets with a Phase 4 trial ANYWHERE in the corpus.

        Determined corpus-wide, not per query, because approval is a
        property of the asset rather than of one result set. Micafungin is
        approved whether or not a particular question happens to retrieve
        one of its Phase 4 trials, and letting the answer depend on the
        query would make "most advanced in development" unstable across
        questions about the same space.

        A Phase 4 trial is a proxy for approval, not a record of it -- the
        registry has no approval field. It is a good proxy (Phase 4 is
        defined as post-marketing) and a stated one, which is the standard
        the honesty rule asks for.
        """
        if self._marketed is None:
            rows = self.conn.execute(
                """SELECT DISTINCT p.asset_name
                     FROM programs p
                     JOIN program_trials pt ON pt.program_id = p.program_id
                     JOIN trials t ON t.nct_id = pt.nct_id
                    WHERE UPPER(COALESCE(t.phase,'')) LIKE '%PHASE4%'"""
            ).fetchall()
            self._marketed = {r[0] for r in rows}
        return self._marketed

    # -- loading -----------------------------------------------------------

    def _trials(self, nct_ids: list[str]) -> dict[str, dict]:
        if not nct_ids:
            return {}
        marks = ",".join("?" * len(nct_ids))
        rows = self.conn.execute(
            f"""SELECT nct_id, title, phase, overall_status, sponsor, enrollment,
                       why_stopped, raw_json
                  FROM trials WHERE nct_id IN ({marks})""",
            nct_ids,
        ).fetchall()

        out: dict[str, dict] = {}
        for nct_id, title, phase, status, sponsor, enrollment, why, raw_json in rows:
            protocol = json.loads(raw_json).get("protocolSection", {}) or {}
            start = (
                (protocol.get("statusModule", {}) or {})
                .get("startDateStruct", {})
                .get("date")
            )
            out[nct_id] = {
                "nct_id": nct_id, "title": title, "phase": phase,
                "phase_rank": phase_rank(phase), "status": status or "",
                "sponsor": sponsor, "enrollment": enrollment,
                "why_stopped": (why or "").strip() or None,
                "start_date": start,
                "sponsor_class": (
                    (protocol.get("sponsorCollaboratorsModule", {}) or {})
                    .get("leadSponsor", {}).get("class")
                ),
            }
        return out

    def _programs(self, nct_ids: list[str]) -> dict[str, dict]:
        """Programs represented in this trial set, with only their trials here."""
        if not nct_ids:
            return {}
        marks = ",".join("?" * len(nct_ids))
        rows = self.conn.execute(
            f"""SELECT p.program_id, p.asset_name, p.sponsor, p.sponsor_norm,
                       p.sponsor_class, p.drug_class, p.match_method, pt.nct_id
                  FROM programs p JOIN program_trials pt
                    ON pt.program_id = p.program_id
                 WHERE pt.nct_id IN ({marks})""",
            nct_ids,
        ).fetchall()

        programs: dict[str, dict] = {}
        for pid, asset, sponsor, sponsor_norm, sclass, dclass, method, nct_id in rows:
            entry = programs.setdefault(pid, {
                "program_id": pid, "asset_name": asset, "sponsor": sponsor,
                "sponsor_norm": sponsor_norm, "sponsor_class": sclass,
                "drug_class": dclass, "match_method": method, "nct_ids": [],
            })
            entry["nct_ids"].append(nct_id)
        return programs

    def _indications(self, nct_ids: list[str], specific_only: bool = True,
                     direct_only: bool = True) -> dict[str, list[str]]:
        """{indication_canonical: [nct_id, ...]}"""
        if not nct_ids:
            return {}
        marks = ",".join("?" * len(nct_ids))
        sql = f"""SELECT canonical, nct_id FROM trial_indications
                   WHERE kind = 'indication' AND nct_id IN ({marks})"""
        if specific_only:
            sql += " AND specificity = 'specific'"
        if direct_only:
            sql += " AND rollup_depth = 0"
        mapping: dict[str, list[str]] = defaultdict(list)
        for canonical, nct_id in self.conn.execute(sql, nct_ids):
            mapping[canonical].append(nct_id)
        return dict(mapping)

    # -- derived program view ---------------------------------------------

    def _program_rows(self, programs: dict, trials: dict,
                      indications: dict) -> list[dict]:
        """
        One row per program: the shape the competitive-landscape table and
        the bubble chart both read.

        A program's phase is the MAX over its trials in this set, because a
        sponsor with a Phase 3 study is a Phase 3 competitor regardless of
        how many Phase 1 studies it also ran. Enrollment is the max for the
        same reason -- it is the scale the sponsor has committed to.
        """
        by_trial_indications: dict[str, list[str]] = defaultdict(list)
        for canonical, ncts in indications.items():
            for nct_id in ncts:
                by_trial_indications[nct_id].append(canonical)

        rows = []
        for pid, program in programs.items():
            own = [trials[n] for n in program["nct_ids"] if n in trials]
            if not own:
                continue
            enrollments = [t["enrollment"] for t in own if t["enrollment"]]
            best_phase = max((t["phase_rank"] for t in own
                              if t["phase_rank"] is not None), default=None)
            best_dev = max((development_rank(t["phase_rank"]) for t in own
                            if development_rank(t["phase_rank"]) is not None),
                           default=None)

            program_indications = sorted({
                ind for t in own for ind in by_trial_indications.get(t["nct_id"], [])
            })
            rows.append({
                **program,
                "trial_count": len(own),
                "phase_rank": best_phase,
                "phase": PHASE_LABEL.get(best_phase) if best_phase else None,
                "development_rank": best_dev,
                "development_phase": PHASE_LABEL.get(best_dev) if best_dev else None,
                "is_marketed": program["asset_name"] in self.marketed_assets(),
                "max_enrollment": max(enrollments, default=None),
                "indications": program_indications,
                "has_active_trial": any(t["status"] in ACTIVE_STATUSES for t in own),
                "stopped_trials": [t for t in own if t["status"] in STOPPED_STATUSES],
                "latest_start": max((t["start_date"] for t in own if t["start_date"]),
                                    default=None),
            })
        return rows

    # -- public ------------------------------------------------------------

    def analyze(self, nct_ids: list[str]) -> dict:
        trials = self._trials(nct_ids)
        programs = self._programs(nct_ids)
        indications = self._indications(nct_ids)
        rows = self._program_rows(programs, trials, indications)

        industry = [r for r in rows if r["sponsor_class"] == "INDUSTRY"]
        investigator = [r for r in rows if r["sponsor_class"] != "INDUSTRY"]

        return {
            "counts": self._counts(trials, rows, industry, investigator, indications),
            "cards": self._cards(trials, rows, industry, indications),
            "crowding": self._crowding(industry, indications),
            "attrition": self._attrition(rows),
            "coverage": self._coverage(trials, rows),
            # Development stage first, marketed assets after: the table should
            # lead with the race, not with the incumbents.
            "programs": sorted(
                rows,
                key=lambda r: (r["is_marketed"],
                               -(r["development_rank"] or r["phase_rank"] or 0),
                               -(r["max_enrollment"] or 0)),
            ),
        }

    def _counts(self, trials, rows, industry, investigator, indications) -> dict:
        phase3 = [r for r in industry
                  if r["development_rank"] is not None and r["development_rank"] >= 3.0]
        return {
            "trials": metric(len(trials), "count of retrieved trials"),
            "programs_industry": metric(
                len(industry),
                "distinct (sponsor, asset) pairs where programs.sponsor_class = "
                "'INDUSTRY'",
                evidence=[r["program_id"] for r in industry[:10]],
            ),
            "programs_investigator_initiated": metric(
                len(investigator),
                "distinct (sponsor, asset) pairs where sponsor_class != 'INDUSTRY' "
                "-- academic and government sponsors running trials of an asset "
                "they do not own",
            ),
            "companies": metric(
                len({r["sponsor_norm"] for r in industry}),
                "distinct normalized sponsor names among INDUSTRY-class programs",
            ),
            "assets": metric(
                len({r["asset_name"] for r in rows}),
                "distinct canonical assets from normalize/asset_aliases.json",
                basis="alias_dictionary",
            ),
            "mechanism_classes": metric(
                len({r["drug_class"] for r in rows if r["drug_class"]}),
                "distinct drug_class values on represented programs",
                basis="alias_dictionary",
            ),
            "indications": metric(
                len(indications),
                "distinct specific indications directly stated by a retrieved "
                "trial (umbrella terms and inherited roll-ups excluded)",
            ),
            "phase3_plus_programs": metric(
                len(phase3),
                "INDUSTRY programs whose most advanced pre-approval trial is "
                "Phase 3. Phase 4 does not count -- it is post-approval",
                evidence=[n for r in phase3 for n in r["nct_ids"][:2]][:10],
            ),
        }

    def _cards(self, trials, rows, industry, indications) -> dict:
        cards: dict[str, dict] = {}

        # Marketed assets are excluded entirely, not just their Phase 4
        # trials. An approved drug running a Phase 3 in a new indication is
        # doing real development, but it is not what "most advanced program
        # in this landscape" is asking -- the reader wants the unapproved
        # asset closest to market. Reporting micafungin there buries
        # fosmanogepix and rezafungin, which is the opposite of useful.
        in_development = [r for r in industry
                          if r["development_rank"] is not None and not r["is_marketed"]]
        if in_development:
            # Ties broken by enrollment then trial count, both descending.
            # Stated rather than left to sort stability, because "most
            # advanced" is a claim validate/ will recompute.
            top = max(in_development, key=lambda r: (r["development_rank"],
                                                     r["max_enrollment"] or 0,
                                                     r["trial_count"]))
            cards["most_advanced_in_development"] = metric(
                {"program": top["asset_name"], "sponsor": top["sponsor"],
                 "phase": top["development_phase"], "program_id": top["program_id"]},
                "INDUSTRY program with the highest phase rank among Phase 1-3; "
                "ties broken by max enrollment, then trial count. Phase 4 is "
                "EXCLUDED -- it means already marketed, so a Phase 4 study of an "
                "approved generic is not a more advanced competitor than a "
                "Phase 3 of a novel asset. Assets with a Phase 4 trial anywhere "
                "in the corpus are excluded as already approved",
                evidence=top["nct_ids"],
            )

        marketed = [r for r in industry if r["is_marketed"]]
        if marketed:
            cards["already_marketed"] = metric(
                sorted({r["asset_name"] for r in marketed}),
                "INDUSTRY assets with a Phase 4 trial anywhere in the corpus -- "
                "approved and on the market, so they set the standard of care "
                "the development-stage programs are measured against rather "
                "than competing within it",
                evidence=[n for r in marketed for n in r["nct_ids"][:1]][:10],
            )

        active = [t for t in trials.values()
                  if t["status"] in ACTIVE_STATUSES and t["enrollment"]]
        if active:
            largest = max(active, key=lambda t: t["enrollment"])
            cards["largest_active_trial"] = metric(
                {"nct_id": largest["nct_id"], "sponsor": largest["sponsor"],
                 "enrollment": largest["enrollment"], "title": largest["title"]},
                f"max enrollment among trials with status in {ACTIVE_STATUSES}",
                evidence=[largest["nct_id"]],
            )

        # Renamed from the proposal's "fastest-moving companies". See the
        # module docstring: velocity needs two observations and the snapshot
        # table holds one.
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=ACTIVITY_WINDOW_MONTHS * 30)).strftime("%Y-%m")
        recent = Counter(
            t["sponsor"] for t in trials.values()
            if t["start_date"] and t["start_date"][:7] >= cutoff and t["sponsor"]
        )
        if recent:
            cards["most_active_sponsors"] = metric(
                [{"sponsor": s, "trials_started": n} for s, n in recent.most_common(5)],
                f"sponsors by count of retrieved trials with a start date on or "
                f"after {cutoff} (trailing {ACTIVITY_WINDOW_MONTHS} months). "
                f"NOT phase velocity -- that needs two snapshots and "
                f"trial_snapshots currently holds one baseline per trial.",
            )
        return cards

    def _crowding(self, industry, indications) -> dict:
        """
        Crowding by indication, and by indication x mechanism class.

        The matrix is the more useful view and it is the reason drug_class
        is carried on every program. "11 programs in candidemia" invites
        the follow-up question; "11 programs, 6 of them echinocandins"
        answers it, because a crowded space where everyone shares one
        mechanism is a differentiation opportunity rather than a closed
        market.
        """
        per_indication: dict[str, set[str]] = defaultdict(set)
        matrix: dict[tuple[str, str], set[str]] = defaultdict(set)

        for row in industry:
            for indication in row["indications"]:
                per_indication[indication].add(row["program_id"])
                matrix[(indication, row["drug_class"] or "(class unknown)")].add(
                    row["program_id"]
                )

        ranked = sorted(per_indication.items(), key=lambda kv: -len(kv[1]))
        out: dict = {
            "by_indication": [
                {"indication": name, "programs": len(pids)} for name, pids in ranked
            ],
            "matrix": [
                {"indication": ind, "mechanism_class": cls, "programs": len(pids)}
                for (ind, cls), pids in sorted(matrix.items(), key=lambda kv: -len(kv[1]))
            ],
        }

        if ranked:
            name, pids = ranked[0]
            classes = Counter(
                row["drug_class"] or "(class unknown)"
                for row in industry if name in row["indications"]
            )
            dominant, dominant_n = classes.most_common(1)[0]
            out["most_crowded_indication"] = metric(
                {"indication": name, "programs": len(pids),
                 "dominant_mechanism": dominant,
                 "dominant_mechanism_programs": dominant_n,
                 "distinct_mechanisms": len(classes)},
                "specific indication with the most distinct INDUSTRY programs, "
                "counting programs rather than trials; umbrella indications and "
                "inherited roll-ups excluded",
                evidence=sorted(pids)[:10],
            )
        return out

    def _attrition(self, rows) -> dict:
        """
        Programs with a stopped trial and a stated reason.

        `whyStopped` is only ever populated when something went wrong, which
        makes it the one place the registry says so in plain text. v1's
        materiality whitelist already identified it as the highest-value
        field; nothing in the v2 proposal's comparison table surfaces it.
        """
        stopped = []
        for row in rows:
            for trial in row["stopped_trials"]:
                stopped.append({
                    "program": row["asset_name"], "sponsor": row["sponsor"],
                    "program_id": row["program_id"], "nct_id": trial["nct_id"],
                    "status": trial["status"],
                    "why_stopped": trial["why_stopped"],
                    "phase": trial["phase"],
                })
        with_reason = [s for s in stopped if s["why_stopped"]]
        return {
            "stopped_trials": metric(
                len(stopped),
                f"retrieved trials with status in {STOPPED_STATUSES}",
                evidence=[s["nct_id"] for s in stopped[:10]],
            ),
            "with_stated_reason": metric(
                len(with_reason),
                "of those, trials where the registry's whyStopped field is "
                "populated -- a field only ever filled in when something "
                "went wrong",
                evidence=[s["nct_id"] for s in with_reason[:10]],
            ),
            "programs_affected": metric(
                len({s["program_id"] for s in stopped}),
                "distinct programs with at least one stopped trial",
            ),
            "detail": with_reason,
        }

    def _coverage(self, trials, rows) -> dict:
        """
        What the corpus does NOT know, reported alongside what it does.

        Same instinct as ingest's capped-term report. 23% of the corpus has
        no usable phase, so a phase axis that silently omits them is a chart
        that misrepresents its own completeness.
        """
        total = max(len(trials), 1)
        no_phase = [t for t in trials.values() if t["phase_rank"] is None]
        no_enrollment = [t for t in trials.values() if not t["enrollment"]]
        unknown_status = [t for t in trials.values() if t["status"] == "UNKNOWN"]
        unplaced = [r for r in rows if r["phase_rank"] is None]
        no_program = [n for n in trials if not any(n in r["nct_ids"] for r in rows)]

        return {
            "trials_without_phase": metric(
                len(no_phase),
                "retrieved trials whose phase is NA, null, or unrecognised. "
                "Excluded from all phase metrics and from the phase axis -- "
                "NA means 'not applicable', so coercing it to Phase 1 would "
                "invent an early program",
            ),
            "trials_without_enrollment": metric(
                len(no_enrollment), "retrieved trials with no enrollment count; "
                "they cannot be sized on the bubble chart"),
            "trials_status_unknown": metric(
                len(unknown_status),
                "retrieved trials with overall_status = UNKNOWN, meaning the "
                "sponsor has not verified the record recently -- these are "
                "neither confirmed active nor confirmed stopped"),
            "trials_without_program": metric(
                len(no_program),
                "retrieved trials whose interventions resolved to no asset in "
                "asset_aliases.json -- usually co-medications or comparator-only "
                "arms. They count as trials but contribute no program"),
            "programs_without_phase": metric(
                len(unplaced),
                "programs with no phase-rankable trial in this result set"),
            "phase_coverage_pct": metric(
                round(100 * (1 - len(no_phase) / total), 1),
                "100 * (1 - trials_without_phase / retrieved trials)",
                basis="derived_from_text",
            ),
        }

    # White space analysis was here and was removed.
    #
    # It crossed every indication with every mechanism class and looked for
    # empty cells, on the theory that gaps matter as much as crowds. The
    # grid came out 24 x 23, and 460 of 552 cells were empty -- because most
    # indication x mechanism pairs are biologically implausible rather than
    # unexploited. There is no topical agent for candidemia and never will
    # be. Making the number mean anything took a paragraph of caveats.
    #
    # A metric that needs a paragraph before it can be read is not a metric
    # this project should carry. Everything else in this file has a
    # one-sentence formula, and that consistency is worth more than one
    # extra card.
