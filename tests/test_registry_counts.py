"""
tests/test_registry_counts.py

Offline tests for the registry-wide counts (pipeline/run.py:
registry_breakdown) and how they reach the writer (organize.render_facts).
The registry is replaced by a fake count function, so no network or API key.

Why this exists: a phase 3 Alzheimer's search showed "6 active", which was
the count inside the 25-trial sample, not the registry. These counts come
from the registry itself and must stay separate from sample counts.

Run:
    python tests/test_registry_counts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.organize import ACTIVE, organize, render_facts  # noqa: E402
from pipeline.retrieve import TESTED, Trial, TrialGroup  # noqa: E402
from pipeline.run import registry_breakdown  # noqa: E402


def fake_registry(calls: list, fail_on: str | None = None):
    """Returns a stand-in for count_matching that records each call."""
    numbers = {"PHASE1": 40, "PHASE2": 90, "PHASE3": 120, "PHASE4": 15, "EARLY_PHASE1": 3}

    def count(conditions, names, phases, statuses):
        calls.append((tuple(phases), tuple(statuses)))
        if fail_on and fail_on in (phases or []) + (statuses or []):
            return None, {}
        if statuses and set(statuses) <= ACTIVE:  # the active call
            return 41, {}
        return numbers.get(phases[0], 0) if len(phases) == 1 else 300, {}
    return count


def check() -> list[tuple[bool, str]]:
    r = []

    calls = []
    active, phases = registry_breakdown(["alzheimer disease"], [], [], [], count=fake_registry(calls))
    r.append((active == 41, f"no status filter: active counted with all four active statuses: {active}"))
    r.append((set(calls[0][1]) == ACTIVE, "active call uses exactly the ACTIVE statuses"))
    r.append((phases == {"EARLY_PHASE1": 3, "PHASE1": 40, "PHASE2": 90, "PHASE3": 120, "PHASE4": 15},
              f"one exact count per phase: {phases}"))

    calls = []
    active, phases = registry_breakdown(["x"], [], ["PHASE3"], [], count=fake_registry(calls))
    r.append((phases == {}, "search already limited to one phase: no phase split"))
    r.append((calls == [(("PHASE3",), tuple(sorted(ACTIVE)))], "the active count keeps the search's phase filter"))

    calls = []
    active, _ = registry_breakdown(["x"], [], ["PHASE3"], ["COMPLETED"], count=fake_registry(calls))
    r.append((active == 0 and calls == [], "status filter with no active status: 0 without a registry call"))

    calls = []
    registry_breakdown(["x"], [], ["PHASE3"], ["RECRUITING", "COMPLETED"], count=fake_registry(calls))
    r.append((calls[0][1] == ("RECRUITING",), "status filter: active is the overlap with ACTIVE only"))

    calls = []
    active, phases = registry_breakdown(["x"], [], [], [], count=fake_registry(calls, fail_on="RECRUITING"))
    r.append((active is None, "failed active call is None (unknown), never 0"))
    calls = []
    _, phases = registry_breakdown(["x"], [], [], [], count=fake_registry(calls, fail_on="PHASE2"))
    r.append((phases["PHASE2"] is None and phases["PHASE3"] == 120, "one failed phase call is unknown; others kept"))

    # The writer sees registry-wide and sample counts as clearly different lines.
    t = Trial("N1", "t", "S", ["PHASE3"], "COMPLETED", [], [], 10, "2020-01", "ACTUAL", None, False, role=TESTED)
    f = organize([TrialGroup("drugA", 300, [t], fetched=1)], 300)
    f.registry_active, f.registry_phases = 41, {"PHASE3": 120, "PHASE2": None}
    text = render_facts("reading", f, [])
    r.append(("REGISTRY-WIDE ACTIVE: 41 of the 300" in text, "writer gets the exact active count against M"))
    r.append(("PHASE3: 120" in text and "PHASE2" not in text.split("REGISTRY-WIDE BY PHASE")[1].split("\n")[0],
              "unknown phase counts are left out, not written as 0"))
    r.append(("ACTIVE IN SAMPLE: 0 of the 1" in text, "the sample's active count is labelled as the sample"))

    f2 = organize([TrialGroup("drugA", 300, [t], fetched=1)], 300)
    text2 = render_facts("reading", f2, [])
    r.append(("REGISTRY-WIDE" not in text2, "no registry counts: no registry-wide lines"))
    return r


def test_registry_counts():
    failures = [msg for ok, msg in check() if not ok]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    results = check()
    for ok, msg in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {msg}")
    test_registry_counts()
    print(f"\nall {len(results)} cases pass")
