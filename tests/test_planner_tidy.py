"""
tests/test_planner_tidy.py

Offline tests for the Python half of stage 1: the input guards and the
tidy-up of whatever JSON the model returns. No API key, no network. The
model's own judgement is tested by running real questions, not here.

Run:
    python tests/test_planner_tidy.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.planner import (  # noqa: E402
    INVALID_INPUT, PLAN, TOO_VAGUE, UNSUPPORTED, _tidy, plan_question,
)

GOOD = {
    "result": "plan", "interpretation": "Trials for gout, any treatment.",
    "conditions": ["gout", "Gout", "  gouty arthritis "],
    "candidate_interventions": [
        {"term": "febuxostat", "confidence": "high", "rationale": "x"},
        {"term": "Febuxostat", "confidence": "low", "rationale": "duplicate"},
        {"term": "allopurinol", "confidence": "high", "rationale": "named drug, wrongly listed"},
        {"term": "[18F]tracer(x)", "confidence": "low", "rationale": "Essie syntax chars"},
    ],
    "phases": ["PHASE3", "PHASE9"], "statuses": ["RECRUITING", "ALIVE"],
    "exclude_terms": ["Allopurinol"], "reason": "", "example_rephrasings": [],
}


def check() -> list[tuple[bool, str]]:
    r = []

    p = _tidy(GOOD, "q")
    r.append((p.result == PLAN, "good plan stays a plan"))
    r.append((p.conditions == ["gout", "gouty arthritis"], f"conditions de-duplicated and trimmed: {p.conditions}"))
    r.append(([c.term for c in p.candidates] == ["febuxostat", "18F tracer x"],
              f"duplicate and excluded candidates removed, syntax chars cleaned: {[c.term for c in p.candidates]}"))
    r.append((any("allopurinol" in w.lower() for w in p.warnings), "removing an excluded candidate is reported"))
    r.append((p.phases == ["PHASE3"] and p.statuses == ["RECRUITING"], "invalid phase/status values dropped"))

    p = _tidy({"result": "plan", "interpretation": "x", "conditions": ["c"], "exclude_terms": ["drugx"],
               "candidate_interventions": [
                   {"term": "alpha", "aliases": ["ALP-1", "alpha", "DrugX", "ALP-1"], "confidence": "high", "rationale": ""},
                   {"term": "ALP-1", "aliases": [], "confidence": "low", "rationale": "same drug again"}]}, "q")
    r.append(([(c.term, c.aliases) for c in p.candidates] == [("alpha", ["ALP-1"])],
              f"aliases: repeats, the term itself, and excluded names removed; an alias can't reappear as "
              f"its own candidate: {[(c.term, c.aliases) for c in p.candidates]}"))

    p = _tidy({"result": "plan", "interpretation": "x", "conditions": ["multiple sclerosis", "MS"],
               "candidate_interventions": [{"term": "drugA", "aliases": ["DA", "DA-123"], "confidence": "high",
                                            "rationale": ""}]}, "q")
    r.append((p.conditions == ["multiple sclerosis"] and p.candidates[0].aliases == ["DA-123"] and len(p.warnings) == 2,
              f"short abbreviations dropped from conditions and aliases, with warnings: {p.conditions}, "
              f"{p.candidates[0].aliases}"))
    p = _tidy({"result": "plan", "interpretation": "x", "conditions": ["RSV"], "candidate_interventions": []}, "q")
    r.append((p.conditions == ["RSV"], "a short condition is kept when it is the only one (nothing better to search)"))

    p = _tidy({}, "q")
    r.append((p.result == TOO_VAGUE and p.warnings, "empty JSON -> too_vague with a warning, no crash"))

    p = _tidy({"result": "plan", "interpretation": "x", "conditions": [], "candidate_interventions": []}, "q")
    r.append((p.result == TOO_VAGUE, "plan with nothing to search -> too_vague"))

    p = _tidy({"result": "plan", "conditions": "gout", "candidate_interventions": ["not-a-dict"]}, "q")
    r.append((p.result == TOO_VAGUE, "wrong types (string not list, list of strings) -> no crash"))

    p = _tidy({"result": "unsupported", "reason": "Predictions.", "example_rephrasings": ["a", "b", "c", "d"],
               "conditions": ["gout"]}, "q")
    r.append((p.result == UNSUPPORTED and p.conditions == [] and len(p.example_rephrasings) == 3,
              "unsupported ignores plan fields, keeps at most 3 rephrasings"))

    p = _tidy({"result": "maybe"}, "q")
    r.append((p.result == TOO_VAGUE and p.warnings, "unknown result -> too_vague with a warning"))

    # Input guards run before any model call, so these need no API key.
    r.append((plan_question("   ").result == INVALID_INPUT, "blank question -> invalid_input"))
    r.append((plan_question("x" * 5000).result == INVALID_INPUT, "5,000-char question -> invalid_input"))
    return r


def test_planner_tidy():
    failures = [msg for ok, msg in check() if not ok]
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    results = check()
    for ok, msg in results:
        print(f"{'ok  ' if ok else 'FAIL'}  {msg}")
    test_planner_tidy()
    print(f"\nall {len(results)} cases pass")
