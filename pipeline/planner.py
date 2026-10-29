"""
pipeline/planner.py -- stage 1: question -> plan, "unsupported", or "too_vague".

Claude (Haiku) proposes; Python tidies and checks the proposal before any
search runs. Nothing here touches the registry: term validation is stage 2.

Run directly to plan one question (calls the Anthropic API):
    .venv/bin/python pipeline/planner.py "what is being tested for gout?"
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.ctgov_client import clean_term  # noqa: E402
from pipeline.llm import PLANNER_MODEL, LLMError, call_json  # noqa: E402
from pipeline.prompts import PLANNER_SYSTEM  # noqa: E402

PLAN, UNSUPPORTED, TOO_VAGUE = "plan", "unsupported", "too_vague"
INVALID_INPUT = "invalid_input"  # decided by Python before any model call
ERROR = "error"                  # the model call failed; message in `reason`

MAX_QUESTION_CHARS = 1000

# Enum values verified against the CT.gov OpenAPI spec (Phase, Status) on
# 2026-10-29. Expanded-access statuses (AVAILABLE, ...) are left out on purpose.
PHASES = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]
STATUSES = ["NOT_YET_RECRUITING", "RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING",
            "COMPLETED", "SUSPENDED", "TERMINATED", "WITHDRAWN"]

_STR_LIST = {"type": "array", "items": {"type": "string"}}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "enum": [PLAN, UNSUPPORTED, TOO_VAGUE]},
        "interpretation": {"type": "string"},
        "conditions": _STR_LIST,
        "candidate_interventions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "rationale": {"type": "string"},
                },
                "required": ["term", "confidence", "rationale"],
                "additionalProperties": False,
            },
        },
        "phases": {"type": "array", "items": {"type": "string", "enum": PHASES}},
        "statuses": {"type": "array", "items": {"type": "string", "enum": STATUSES}},
        "exclude_terms": _STR_LIST,
        "reason": {"type": "string"},
        "example_rephrasings": _STR_LIST,
    },
    "required": ["result", "interpretation", "conditions", "candidate_interventions", "phases",
                 "statuses", "exclude_terms", "reason", "example_rephrasings"],
    "additionalProperties": False,
}


@dataclass
class Candidate:
    term: str
    confidence: str   # recorded for the trace panel; never used to decide anything
    rationale: str


@dataclass
class Plan:
    result: str
    question: str
    interpretation: str = ""
    conditions: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    reason: str = ""
    example_rephrasings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # Python's corrections to the model's plan
    model: str = ""
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0


def _terms(values, warnings: list[str], label: str) -> list[str]:
    """Clean, de-duplicate (case-insensitive), drop blanks and over-long terms."""
    out, seen = [], set()
    for v in values if isinstance(values, list) else []:
        try:
            t = clean_term(str(v))
        except ValueError as e:
            warnings.append(f"dropped {label} term: {e}")
            continue
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _tidy(raw: dict, question: str) -> Plan:
    """Turn the model's JSON into a Plan, reading every field defensively."""
    w: list[str] = []
    result = raw.get("result")
    if result not in (PLAN, UNSUPPORTED, TOO_VAGUE):
        w.append(f"model returned unknown result {result!r}; treated as too_vague")
        result = TOO_VAGUE

    rephrasings = [str(r).strip() for r in raw.get("example_rephrasings", []) or [] if str(r).strip()][:3]
    if result != PLAN:
        return Plan(result, question, reason=str(raw.get("reason", "") or "").strip(),
                    example_rephrasings=rephrasings, warnings=w)

    exclude = _terms(raw.get("exclude_terms", []), w, "exclude")
    excluded = {e.lower() for e in exclude}
    candidates, seen = [], set()
    for c in raw.get("candidate_interventions", []) or []:
        c = c if isinstance(c, dict) else {}
        terms = _terms([c.get("term", "")], w, "intervention")
        if not terms or terms[0].lower() in seen:
            continue
        if terms[0].lower() in excluded:
            # The competitor rule says the named drug is never a candidate.
            w.append(f"removed {terms[0]!r} from candidates: it is also an exclude term")
            continue
        seen.add(terms[0].lower())
        candidates.append(Candidate(terms[0], c.get("confidence", "low"), str(c.get("rationale", ""))))

    plan = Plan(
        PLAN, question,
        interpretation=str(raw.get("interpretation", "") or "").strip(),
        conditions=_terms(raw.get("conditions", []), w, "condition"),
        candidates=candidates,
        phases=[p for p in raw.get("phases", []) or [] if p in PHASES],
        statuses=[s for s in raw.get("statuses", []) or [] if s in STATUSES],
        exclude_terms=exclude,
        warnings=w,
    )
    if not plan.conditions and not plan.candidates:
        # Nothing to search on. Say so rather than search the whole registry.
        w.append("model returned a plan with no conditions and no interventions; treated as too_vague")
        return Plan(TOO_VAGUE, question, reason="The question names no condition or treatment to search for.",
                    example_rephrasings=rephrasings, warnings=w)
    if not plan.interpretation:
        w.append("model gave no interpretation")
    return plan


def plan_question(question: str) -> Plan:
    """Stage 1 entry point. Never raises for bad input or a failed model call."""
    question = (question or "").strip()
    if not question:
        return Plan(INVALID_INPUT, question, reason="Please type a question.")
    if len(question) > MAX_QUESTION_CHARS:
        return Plan(INVALID_INPUT, question[:80] + "...",
                    reason=f"The question is {len(question):,} characters; please keep it under {MAX_QUESTION_CHARS:,}.")

    start = time.monotonic()
    try:
        # Tags mark where the user's text starts and ends, so instructions
        # inside it read as data (the prompt says so too).
        reply = call_json(PLANNER_SYSTEM, f"<question>\n{question}\n</question>", PLAN_SCHEMA,
                          model=PLANNER_MODEL, max_tokens=8000, effort="medium")
    except LLMError as e:
        print(f"  planner call failed: {e}")
        return Plan(ERROR, question, reason=str(e), seconds=round(time.monotonic() - start, 1))

    plan = _tidy(reply.data, question)
    plan.model, plan.usage, plan.seconds = reply.model, reply.usage, round(time.monotonic() - start, 1)
    for warning in plan.warnings:
        print(f"  planner warning: {warning}")
    return plan


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is being tested for gout?"
    print(json.dumps(asdict(plan_question(q)), indent=2))
