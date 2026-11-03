"""
tests/test_organize.py

Offline tests for stage 4 (pipeline/organize.py): every number the writer
sees comes from here, so it is tested on hand-built trials with no network.

Run:
    python tests/test_organize.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.organize import breadth_note, lead_sentence, organize, render_facts, trial_key  # noqa: E402
from pipeline.retrieve import (  # noqa: E402
    COMPARATOR_ONLY, NOT_IN_DRUG_LIST, ROLE_UNCLEAR, TESTED, Trial, TrialGroup,
)


def T(nct, phase="PHASE2", status="COMPLETED", date="2020-01", role=TESTED, sponsor="S", dtype="ACTUAL"):
    return Trial(nct, f"title {nct}", sponsor, [phase] if phase else [], status, [], [], 100, date, dtype,
                 None, False, role=role)


def check() -> list[tuple[bool, str]]:
    r = []

    # Ranking inside a group: phase, then status, then latest date; missing date last.
    ts = [T("A", "PHASE2", "RECRUITING"), T("B", "PHASE3", "COMPLETED"), T("C", "PHASE3", "RECRUITING", "2027-01"),
          T("D", "PHASE3", "RECRUITING", "2029-12"), T("E", "PHASE3", "RECRUITING", None)]
    order = [t.nct_id for t in sorted(ts, key=trial_key)]
    r.append((order == ["D", "C", "E", "B", "A"], f"trial ranking phase>status>date, missing date last: {order}"))

    g1 = TrialGroup("drugA", 40, [T("N1", "PHASE3", "RECRUITING", sponsor="X"), T("N2", "PHASE2", sponsor="X"),
                                   T("N3", role=COMPARATOR_ONLY), T("N4", role=NOT_IN_DRUG_LIST),
                                   T("N5", role=ROLE_UNCLEAR, status="NOT_YET_RECRUITING", sponsor="Y")],
                    fetched=6, excluded={"N9": "x: tested"})
    g2 = TrialGroup("drugB", 10, [T("N1", "PHASE3", "RECRUITING", role=COMPARATOR_ONLY), T("M1", "PHASE2")], fetched=2)
    g3 = TrialGroup("drugC", 0, [], 0, error="timeout")
    g4 = TrialGroup("drugD", 3, [T("Z1", role=NOT_IN_DRUG_LIST)], fetched=1)
    f = organize([g3, g4, g2, g1], total_matching=47)

    r.append(([g.term for g in f.groups] == ["drugA", "drugB", "drugD", "drugC"],
              f"groups ranked by phase/activity; no-core group then failed group last: {[g.term for g in f.groups]}"))
    a = f.groups[0]
    r.append(((a.core, a.comparator, a.off_topic, a.excluded) == (3, 1, 1, 1),
              f"buckets: core (tested+unclear) 3, comparator 1, off-topic 1, excluded 1: "
              f"{(a.core, a.comparator, a.off_topic, a.excluded)}"))
    r.append((a.active == 2 and a.top_phase == "PHASE3" and a.sponsors[0] == ("X", 2),
              f"active counts RECRUITING + NOT_YET_RECRUITING; top phase; sponsors counted: {a.active}, {a.top_phase}, {a.sponsors}"))
    # Unique trials: N1-N5, M1, Z1 = 7 (N1 is in two groups). Core: N1, N2, N5, M1 = 4
    # (N3 is comparator-only; N4 and Z1 don't list their drug).
    r.append((f.retrieved == 7 and len(f.rows) == 7, f"N1 appears in two groups but counts once: retrieved={f.retrieved}"))
    r.append((f.core_unique == 4, f"unique core trials (N1 core in A even though comparator in B): {f.core_unique}"))
    r.append((f.active_unique == 2, f"active unique core trials (N1 recruiting, N5 not yet recruiting): {f.active_unique}"))
    n1 = next(row for row in f.rows if row["trial"].nct_id == "N1")
    r.append(([term for term, _, _ in n1["groups"]] == ["drugA", "drugB"], "a trial's row lists every group it came from"))

    sent = [t.nct_id for t, _ in f.summary_trials]
    r.append(("N4" not in sent and "Z1" not in sent, "off-topic trials never reach the writer"))
    r.append((len(sent) == len(set(sent)), "each trial sent to the writer once"))
    r.append((sent[:2] == ["N1", "M1"], f"round-robin: each group's best trial first: {sent[:2]}"))
    capped = organize([g1, g2], 47, cap=2)
    r.append((len(capped.summary_trials) == 2, "cap respected"))

    r.append((lead_sentence(f) == "Based on 7 trials retrieved of 47 matching interventional studies in ClinicalTrials.gov.",
              lead_sentence(f)))
    r.append(("unknown number" in lead_sentence(organize([g1], None)), "unknown registry total is said, not shown as 0"))

    cond_only = TrialGroup(None, 5000, [T("K1")], fetched=1)
    r.append(("5,000 trials" in breadth_note(organize([cond_only], 5000)), "broad condition-only search gets the note"))
    r.append((breadth_note(organize([cond_only], 999)) == "", "below the threshold: no note"))
    r.append((breadth_note(organize([g1], 5000)) == "", "drug searches never get the note (their size comes from the drugs)"))
    r.append((breadth_note(organize([cond_only], None)) == "", "unknown total: no note rather than a guess"))

    text = render_facts("the planner's reading", f, ["x"])
    r.append(("(actual)" in text and "SEARCH FAILED" in text and "N4 |" not in text,
              "facts text: date type shown, failed group stated, off-topic trial absent"))
    r.append((text.startswith("QUESTION (as interpreted): the planner's reading"),
              "writer sees the interpretation; the raw question is not a parameter at all"))
    return r


def test_organize():
    failures = [msg for ok, msg in check() if not ok]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    results = check()
    for ok, msg in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {msg}")
    test_organize()
    print(f"\nall {len(results)} cases pass")
